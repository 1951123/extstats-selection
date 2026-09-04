"""Two-phase PG-order E2E: Phase1 generic set choice + Phase2 OID-order solve,
then deploy the Phase-2 order on a fresh mirror and EXPLAIN all census queries.

Phase 1 (unchanged generic): solve L1 @ budget via ``solve_ilp`` -> chosen set.
Phase 2: build ``ChosenStat`` per chosen colset with isolated e_is from corpus,
compute per-query applicable chosen colsets, call
``optimize_pg_order.solve_pg_order`` -> CREATE order.
Deploy in that order -> EXPLAIN 468 -> compare true vs (generic) predicted.

Usage:
  .venv/bin/python -u scratch/e2e_order_deploy.py --level 1 --budget 100000 \
      --db census_e2e3 --out results/e2e_order_L1_100KB.json
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def numeric_qid_sort(qid):
    return int(qid.split(".")[1])


def main(level: str, budget: int, db: str, table: str, deploy: bool, out: Path):
    from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
    from extstats2.core.optimize import solve_ilp, OptimizerClass
    from extstats2.core.optimize_pg_order import ChosenStat, solve_pg_order
    from extstats2.bench import load_benchmark
    from extstats2.core.measure_lambda_io import read_query_measure

    corpus = ROOT / "results" / "per_lambda" / "census" / "postgres"
    meta, blocks = load_lambda_problem(ROOT / "results" / "per_lambda", "census", "postgres")
    phys, opts, qbases = build_inner_at_level(blocks, level, skip_worse_than_baseline=True)
    qb = [float(v) for v in qbases]
    res = solve_ilp(phys, opts, qb, budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, objective="mean")
    chosen_stats = res.selected_stats          # list[PhysicalStat]
    cols2id = {}                               # (table,columns)-> phys index used in build_inner
    n_stats = len(phys)
    # predicted qerror per query by qid (build_inner order uses blocks keys, numeric? load_lambda_problem sorts p.stem string)
    # NOTE: blocks keys are lexicographically sorted file stems (query.1, query.10,...). We map by qid.
    qids_order = list(blocks.keys())
    predicted_by_qid = {qid: float(qv) for qid, qv in zip(qids_order, res.qerror_per_query)}
    baseline_by_qid = {qid: float(bv) for qid, bv in zip(qids_order, qb)}

    # chosen distinct colsets (dedupe by columns; generic enforces one param per colset)
    chosen_colsets = {}                        # columns -> param
    for ps in chosen_stats:
        chosen_colsets[tuple(ps.columns)] = ps.level

    # predicate columns per query (census)
    def qpred(q):
        m = re.search(r"WHERE\s+(.*)", q.sql)
        if not m:
            return set()
        return {c for c in re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*(?:=|>=|<=|<>|<|>)", m.group(1))}

    queries = {q.qid: q for q in load_benchmark("census")}
    qids_all = sorted(queries.keys(), key=numeric_qid_sort)

    # Load corpus blocks ONCE so we never re-read a file per (colset, query).
    candidx = {qid: {} for qid in qids_all}   # qid -> {(cols_sorted): {param: qerr}}
    for q in qids_all:
        blk = read_query_measure(corpus, q)
        if not blk:
            continue
        slot = blk.get("by_lambda", {}).get(level)
        if not slot:
            continue
        pm = {}
        for cd in slot.get("candidates", []):
            k = tuple(sorted(cd["cols"]))
            pm.setdefault(k, {})[int(cd["param"])] = float(cd["qerror"])
        candidx[q] = pm

    # build ChosenStat list over the chosen colsets
    chosen_list = []
    cols_order = sorted(chosen_colsets.keys(), key=lambda c: tuple(sorted(c)))
    for c_ in cols_order:
        param = chosen_colsets[c_]
        skey = f"|{','.join(sorted(c_))}|P{param}"
        qerd = {}
        for q in qids_all:
            ev = candidx[q].get(tuple(sorted(c_)), {}).get(param)
            if ev is not None:
                qerd[q] = ev
        chosen_list.append(ChosenStat(columns=c_, stat_key=skey, qerr_by_query=qerd))

    # per-query applicable chosen indices (columns subset of its predicate cols)
    applicable = {}
    for q in qids_all:
        pred = qpred(queries[q])
        apps = tuple(i for i, c_ in enumerate(cols_order) if set(c_) <= pred)
        if apps:
            applicable[q] = apps

    print(f"Phase1: chosen={len(cols_order)} distinct colsets "
          f"bytes={res.total_bytes} predicted_mean={res.mean_qerror:.4f}")
    print(f"queries with >=1 applicable chosen colset: {len(applicable)}/468")

    sol = solve_pg_order(chosen_list, baseline_by_qid, applicable)
    order = sol["order"]
    print("Phase2 acyclic:", sol["acyclic"],
          "order len:", len(order), "first:", order[:6])
    print(f"Phase2 realised mean (order-aware, single-winner): {sol['realised_mean']:.4f}")

    json.dump({"level": level, "budget": budget,
               "chosen_colsets": len(cols_order),
               "predicted_mean": res.mean_qerror,
               "realised_mean": sol["realised_mean"],
               "acyclic": sol["acyclic"],
               "order": order,
               "realised_perquery": sol["realised"],
               "baseline_by_qid": baseline_by_qid}, open(out, "w"), indent=1)
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--budget", type=int, default=100000)
    ap.add_argument("--db", default="census_e2e3")
    ap.add_argument("--table", default=".climate")
    ap.add_argument("--out", default=str(ROOT / "results" / "e2e_order_L1_100KB.json"))
    a = ap.parse_args()
    main(str(a.level), a.budget, a.db, a.table, False, Path(a.out))
