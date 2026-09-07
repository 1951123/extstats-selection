"""End-to-end Oracle "optimize -> deploy" round-trip (basic-flow gate).

Shows that the *other* stages of the pipeline (selection/deployment), not just
Protocol-A measurement, run on Oracle — using measurement that already exists
(the small ``census_mini/oracle`` 3-query corpus: q.184/61/62), so no re-measure.

Flow (models e2e_deploy_census.py but for Oracle's Protocol-A / column-group
deployment, no catalog-mask, no OID/FB ordering — those are PG-only):
  1. Load the existing Oracle per-λ corpus ``results/measure/census_mini/oracle``
     and build the INNER selection problem at a λ level (skip_worse_than_baseline
     only) -> solve under a storage budget -> ``selected_stats`` (colset at its
     chosen representation param) + per-query predicted q-error (model value).
  2. On the real Oracle CLIMATE table: restore the natural no-ext single-column
     λ-state, then physically build each selected column group at its chosen
     param (Protocol-A: one GATHER per group, the honest Oracle deploy) — no
     masking; every chosen stat is ON (true deploy).
  3. EXPLAIN each census query in that deployed state -> TRUE q-error.
  4. Compare predicted vs true (mean / geomean / max per-query).

Usage::
    .venv/bin/python -u scratch/e2e_deploy_oracle.py --level 0 --budget 8000 \\
        --out results/e2e_deploy_oracle.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "results" / "measure"
WORKLOAD = "census_mini"      # existing Oracle-measured 3-query corpus
BACKEND = "oracle"
OR = dict(host="localhost", port=1521, user="SYSTEM",
          password="lxf82073077", service="FREEPDB1")


def metrics(qerrs):
    q = np.asarray([max(float(v), 1.0) for v in qerrs], dtype=float)
    if q.size == 0:
        return {"mean": float("nan"), "geomean": float("nan"), "max": float("nan")}
    return {"mean": float(q.mean()),
            "geomean": float(np.exp(np.log(q).mean())),
            "max": float(q.max())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=0)
    ap.add_argument("--budget", type=int, default=8000)
    ap.add_argument("--bench", default="census")
    ap.add_argument("--out", default=str(ROOT / "results" / "e2e_deploy_oracle.json"))
    a = ap.parse_args()
    level = str(a.level)

    from extstats2.core.optimize_sgrid import (load_sgrid_problem,
                                                build_inner_at_level)
    from extstats2.core.optimize import solve_ilp, OptimizerClass
    from extstats2.config import DBConfig, get_backend
    from extstats2.backend.base import StatObject
    from extstats2.backend.capabilities import SamplingLevel

    # ---- 1) build + solve over the existing Oracle-measured corpus ----------
    meta, blocks = load_sgrid_problem(CORPUS, WORKLOAD, BACKEND)
    print(f"[oracle-e2e] corpus={WORKLOAD}/{BACKEND} queries={len(blocks)} "
          f"levels_in_meta={[t.level for t in (meta.tiers if meta else [])]}")
    phys, opts, qbases = build_inner_at_level(blocks, level,
                                              skip_worse_than_baseline=True)
    if not opts:
        print("[oracle-e2e] no solvable queries at this level; abort")
        return
    qb = [float(v) for v in qbases]
    res = solve_ilp(phys, opts, qb, a.budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1)
    # per-query predicted in build_inner qid order = blocks insertion order
    qids_solve = list(blocks.keys())
    pred = {q: float(qv) for q, qv in zip(qids_solve, res.qerror_per_query)}
    base = {q: float(qv) for q, qv in zip(qids_solve, qb)}
    print(f"[oracle-e2e] pred mean={res.mean_qerror:.4f} "
          f"n_selected={len(res.selected_stats)} bytes={res.total_bytes}B "
          f"budget={a.budget}B")
    sel = [(tuple(ps.columns), int(ps.level)) for ps in res.selected_stats]
    print(f"[oracle-e2e] selected (colset@param): {sel}")

    # ---- 2) deploy on real Oracle CLIMATE -----------------------------------
    from extstats2.bench import load_benchmark
    be = get_backend("oracle", cfg=DBConfig(**OR))
    table = ".climate"
    mcv = [c for c in be.supported_capabilities() if c.name == "mcv"][0]
    # clean slate: a fresh natural no-ext λ-state (as the corpus baselines were
    # measured) so deployed comparisons are fair.
    be.restore_natural_stats(table, estimate_percent=100.0)
    for s in list(be.list_stats(table)):
        be.drop_stat(s)
    # enter λ-state at the chosen level (sampling depth) so deploy matches how
    # the corpus was measured at this λ.
    ep = be.sample_percent_at_level(table, a.level)
    be.restore_natural_stats(table, estimate_percent=float(ep))
    for s in list(be.list_stats(table)):
        be.drop_stat(s)
    print(f"[oracle-e2e] λ L{a.level} (est_percent={ep}) natural state ready")

    # DEPLOY the whole selected SET as ONE combined column-group GATHER
    # (single analytic scan building every chosen group at its own param).
    # CRITICAL (2026-09-04): do NOT deploy stat-by-stat with N sequential
    # GATHERs — each re-gathers FOR ALL COLUMNS SIZE AUTO and the accumulated
    # re-sampling drives estimates back to baseline. One combined method_opt
    # with all `FOR COLUMNS (...) SIZE <param>` parts reproduces the model.
    tname = be._q_table(table)          # upper, e.g. CLIMATE
    parts = ["FOR ALL COLUMNS SIZE AUTO"]
    for cols, p in sel:
        grp = "(" + ",".join('"%s"' % c.upper() for c in cols) + ")"
        parts.append(f"FOR COLUMNS {grp} SIZE {int(p)}")
    method_opt = " ".join(parts)
    ep = be.sample_percent_at_level(table, a.level)
    cur = be.conn.cursor()
    cur.execute(
        "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
        f"ownname=>'{be._owner}', tabname=>'{tname}', "
        "method_opt=>:m, estimate_percent=>:ep, degree=>1); END;",
        {"m": method_opt, "ep": float(ep)})
    live = [s.columns for s in be.list_stats(table)]
    print(f"[oracle-e2e] deployed {len(sel)} group(s) in ONE combined GATHER "
          f"@ ep={ep}%; live groups={len(live)} params="
          f"{sorted({p for _, p in sel})}")

    # ---- 3) EXPLAIN every query in the deployed state -> TRUE q-error ------
    queries = load_benchmark(a.bench)
    rows = []
    for q in queries:
        qid = q.qid
        estv = float(be.estimate(q).estimate or float("nan"))
        truth = float(q.ground_truth) if q.ground_truth else float("nan")
        qe = (max(estv / truth, truth / estv)
              if estv == estv and truth == truth else float("nan"))
        rows.append({"qid": qid, "truth": truth, "est_deployed": estv,
                     "true_qerr": qe,
                     "predicted_qerr": pred.get(qid),
                     "baseline_qerr": base.get(qid)})

    haspred = [r for r in rows
               if r["predicted_qerr"] is not None
               and r["true_qerr"] is not None
               and r["predicted_qerr"] == r["predicted_qerr"]
               and r["true_qerr"] == r["true_qerr"]]
    result = {
        "workload": WORKLOAD, "backend": BACKEND, "level": a.level,
        "budget_bytes": a.budget,
        "n_selected": len(res.selected_stats), "total_bytes": res.total_bytes,
        "selected": [{"cols": list(c), "param": p} for c, p in sel],
        "predicted_metrics": metrics([r["predicted_qerr"] for r in haspred]),
        "true_metrics": metrics([r["true_qerr"] for r in haspred]),
        "n_queries_compared": len(haspred),
        "rows": rows,
    }
    Path(a.out).write_text(json.dumps(result, indent=2))
    pm, tm = result["predicted_metrics"], result["true_metrics"]
    print("\n=== PREDICTED vs TRUE on deployed Oracle set ===")
    for k in ("mean", "geomean", "max"):
        print(f"  {k:8s}: predicted={pm[k]:.4f}  true={tm[k]:.4f}  "
              f"true/pred={tm[k]/pm[k]:.3f}")
    print(f"compared {len(haspred)} queries ; wrote {a.out}")


if __name__ == "__main__":
    main()
