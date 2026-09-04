"""Sweep MAINTENANCE budget for the multi-table stats_CEB_single corpus.

Background / model
------------------
stats_CEB_single is a *multi-table* single-sub-plan benchmark: each candidate
query filters exactly ONE base table, so the (only) extended statistics that
can help it live on that same table.  The census maintenance model treated the
whole workload as one table (one shared ANALYZE per λ).  Here the correct model
must be PER ACTIVE TABLE:

  one full refresh at λ level ``lv`` ANALYSEs each *activated* table once, at
  that λ's single-column target ``ladder_target[lv]`` -> a per-table fixed cost

      F_t(lv) = w_per_target * min( ladder_target[lv], reltuples_t/300 )

  (the shared-sampling ramp used by the PG backend), plus a per-selected-stat
  variable payload term (``PhysicalStat.maint_cost``, both read from the corpus).
  Total deployed-refresh maintenance = sum_{t activated} F_t(lv) + sum_s var_s.

Why exact enumeration of activated tables
-----------------------------------------
PhysicalStat.level on this dense-param corpus is the *representation param*
(5..10000), NOT an abstract capacity tier, so the staircase ``MaintProfile``
index-by-level path in `solve_ilp` is structurally inapplicable (same reason as
census).  Because each table's candidate stats are LOCAL to that table, whether
we activate a table is a clean 0/1 choice.  With only 6 candidate tables we
enumerate every activated-subset S exactly:

    for each budget M and λ lv, and each S ⊆ tables with sum_{t in S} F_t <= M:
        var_budget = M - sum_{t in S} F_t      # spare budget for per-stat var
        solve var-only additive ILP (maint_budget=var_budget) restricted to
        queries whose table is in S   (others keep their baseline, counted as
        baseline in the realised workload mean)
    realised(M,lv) = min over S of the resulting full-workload mean q-error.
    The true optimal activated-set is always among the enumerated S (a solution
    that would activate only part of an S is reachable under that smaller set),
    so taking the min keeps the curve exact.

Output JSON: results/milp_maint_time_stceb_single_L{lv}.json or a combined writer.
Usage:
  .venv/bin/python -u scratch/measure_milp_maint_stceb.py \
      --storage 100000 --level 1 --maint 0.1,0.5,1.0,2.0,4.0,6.0,8.0
"""
from __future__ import annotations

import argparse
import json
import math
import time
from itertools import combinations
from pathlib import Path

import numpy as np

from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass
from extstats2.bench import load_benchmark
from extstats2.core.predicates import predicate_columns

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "results" / "per_lambda"
WORKLOAD = "stats_ceb_single"
OUT = ROOT / "results" / "milp_maint_time_stceb_single_L1.json"

_W = 0.00256                    # s per statistics_target unit (shared-sampling ramp)
LADDER_TARGET = {0: 100, 1: 1000, 2: 10000}   # single-column target per λ level
# pg_class.reltuples for the six candidate tables of stats_CEB_single (from the
# `stats` DB, 2026-09-04). Only these have candidate-bearing queries.
RELTUPLES = {".users": 40325, ".comments": 174305, ".votes": 328064,
             ".posts": 91976, ".posthistory": 303187, ".postlinks": 11102}
CANDIDATE_TABLES = sorted(RELTUPLES.keys())


def fixed_cost(table: str, level: int) -> float:
    """One-refresh per-table fixed ANALYZE cost (s) at λ ``level``."""
    rel = RELTUPLES[table]
    t_sat = rel / 300.0
    return _W * min(LADDER_TARGET[level], t_sat)


def qtab(q):
    pcs = predicate_columns(q)
    return (".%s" % next(iter(pcs)).lstrip(".").lower()) if len(pcs) == 1 else None


def geo(vals):
    a = np.asarray([max(float(v), 1e-12) for v in vals], float)
    return float(np.exp(np.mean(np.log(a))))


def go(level, storage, maint_list, out):
    lv = str(level)
    meta, blocks = load_lambda_problem(CORPUS, WORKLOAD, "postgres")
    phys, opts, qbs = build_inner_at_level(blocks, lv, skip_worse_than_baseline=True)
    Q = load_benchmark(WORKLOAD)
    qt = {q.qid: qtab(q) for q in Q}
    base_by_q = {qid: float(b) for qid, b in zip(blocks, qbs)}
    n_full = len(qbs)
    base_full = np.asarray(list(base_by_q.values()), float)
    base_mean = float(base_full.mean())

    # table of each option's query; validate single-table assumption
    opt_tab = []
    bad = 0
    for grp in opts:
        for o in grp:
            t = qt.get(o.query)
            if t is None:
                bad += 1
            opt_tab.append(t)
    assert bad == 0, f"{bad} options on unresolved/non-single-table query"

    # group option-groups by query (order aligned with qbs)
    qids = list(blocks)
    q_table = [qt[q] for q in qids]

    print(f"bench={WORKLOAD} λ L{level}: n_query(candidate)={n_full} "
          f"n_phys={len(phys)} baseline_mean={base_mean:.4f}")
    print(f"fixed per table at L{level}: " +
          " ".join(f"{t}~{fixed_cost(t, level):.4f}" for t in CANDIDATE_TABLES))

    # ---------------------------------------------------------------- sweep
    rows = []
    for M in maint_list:
        best = None           # (mean, subset, n_sel, var)
        # iterate all subsets of candidate tables; 2^6 = 64 per (M, λ).
        tables = CANDIDATE_TABLES
        for r in range(len(tables) + 1):
            for S in combinations(tables, r):
                fS = sum(fixed_cost(t, level) for t in S)
                if fS > M + 1e-9:
                    continue
                var_budget = M - fS
                # restrict to queries whose table is in S
                keep = [i for i, t in enumerate(q_table) if t in S]
                sub_opts = [opts[i] for i in keep]
                if not sub_opts:
                    # only baseline tables selected: no solve needed
                    real_mean = base_mean
                    if best is None or real_mean < best[0] - 1e-12:
                        best = (real_mean, tuple(sorted(S)), 0, 0.0)
                    continue
                t0 = time.perf_counter()
                res = solve_ilp(phys, sub_opts, [float(qbs[i]) for i in keep],
                                storage, maint_budget=var_budget,
                                optimizer_class=OptimizerClass.SPARSE_LINEAR,
                                per_query_cap=1, objective="mean")
                dt = time.perf_counter() - t0
                # realised full-workload mean: solved queries take res qerrors,
                # non-included queries take their baseline
                achieved = dict(zip([qids[i] for i in keep], res.qerror_per_query))
                tot = 0.0
                for q in qids:
                    tot += achieved.get(q, base_by_q[q])
                real_mean = tot / n_full
                # n_sel/var over included only
                n_sel = len(res.selected_stats)
                var = float(res.total_maint) if res.total_maint is not None else 0.0
                if best is None or real_mean < best[0] - 1e-12:
                    best = (real_mean, tuple(sorted(S)), n_sel, var)
        # rows wants best mean etc
        rows.append({
            "M": M, "level": level, "choose_tables": list(best[1]) if best else [],
            "n_tables_active": len(best[1]) if best else 0,
            "mean": best[0] if best else base_mean,
            "n_selected": best[2] if best else 0,
            "var_used": best[3] if best else 0.0,
            "baseline_mean": base_mean,
        })
        print(f"  M={M:>6.3f}: mean={rows[-1]['mean']:.4f} "
              f"tables={rows[-1]['n_tables_active']} sel={rows[-1]['n_selected']} "
              f"choose={rows[-1]['choose_tables']} var={rows[-1]['var_used']:.3f}")
    out.write_text(json.dumps({
        "level": level, "storage_bytes": storage, "baseline_mean": base_mean,
        "fixed_per_table_L": {t: fixed_cost(t, level) for t in CANDIDATE_TABLES},
        "rows": rows}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--storage", type=int, default=100000)
    ap.add_argument("--maint", default="0.05,0.1,0.2,0.3,0.4,0.5,0.8,1.0,1.5,2.0,3.0,4.0,6.0,8.0")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    maint = [float(x) for x in a.maint.split(",") if x.strip()]
    go(a.level, a.storage, maint, Path(a.out))
