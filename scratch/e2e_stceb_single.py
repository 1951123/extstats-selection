"""E2E deploy for stats_CEB_single (Phase-1 chosen set), naive coexist order.

census diverge: stats_CEB_single is multi-table; each query filters one base
table, OID/interference is per-table. We build the Phase-1 chosen (colset,param)
on its owning table, ANALYZE each table once, EXPLAIN all 180 candidate-bearing
queries. If this easy workload shows little cross-stat interference (low overlap
per table), naive coexist already ≈ predicted; else we then apply per-table
FB-order.

Usage: create fresh `stats_e2e` mirror from `stats` first, then:
  .venv/bin/python -u scratch/e2e_stceb_single.py --db stats_e2e --level 1 --budget 80000
"""
from __future__ import annotations
import argparse, json, collections, sys, time
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from extstats2.config import DBConfig, get_backend
from extstats2.bench import load_benchmark
from extstats2.core.predicates import predicate_columns
from extstats2.core.measure_io import read_query_measure


def _qtab(q):
    pcs = predicate_columns(q)
    return ("." + next(iter(pcs)).lstrip(".").lower()) if len(pcs) == 1 else None


def metrics(qs):
    q = np.asarray([max(float(v),1.0) for v in qs if v==v], float)
    return {"mean": float(q.mean()), "geo": float(np.exp(np.log(q).mean())) if len(q) else None,
            "max": float(q.max()) if len(q) else None}


def main(level, budget, db, table_for_meta, out, strategy="naive"):
    from extstats2.core.optimize_sgrid import load_sgrid_problem, build_inner_at_level
    from extstats2.core.optimize import solve_ilp, OptimizerClass
    from extstats2.backend.base import StatObject
    from extstats2.backend.capabilities import SAMPLING_NONE
    corpus = ROOT / "results" / "measure" / "stats_ceb_single" / "postgres"
    meta, blocks = load_sgrid_problem(ROOT/"results"/"measure", "stats_ceb_single", "postgres")
    phys, opts, qbases = build_inner_at_level(blocks, str(level), skip_worse_than_baseline=True)
    res = solve_ilp(phys, list(opts), [float(v) for v in qbases], budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR, per_query_cap=1)
    pred = res.mean_qerror
    qids_order = list(blocks.keys())
    pred_by_qid = {qid: float(qv) for qid,qv in zip(qids_order, res.qerror_per_query)}
    base_by_qid = {qid: float(b) for qid,b in zip(qids_order, qbases)}

    # chosen colsets with param
    chosen = {tuple(p.columns): p.level for p in res.selected_stats}
    Q = load_benchmark("stats_ceb_single")
    qc_load = {q.qid: read_query_measure(corpus, q.qid) for q in Q}
    # colset -> table: unique table whose column-owner contains it, resolved by the qids it serves
    # authoritatively resolve chosen colset -> table from the query the optimizer
    # actually SERVED it to (each served qid is single-table), asserting consistency.
    # res.chosen[i] = list of stat keys "|a,b|L<p>" served to qids_order[i]
    cs_table = {}
    q_single_tab = {q.qid: _qtab(q) for q in Q}   # None if not single table (won't happen)
    for i, qid in enumerate(qids_order):
        tab = q_single_tab.get(qid)
        for key in res.chosen[i]:
            inner = key.split("|")[1] if key.count("|") >= 2 else None
            if inner is None:
                continue
            cols = tuple(sorted(c.strip() for c in inner.split(",")))
            prev = cs_table.get(cols)
            if prev is not None and prev != tab:
                print("WARN cols", cols, "served on multiple tables:", prev, tab)
            cs_table[cols] = tab
    print("chosen distinct", len(chosen), "; resolved to table for", len(cs_table),
          "; unresolved:", sorted(set(chosen) - set(cs_table))[:5])

    # deploy grouped by table (naive coexist, inside-table create order = solver discovery; keep deterministic: sort)
    be = get_backend("postgres", cfg=DBConfig(host="localhost",port=5432,user="postgres",
                     password="postgres", dbname=db))
    cap = be.supported_capabilities()
    cap_mcv = [c for c in cap if c.name=='mcv'][0]
    # per-table chosen set
    tab_cs = collections.defaultdict(list)
    for cs, tab in cs_table.items():
        tab_cs[tab].append((cs, chosen[cs]))

    # determine the per-table CREATE order.
    # naive : lexicographic (deterministic).
    # fb    : Phase-2 weighted feedback-arc per table (queries on that table).
    table_order = {}   # tab -> list of (cols,param) in creation order
    for tab, items in tab_cs.items():
        if strategy != "fb":
            table_order[tab] = sorted(items, key=lambda x: (x[0], x[1]))
            continue
        # gather queries that filter THIS table and their applicable chosen colset on it
        from extstats2.core.optimize_pg_order import ChosenStat, solve_pg_order_fb
        local_cs = sorted(items, key=lambda x: x[0])
        q_on_tab = [q for q in Q if _qtab(q) == tab]
        chosens=[]
        for i,(cs,p) in enumerate(local_cs):
            skey=f"|{','.join(sorted(cs))}|P{p}"
            qerd={}
            for q in q_on_tab:
                blk=qc_load.get(q.qid)
                s=blk.get('by_lambda',{}).get(str(level)) if blk else None
                if not s: continue
                for cd in s.get('candidates',[]):
                    if tuple(sorted(cd['cols']))==cs and int(cd['param'])==p:
                        qerd[q.qid]=float(cd['qerror'])
            chosens.append(ChosenStat(columns=tuple(sorted(cs)),stat_key=skey,qerr_by_query=qerd))
        # applicable per query: chosen colset subset of its predicate cols
        appl={}
        def preds_of(q):
            pc=predicate_columns(q)
            return set().union(*(set(c) for c in pc.values())) if pc else set()
        for q in q_on_tab:
            preds=preds_of(q)
            idx=tuple(i for i,(cs,p) in enumerate(local_cs) if set(cs)<=preds)
            if idx: appl[q.qid]=idx
        sol=solve_pg_order_fb(chosens, base_by_qid, appl)
        order_loc = sol["order_idx"]
        table_order[tab]=[local_cs[i] for i in order_loc]
        print(f"  table {tab}: fb-order n_cut={sol.get('n_feedback_cut')}")

    created=0
    for tab, items in table_order.items():
        for obj in list(be.list_stats(tab)):
            be.drop_stat(obj)
        objs=[]
        for i,(cs,p) in enumerate(items):
            nm=f"e2e_{i}_{'_'.join(sorted(cs)).lower()}_p{p}"
            st=StatObject(table=tab, columns=tuple(cs), capability=cap_mcv,
                          sampling_level=SAMPLING_NONE, name=nm)
            be.create_stat(st)
            objs.append((st,p))
        for st,p in objs:
            with be.conn.cursor() as cur:
                cur.execute(f"ALTER STATISTICS {st.name} SET STATISTICS {int(p)}")
        with be.conn.cursor() as cur:
            cur.execute(f"ANALYZE {tab.lstrip('.')}")
        created += len(objs)
        print(f"table {tab}: {len(objs)} stats ANALYZEd (strategy={strategy})")
    print("total created", created)

    # EXPLAIN the candidate-bearing queries in deployed state
    true_q={}
    for q in Q:
        qid=q.qid
        if qid not in pred_by_qid: continue
        e = be.estimate(q)
        est=float(e.estimate) if e.estimate else float("nan")
        truth=float(q.ground_truth)
        true_q[qid]= max(est/truth,truth/est) if est==est and truth else float("nan")
    want=[qid for qid in qids_order]
    tru=[true_q[qid] for qid in want if qid in true_q]
    predv=[pred_by_qid[qid] for qid in want if qid in pred_by_qid]
    print(f"TRUE({strategy} coexist):", metrics(tru))
    print("PRED(intf-free):", metrics(predv))
    # per-query true vs predicted path usage
    res_json={"level":level,"budget":budget,"db":db,"strategy":strategy,
              "predicted_metrics":metrics(predv),
              "true_metrics":metrics(tru),"n":len(tru),
              "per_query":{qid:true_q[qid] for qid in want if qid in true_q}}
    json.dump(res_json, open(out,"w"), indent=1)
    print("wrote", out)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--level",type=int,default=1)
    ap.add_argument("--budget",type=int,default=80000)
    ap.add_argument("--db",default="stats_e2e")
    ap.add_argument("--table",default=".posts")
    ap.add_argument("--strategy",choices=["naive","fb"],default="naive")
    ap.add_argument("--out",default=str(ROOT/"results"/"e2e_stceb_single_L1_80KB.json"))
    a=ap.parse_args()
    main(str(a.level),a.budget,a.db,a.table,a.out,a.strategy)
