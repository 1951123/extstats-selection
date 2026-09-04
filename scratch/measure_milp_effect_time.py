"""Measure the phase-2 MILP's EFFECT (baseline vs deployed mean q-error) and its
OWN solve time, on the dense-11-param census corpus.

Corpus : results/per_lambda/census/postgres/  (468 queries, λ 0/1, 11 param grid)
Model  : sparse one-stat (per_query_cap=1) MILP via solve_ilp, per λ, under a
         storage budget. The ONLY pruning applied is v1's inherited
         skip_worse_than_baseline (drop a per-(query,λ,colset,param) option when
         its q-error >= that query's same-λ baseline). No other pruning.

Output : JSON {level, budget_bytes, baseline_mean, deployed_mean, n_selected,
               total_bytes, total_maint, solve_seconds, n_stats, n_options,
               reduced_tail_max, reduced_max_query}
         written to results/milp_effect_time.json (or --out).

Usage:
  .venv/bin/python -u scratch/measure_milp_effect_time.py \
      --level 1 --budgets 5000,20000,50000,100000,200000,400000
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "results" / "per_lambda"
DEFAULT_OUT = ROOT / "results" / "milp_effect_time.json"
REPEATS = 3   # solve repeats per budget: median timing, first result used for metrics


def go(level, budgets, corpus, out, bench="census"):
    meta, blocks = load_lambda_problem(corpus, bench, "postgres")
    # restrict to the λ being tested; build problem once
    phys, opts, qbases = build_inner_at_level(blocks, str(level), skip_worse_than_baseline=True)
    qbases = np.asarray(qbases, dtype=float)
    baseline_mean = float(np.mean(qbases))
    n_opt = sum(len(o) for o in opts)
    print(f"bench={bench} λ L{level}: n_query={len(qbases)} n_phys_stats(full)={len(phys)} "
          f"n_options(post skip_worse)={n_opt} baseline_mean_q={baseline_mean:.4f}")

    rows = []
    for B in budgets:
        # solve up to REPEATS times to stabilise the solve-time reading
        res = None
        t_ms = []
        for _ in range(REPEATS):
            t0 = time.perf_counter()
            res = solve_ilp(phys, opts, list(qbases.tolist()), B,
                            maint_budget=None,
                            optimizer_class=OptimizerClass.SPARSE_LINEAR,
                            per_query_cap=1, objective="mean")
            t_ms.append((time.perf_counter() - t0) * 1000.0)
        dt_ms = float(np.median(t_ms))
        # per-query achieved q-errors (unrepaired queries keep their baseline)
        qq = np.asarray(res.qerror_per_query, dtype=float)
        n_unrepaired = int(np.sum(qq >= np.asarray(qbases) - 1e-9))  # no improvement
        # geometric mean + tail
        geomean = float(np.exp(np.mean(np.log(np.maximum(qq, 1e-12)))))
        row = {
            "level": level, "budget_bytes": B,
            "baseline_mean": baseline_mean,
            "baseline_geomean": float(np.exp(np.mean(np.log(np.maximum(qbases, 1e-12))))),
            "deployed_mean": res.mean_qerror,
            "deployed_geomean": geomean,
            "n_selected": len(res.selected_stats),
            "total_bytes": res.total_bytes,
            "total_maint": res.total_maint,
            "solve_seconds": dt_ms / 1000.0,
            "n_stats_full": len(phys), "n_options": n_opt,
            "n_unrepaired": n_unrepaired,
            "max_per_query_after": float(np.max(qq)) if len(qq) else None,
            "p90_per_query_after": float(np.percentile(qq, 90)) if len(qq) else None,
            "max_baseline": float(np.max(qbases)),
        }
        rows.append(row)
        print(f"  B={B:>8}: mean={res.mean_qerror:.4f} geo={geomean:.4f} "
              f"max={row['max_per_query_after']:.2f} n_sel={row['n_selected']:>3} "
              f"bytes={row['total_bytes']:>7} unrepaired={n_unrepaired:>3} "
              f"solve={dt_ms:7.1f}ms")
    out.write_text(json.dumps({"bench": bench, "level": level, "rows": rows}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--budgets", default="5000,20000,50000,100000,200000,400000")
    ap.add_argument("--bench", default="census")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    budgets = [int(x) for x in a.budgets.split(",") if x.strip()]
    go(a.level, budgets, Path(a.corpus), Path(a.out), a.bench)
