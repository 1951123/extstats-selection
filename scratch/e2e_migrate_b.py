"""Experiment (B): boundary-of-benefit migration test.

Deploy the single-workload (stats_ceb_single) chosen extended-stat set onto the
*shared* base tables, then check whether the resulting better single-input
cardinality propagates to better multi-table join *plans* (est cardinality +
join order) on the stats_CEB (145-join) workload.

Causal chain under test:  better single-card (ext stats) -> lower join-input
error -> better join order.  Because single-table ext stats only tighten the
SELECTION filters on base tables (NOT cross-table join selectivity), we expect
the chain to be WEAK; this experiment measures how weak / where it stops.

Protocol (on one fresh clone `stats_jb`):
  1) baseline : default-ANALYZE base tables; EXPLAIN all 145 joins (est vs true)
                + capture plan tree signature (join order).
  2) deploy   : create the Phase-1 stats_ceb_single chosen stats L1@budget on
                their owning tables, in per-table FB order, then ANALYZE.
  3) post     : re-EXPLAIN all 145 joins (est vs true) + plan signature.
Reports join-level card-error before/after and count of join-order changes.
Does NOT time runtimes (order change is a strict prerequisite for any runtime
gain; runtime leg is separate and only worth it if order actually changes).

Usage:
  .venv/bin/python -u scratch/e2e_migrate_b.py --db stats_jb --level 1 --budget 80000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from collections import defaultdict

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extstats2.config import DBConfig, get_backend
from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass
from extstats2.backend.base import StatObject
from extstats2.backend.capabilities import Capacity
from extstats2.bench import load_benchmark, load_benchmark as lb
from extstats2.core.predicates import predicate_columns

BASE_TABLES = ["badges", "comments", "posthistory", "postlinks", "posts", "tags",
               "users", "votes"]


def qtab(q):
    pc = predicate_columns(q)
    return (".%s" % next(iter(pc)).lstrip(".").lower()) if len(pc) == 1 else None


def plan_signature(plan_json):
    """Return a tree-structured signature that captures JOIN ORDER + algorithm":
    pre-order of (NodeType, Relation, JoinCondition-ish tags), with children
    nested in plan order. Two plans differ here ONLY if the join shape/order or
    the per-node algorithm/relation placement differs -> much stricter than the
    flat qerror and immune to pure row-estimate changes."""
    import json as _json

    def strip(n):
        out = {"t": n.get("Node Type"), "r": n.get("Relation Name")}
        cond = n.get("Join Filter") or n.get("Hash Cond") or n.get("Merge Cond")
        # keep join-key tags so re-ordering of the SAME tables is caught even
        # when types coincide
        if cond:
            out["c"] = cond
        ch = n.get("Plans") or []
        if ch:
            out["p"] = [strip(c) for c in ch]
        return out
    return _json.dumps(strip(plan_json[0]["Plan"]))


def compact_plan(plan_json):
    """Serialize a stripped plan TREE (small) for offline structural diffing:
    node type, relation, join condition, per-node Plan Rows."""
    def strip(n):
        o = {"t": n.get("Node Type"), "r": n.get("Relation Name"),
             "rows": n.get("Plan Rows")}
        cond = n.get("Join Filter") or n.get("Hash Cond") or n.get("Merge Cond")
        if cond:
            o["c"] = cond
        ch = n.get("Plans") or []
        if ch:
            o["p"] = [strip(c) for c in ch]
        return o
    return strip(plan_json[0]["Plan"])


def leaves_of(compact):
    """Set of base relations (scans) under a plan node."""
    out = set()
    if compact.get("r"):
        out.add(compact["r"])
    for c in compact.get("p", []) or []:
        out |= leaves_of(c)
    return out


def strip_rows(tree):
    """Row-estimate-free view of a compact plan -> structure/order only."""
    if isinstance(tree, list):
        return [strip_rows(x) for x in tree]
    if not isinstance(tree, dict):
        return tree
    o = {}
    for k, v in tree.items():
        if k in ("rows",):
            continue
        o[k] = strip_rows(v)
    return o


def classify_change(cbase, cdep):
    """Structural classifier between two row-free plan trees.
    Returns one of: 'same' | 'swap_children' | 'join_shape_changed' |
    'algo_scan_change_only'. 'swap_children' is the smoking-gun for a genuine
    join-ORDER flip among the same set of inputs."""
    def norm_jc(c):
        # "which subtree" by its relation leaves (order-insensitive set)
        return tuple(sorted(leaves_of(c))) if c else None
    if cbase == cdep:
        return "same"
    # top-level join shape
    def shape(n):
        return (n.get("t"), n.get("r"), norm_jc(n))
    sb, sd = shape(cbase), shape(cdep)
    if sb[2] is not None and sb[2] == sd[2] and sb[0] == sd[0]:
        # same leaf set & same node type -> recurse into children to localise
        pb = cbase.get("p", []); pd = cdep.get("p", [])
        if len(pb) == len(pd) == 2:
            l0, l1 = pb; r0, r1 = pd
            # if the two children are simply swapped (identical subtrees swapped)
            if l0 == r1 and l1 == r0:
                return "swap_children"
            k0, k1 = classify_change(l0, r0), classify_change(l1, r1)
            if k0 == "same" and k1 == "same":
                return "algo_scan_change_only"
            return "join_shape_changed"
        return "join_shape_changed"
    return "join_shape_changed"


def main(db, level, budget):
    corpus = ROOT / "results" / "per_lambda"
    # ---- shared single-workload chosen set (L/level @ budget) -------------
    meta, blocks = load_lambda_problem(corpus, "stats_ceb_single", "postgres")
    phys, opts, qbs = build_inner_at_level(blocks, str(level), skip_worse_than_baseline=True)
    res = solve_ilp(phys, list(opts), [float(v) for v in qbs], budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, objective="mean")
    chosen = {tuple(ps.columns): ps.level for ps in res.selected_stats}
    Qs = load_benchmark("stats_ceb_single")
    qtabs = {q.qid: qtab(q) for q in Qs}
    qids = list(blocks)
    cs_tab = {}
    for i, qid in enumerate(qids):
        t = qtabs.get(qid)
        for key in res.chosen[i]:
            if "|" in key:
                cs_tab[tuple(sorted(key.split("|")[1].split(",")))] = t
    print(f"single Phase-1 L{level}@{budget}: n_sel={len(chosen)} pred={res.mean_qerror:.4f}")

    be = get_backend("postgres", cfg=DBConfig(host="localhost", port=5432,
                     user="postgres", password="postgres", dbname=db))
    cap_mcv = [c for c in be.supported_capabilities() if c.name == "mcv"][0]
    JQ = load_benchmark("stats_ceb")

    def reset_base_stats():
        with be.conn.cursor() as cur:
            for t in BASE_TABLES:
                cur.execute(f"ANALYZE {t}")

    def explain_all(tag):
        ests, plans = {}, {}
        for q in JQ:
            e = be.estimate(q)
            ests[q.qid] = int(e.estimate) if e.estimate else None
            plans[q.qid] = compact_plan(e.raw)
        json.dump({f"{tag}_est": ests, f"{tag}_plan": plans},
                  open(ROOT / f"results/_migrate_b_{tag}.json", "w"))
        return ests, plans

    def card_err(ests):
        out = {}
        for q in JQ:
            e = ests[q.qid]
            t = q.ground_truth
            out[q.qid] = 2.0 if (t or 0) == 0 else float(
                max((e or 0) / max(t, 1), max(t, 1) / max((e or 1), 1)))
        return out

    # ---- 1) baseline ------------------------------------------------------
    reset_base_stats()
    est0, sig0 = explain_all("base")
    err0 = card_err(est0)
    v0 = np.asarray([err0[q.qid] for q in JQ], float)
    print(f"[baseline] join card-err: mean={v0.mean():.1f} geo="
          f"{np.exp(np.log(v0).mean()):.2f} med={np.median(v0):.1f} "
          f"n>2x={int((v0 > 2).sum())} n>10x={int((v0 > 10).sum())}")

    # ---- 2) deploy chosen stats per owning table (deterministic order) ----
    tab_items = defaultdict(list)
    for cols, p in chosen.items():
        t = cs_tab.get(cols)
        if t is None:
            continue
        nm = t.split(".")[1]  # physical
        tab_items[nm].append((cols, p))
    created = 0
    for nm, items in tab_items.items():
        # clear any leftover (from an earlier run) on this table
        for obj in list(be.list_stats("." + nm)):
            be.drop_stat(obj)
        objs = []
        for i, (cs, p) in enumerate(sorted(items, key=lambda x: x[0])):
            name = f"e2em_{i}_{'_'.join(sorted(cs)).lower()}_p{p}"
            st = StatObject(table="." + nm, columns=tuple(sorted(cs)),
                            capability=cap_mcv, capacity=Capacity(p, "mcv"), name=name)
            try:
                be.create_stat(st)
                objs.append(st)
            except Exception as ex:
                print("  skip", nm, cs, ex)
        for st in objs:
            with be.conn.cursor() as cur:
                cur.execute(f"ALTER STATISTICS {st.name} SET STATISTICS {int(st.capacity.level)}")
        with be.conn.cursor() as cur:
            cur.execute(f"ANALYZE {nm}")
        created += len(objs)
        print(f"deployed table {nm}: {len(objs)} stats ANALYZEd")
    print("total created", created)

    # ---- 3) post-deploy ---------------------------------------------------
    est1, sig1 = explain_all("dep")
    err1 = card_err(est1)
    v1 = np.asarray([err1[q.qid] for q in JQ], float)
    print(f"[deployed ] join card-err: mean={v1.mean():.1f} geo="
          f"{np.exp(np.log(v1).mean()):.2f} med={np.median(v1):.1f} "
          f"n>2x={int((v1 > 2).sum())} n>10x={int((v1 > 10).sum())}")
    # change in card error per query
    improved = 0
    worse = 0
    for i, q in enumerate(JQ):
        if v1[i] < v0[i] - 0.05:
            improved += 1
        elif v1[i] > v0[i] + 0.05:
            worse += 1
    print(f"#join card-err improved >0.05: {improved}; worse: {worse}")

    # ---- structural join-order/structure diff -----------------------------
    # (tree-shaped, algorithm+order+relation placement; immune to row-estimate
    # churn). Classify each structural change.
    from collections import Counter as _Counter
    classes = _Counter()
    swapped = []      # genuine join-ORDER flips of the same input subtrees
    shape_changed = []
    for q in JQ:
        c0, c1 = sig0[q.qid], sig1[q.qid]
        s0, s1 = strip_rows(c0), strip_rows(c1)   # row-free structural views
        if s0 == s1:
            classes["same"] += 1
            continue
        k = classify_change(s0, s1)
        classes[k] += 1
        if k == "swap_children":
            swapped.append(q.qid)
        elif k in ("join_shape_changed", "rel_reassoc"):
            shape_changed.append(q.qid)
    order_changed = [q.qid for q in JQ if strip_rows(sig0[q.qid]) != strip_rows(sig1[q.qid])]
    print(f"#join structural-change: {len(order_changed)}/{len(JQ)} "
          f"by class {dict(classes)}")
    print(f"  smoking-gun join-ORDER flips (same inputs swapped): {len(swapped)} "
          f"{swapped[:8]}")
    print(f"  join shape / relation re-associated: {len(shape_changed)} "
          f"{shape_changed[:10]}")
    for qid in (swapped + shape_changed)[:10]:
        base_rows = sig0[qid].get("rows")
        print(f"    {qid}: base_card_err={err0[qid]:.1f} -> dep_card_err="
              f"{err1[qid]:.1f}")

    out = {
        "db": db, "level": level, "budget_bytes": budget,
        "n_sel_single": created,
        "baseline_card_err": {q.qid: float(err0[q.qid]) for q in JQ},
        "deployed_card_err": {q.qid: float(err1[q.qid]) for q in JQ},
        "order_changed": order_changed,
        "plan_class_changes": dict(classes),
        "join_order_flips": swapped,
        "join_shape_changed": shape_changed,
    }
    Path(ROOT / "results" / f"e2e_migrate_b_L{level}_B{budget}.json").write_text(
        json.dumps(out, indent=1))
    print("wrote results/e2e_migrate_b_L%d_B%d.json" % (level, budget))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="stats_jb")
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--budget", type=int, default=80000)
    a = ap.parse_args()
    main(a.db, a.level, a.budget)
