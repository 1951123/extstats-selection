"""Sweep MAINTENANCE budget per λ (census corpus) holding storage fixed, with the
correct per-λ maintenance semantics for a dense-param corpus.

Background / model chosen
-------------------------
A deployment at a given λ ANALYSEs the table ONCE (one shared sampling scan) at
that λ's single-column target => its refresh cost is a per-λ FIXED scan plus a
tiny per-stat payload var:
      fixed[λ0]=0.256s (t=100) , fixed[λ1]=2.56s (t=1000)
      var        = 0.002s/stat (λ0) , 0.02s/stat (λ1)
Total deployed-refresh maintenance for a (λ, chosen-set) = fixed[λ] + Σ_s var_s.

(We deliberately do NOT use MaintProfile's staircase here: build_inner_at_level
stores the *param* in PhysicalStat.level, which is not an abstract capacity tier,
so Y-two-layer's param-as-level indexing is structurally inapplicable. The flat
per-λ fixed + additive var is the faithful model.)

For each maintenance budget M and each λ:
  - if M <= fixed[λ]: no extended stat is maintainable => baseline only.
  - else: additive var cap = M - fixed[λ]  (solve_ilp additive path: Σ var·y <= cap).
Then recombine across λ = min-mean deployment (the deployment "chooses λ+set").

Output JSON: results/milp_maint_time.json (per-level rows + argmin_over_level).
Usage:
  .venv/bin/python -u scratch/measure_milp_maint.py --storage 100000 \\
      --maint 0.1,0.25,0.5,1.0,1.5,2.0,2.5,2.56,2.7,3.0,3.5,4.0
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "results" / "per_lambda"
DEFAULT_OUT = ROOT / "results" / "milp_maint_time.json"

# census climate (from postgres backend maintenance model)
_W = 0.00256
FIXED = {"0": _W * 100, "1": _W * 1000}   # seconds per whole-table shared scan
VAR = {"0": 0.002, "1": 0.02}             # seconds per statistic (arity-2)


def baseline_metrics():
    files = sorted((ROOT / "results" / "per_lambda" / "census" / "postgres").glob("query.*.json"))
    vals = {"0": [], "1": []}
    for f in files:
        d = json.loads(f.read_text())
        for lk in ("0", "1"):
            L = d.get("by_lambda", {}).get(lk)
            if L is not None:
                vals[lk].append(L["baseline"]["qerror"])
    out = {}
    for lk, x in vals.items():
        n = len(x)
        out[lk] = {"mean": sum(x) / n,
                   "geo": float(math.exp(sum(math.log(max(v, 1e-12)) for v in x) / n)),
                   "max": max(x)}
    return out


def go(storage, maint_list, corpus, out):
    meta, blocks = load_lambda_problem(corpus, "census", "postgres")
    base0 = baseline_metrics()
    print(f"storage fixed={storage}B ; sweep maint budget M (s): {maint_list}")
    print(f"fixed L0={FIXED['0']:.3f}s L1={FIXED['1']:.3f}s ; baseline mean "
          f"L0={base0['0']['mean']:.2f} L1={base0['1']['mean']:.2f}")

    curves = {}
    for lv in ("0", "1"):
        phys, opts, qbs = build_inner_at_level(blocks, lv, skip_worse_than_baseline=True)
        qb = [float(v) for v in qbs]
        curves[lv] = {}
        for M in maint_list:
            if M <= FIXED[lv] + 1e-9:
                # fixed alone over budget -> no extended stat maintainable: baseline
                qq = np.asarray([max(q, 1.0) for q in qb], float)
                curves[lv][M] = {"mean": float(qq.mean()),
                                 "geo": float(np.exp(np.log(np.maximum(qq, 1e-12)).mean())),
                                 "max": float(qq.max()), "n_selected": 0,
                                 "solve_s": 0.0}
                continue
            t0 = time.perf_counter()
            res = solve_ilp(phys, opts, qb, storage,
                            maint_budget=float(M - FIXED[lv]),   # additive var cap
                            optimizer_class=OptimizerClass.SPARSE_LINEAR,
                            per_query_cap=1, objective="mean")
            dt = time.perf_counter() - t0
            qq = np.asarray(res.qerror_per_query, float)
            curves[lv][M] = {"mean": res.mean_qerror,
                             "geo": float(np.exp(np.mean(np.log(np.maximum(qq, 1e-12))))),
                             "max": float(np.max(qq)) if len(qq) else None,
                             "n_selected": len(res.selected_stats),
                             "total_maint": float(res.total_maint),
                             "solve_s": dt}
            print(f"  L{lv} M={M:>4}: mean={res.mean_qerror:.4f} "
                  f"n_sel={len(res.selected_stats):>3} maint_var={res.total_maint:.3f}s "
                  f"bytes={res.total_bytes:>7} solve={dt*1000:6.1f}ms")

    # recombine: min-mean deployment across λ for each M
    best = []
    for M in maint_list:
        cand = [("0", curves["0"][M]), ("1", curves["1"][M])]
        lv, r = min(cand, key=lambda x: x[1]["mean"])
        best.append({"M": M, "choose_level": lv,
                     "mean": r["mean"], "geo": r["geo"], "max": r["max"],
                     "n_selected": r["n_selected"]})
    out.write_text(json.dumps({"storage_bytes": storage, "fixed": FIXED,
                               "baseline": base0, "per_level": curves,
                               "argmin_over_level": best}, indent=2))
    print("\nrecombined (choose lower-mean λ):")
    for b in best:
        print(f"  M={b['M']:>4}: chose L{b['choose_level']} mean={b['mean']:.4f} "
              f"geo={b['geo']:.4f} max={b['max']:.2f} n_sel={b['n_selected']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--storage", type=int, default=100000)
    ap.add_argument("--maint", default="0.1,0.25,0.5,1.0,1.5,2.0,2.5,2.56,2.7,3.0,3.5,4.0")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    go(a.storage, [float(x) for x in a.maint.split(",") if x.strip()],
       Path(a.corpus), Path(a.out))
