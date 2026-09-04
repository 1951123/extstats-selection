"""Full Oracle Census L0 per-λ measurement (Protocol-A).

Reusable driver over the generic Protocol-A ``measure_query_lambda`` on the
Oracle column-group backend.  Measures every census query at λ-level 0 only
(L0 = Oracle ``estimate_percent 1``, the cheap-tier shared GATHER scan), for all
arity-2 column-set candidates.

Cost model / timing
-------------------
Oracle has no Protocol-M (``has_protocol_m()==False``), so each candidate is an
independent column-group GATHER on the shared CLIMATE table (~2.46M rows).  The
generic driver drops the previous group then builds the next one, so exactly one
group is live per measurement (clean Protocol-A).  L0's representation cap
(``S_rows/300`` ≈ 82) admits only the SIZE-64 param from Oracle's grid
``{64,254}``, so every candidate costs ~1 GATHER (~0.6 s).  Measured:
~ 9 998 candidate slots across 468 queries  ->  ~1.8 h serial wall-clock.

Resumable: a query is skipped if ``results/per_lambda/census/oracle/<qid>.json``
already exists, so an interrupted run simply re-launches to continue.

Usage::
    python scratch/measure_census_oracle_l0.py            # full 468, L0
    python scratch/measure_census_oracle_l0.py --limit 5  # first 5 (sanity)
    python scratch/measure_census_oracle_l0.py --qid query.184  # one query
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import measure_query_lambda
from extstats2.core.measure_lambda_io import (LambdaTier, Meta, result_dir,
                                              write_meta)

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")

LEVELS = (0,)  # L0 only (fast phase). L1/L2 dropped for now.


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="census")
    ap.add_argument("--out", default="results")
    ap.add_argument("--table", default=".climate")
    ap.add_argument("--limit", type=int, default=0, help="0 = all; else first N")
    ap.add_argument("--qid", default=None, help="measure a single qid")
    args = ap.parse_args()

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query

    be = get_backend("oracle", cfg=OR)
    # safety: leave no leftover column group from a prior probe
    for s in list(be.list_stats(args.table)):
        be.drop_stat(s)

    bench = args.bench
    Q = {q.qid: q for q in load_benchmark(bench)}
    if args.qid is not None:
        qids = [args.qid] if args.qid in Q else []
    else:
        qids = sorted(Q.keys())
        if args.limit:
            qids = qids[: args.limit]
    print(f"[oracle-l0] benchmark={bench} queries_to_measure={len(qids)} "
          f"levels={LEVELS}", flush=True)

    dest = result_dir(Path(args.out), bench, "oracle")
    dest.mkdir(parents=True, exist_ok=True)

    # precompute full candidate map once (deterministic order)
    cand_all = generate_candidates_per_query([Q[k] for k in qids], arities=(2,))

    done, skipped, failed = 0, 0, 0
    t_start = time.time()
    for i, qid in enumerate(qids, 1):
        out_f = dest / f"{qid}.json"
        if out_f.exists():
            skipped += 1
            continue
        q = Q[qid]
        cands = cand_all.get(qid, [])
        try:
            measure_query_lambda(be, q, cands, levels=LEVELS,
                                 param_tiers=None, outdir=dest)
            done += 1
        except Exception as e:  # noqa: BLE001 - keep the run alive
            failed += 1
            with (dest / f"{qid}.err").open("a") as f:
                f.write(f"{time.time()}\t{type(e).__name__}: {e}\n")
            print(f"[oracle-l0] {qid} FAILED {type(e).__name__}: {e}",
                  flush=True)
            continue
        if done % 10 == 0 or done == 1:
            el = time.time() - t_start
            print(f"[oracle-l0] {i}/{len(qids)} done={done} skip={skipped} "
                  f"fail={failed} elapsed={el:.0f}s ({el/60:.1f}m) <- {qid}",
                  flush=True)

    # ---- write _meta.json (L0-only tier + offered param grid) ------------
    meta_tiers = []
    for level in LEVELS:
        sr = be.lambda_sampling_rows(args.table, level)
        ep = float(be.lambda_sampling_percent(args.table, level))
        meta_tiers.append(LambdaTier(level=level, S_rows=sr, single_target=None,
                                     estimate_percent=ep))
    pgrid = tuple(be.representation_param_tiers(args.table))
    write_meta(dest, Meta(bench=bench, backend="oracle", tiers=meta_tiers,
                          param_tiers=pgrid,
                          extra={"note": "full census L0 (Protocol-A, "
                                          "colgroup); only param<=S/300 "
                                          "offered per level"}))
    el = time.time() - t_start
    print(f"[oracle-l0] COMPLETE done={done} skipped={skipped} "
          f"failed={failed} total_elapsed={el:.0f}s ({el/60:.1f}m)", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
