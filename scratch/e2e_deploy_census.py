"""End-to-end deployment validation for v2 (PG census).

Closes O3: does the real planner's per-query q-error after physically building the
ILP-selected (colset, param) set match the model's prediction that the optimizer
optimised over?

Flow (on a fresh mirror DB clone of census so the canonical DB is untouched):
  1. Load dense-11 census corpus; build the L1 inner problem (skip_worse_than_baseline,
     the ONLY pruning) and solve under a storage budget → get `selected_stats`
     (physical (colset, param)) and predicted per-query q-error (model value).
  2. On the mirror, enter λ=level state, DROP any leftover stats, CREATE + build all
     selected statistics at their params, (one shared ANALYZE).  No masking: every
     chosen stat is ON simultaneously (true deploy).
  3. EXPLAIN every query in that deployed state → TRUE per-query q-error.
  4. Compare: true vs predicted (mean / geomean / max / per-query scatter).  Any gap
     is planner interference / model-truth divergence (the whole point of E2E).

Usage:
  .venv/bin/python -u scratch/e2e_deploy_census.py --level 1 --budget 100000 \\
      --db census_e2e --table .climate
Output: results/e2e_deploy.json
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "results" / "measure"
DEFAULT_OUT = ROOT / "results" / "e2e_deploy.json"


def metrics(qerrs):
    q = np.asarray([max(float(v), 1.0) for v in qerrs], dtype=float)
    return {"mean": float(q.mean()),
            "geomean": float(np.exp(np.log(q).mean())),
            "max": float(q.max())}


def build_and_solve(level: str, budget: int, disjoint: bool = False):
    from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
    from extstats2.core.optimize import solve_ilp, OptimizerClass
    meta, blocks = load_lambda_problem(CORPUS, "census", "postgres")
    phys, opts, qbases = build_inner_at_level(blocks, level, skip_worse_than_baseline=True)
    qb = [float(v) for v in qbases]
    res = solve_ilp(phys, opts, qb, budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, global_disjoint=disjoint)
    return blocks, res, phys, qbases


def go(level: str, budget: int, db: str, table: str, limit: int, disjoint: bool, out: Path) -> None:
    from extstats2.config import DBConfig, get_backend
    from extstats2.backend.base import StatObject
    from extstats2.backend.capabilities import Capacity

    blocks, res, phys, qbases = build_and_solve(level, budget, disjoint=disjoint)
    # per-query predicted values (= build_inner's qid order = blocks insertion order)
    qids_solve = list(blocks.keys())
    pred_by_qid = {qid: float(qv) for qid, qv in zip(qids_solve, res.qerror_per_query)}
    base_by_qid = {qid: float(qv) for qid, qv in zip(qids_solve, qbases)}

    print(f"predicted(disjoint={disjoint}): mean={res.mean_qerror:.4f} "
          f"n_selected={len(res.selected_stats)} bytes={res.total_bytes}B")

    be = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres", dbname=db))
    cap_obj = [c for c in be.supported_capabilities() if c.name == "mcv"][0]
    be.enter_lambda_state(table, int(level))

    for s in list(be.list_stats(table)):
        be.drop_stat(s)

    # build every selected stat at its chosen param (single shared ANALYZE), no mask
    objs_params = []
    for ps in res.selected_stats:
        cols_t = "_".join(c.lower() for c in ps.columns)
        name = f"e2e_{level}_{cols_t}_p{ps.level}"
        obj = StatObject(table=table, columns=tuple(ps.columns),
                         capability=cap_obj, capacity=Capacity(ps.level, "e2e"),
                         name=name)
        be.create_stat(obj)
        objs_params.append((obj, ps.level))
    be.build_stat_params_batch(objs_params)
    print(f"deployed {len(objs_params)} stats @ λ L{level} (shared ANALYZE done)")

    # EXPLAIN every query in the deployed state -> TRUE q-error
    from extstats2.bench import load_benchmark
    queries = load_benchmark("census")
    # confirm these query qids correspond to the ones we solved
    solved_qids = set(pred_by_qid)
    if limit:
        queries = queries[:limit]

    rows = []
    for q in queries:
        qid = q.qid
        est = be.estimate(q)
        estv = float(est.estimate) if est.estimate is not None else float("nan")
        truth = float(q.ground_truth) if q.ground_truth else float("nan")
        if estv == estv and truth == truth:
            qe = max(estv / truth, truth / estv)
        else:
            qe = float("nan")
        rows.append({
            "qid": qid, "truth": truth, "est_deployed": estv,
            "true_qerr": qe,
            "predicted_qerr": pred_by_qid.get(qid),
            "baseline_qerr": base_by_qid.get(qid),
        })

    haspred = [r for r in rows
               if r["predicted_qerr"] is not None
               and r["predicted_qerr"] == r["predicted_qerr"]
               and r["true_qerr"] == r["true_qerr"]]
    result = {
        "level": level, "budget_bytes": budget, "db": db, "table": table,
        "n_selected": len(res.selected_stats), "total_bytes": res.total_bytes,
        "predicted_metrics": metrics([r["predicted_qerr"] for r in haspred]),
        "true_metrics": metrics([r["true_qerr"] for r in haspred]),
        "n_queries_compared": len(haspred),
        "rows": rows,
    }
    out.write_text(json.dumps(result, indent=2))
    pm = result["predicted_metrics"]; tm = result["true_metrics"]
    print("\n=== PREDICTED vs TRUE (deployed set) ===")
    for k in ("mean", "geomean", "max"):
        print(f"  {k:8s}: predicted={pm[k]:.4f}  true={tm[k]:.4f}  "
              f"true/pred={tm[k]/pm[k]:.3f}")
    print(f"compared {len(haspred)} queries ; wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--budget", type=int, default=100000)
    ap.add_argument("--db", default="census_e2e")
    ap.add_argument("--table", default=".climate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--disjoint", action="store_true", help="Option-A: forbid any shared column between chosen stats")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    go(str(a.level), a.budget, a.db, a.table, a.limit, a.disjoint, Path(a.out))
