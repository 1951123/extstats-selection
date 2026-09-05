"""Add the L1 (10%) lambda tier to the existing stats_CEB_single-on-Oracle L0
corpus, per the 2026-09-05 decision (stats_CEB_single = the drill bench measured
at both L0(1%) and L1(10%); census/DMV stay L0-only).

The existing corpus (results/per_lambda/stats_ceb_single/oracle/*.json) only
carries ``by_lambda['0']`` (1%). This driver re-runs each query at level 1
(10%) via the generic Protocol-A ``measure_query_lambda`` and MERGES
``by_lambda['1']`` into the existing file in place (never redoes/replaces L0).

Resumable: a query is skipped if its file already has ``by_lambda['1']``.

Usage::
    python scratch/measure_stcebsi_oracle_l1.py            # full 632
    python scratch/measure_stcebsi_oracle_l1.py --limit 5  # sanity
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import measure_query_lambda
from extstats2.bench import load_benchmark

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")
BENCH = "stats_ceb_single"
NEWLEVEL = 1  # L1 = estimate_percent 10%


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results")
    ap.add_argument("--limit", type=int, default=0, help="0 = all stats_CEB qids")
    args = ap.parse_args()

    from extstats2.core.measure_lambda_io import result_dir
    from extstats2.core.candidates import generate_candidates_per_query

    dest = result_dir(Path(args.out), BENCH, "oracle")
    dest.mkdir(parents=True, exist_ok=True)

    Q = {q.qid: q for q in load_benchmark(BENCH)}
    qids = sorted(Q.keys())
    if args.limit:
        qids = qids[: args.limit]
    # precompute deterministic per-qid candidate map once (dict {qid: [cands]})
    cand_all = generate_candidates_per_query([Q[k] for k in qids], arities=(2,))
    print(f"[oracle-l1] bench={BENCH} to_process={len(qids)} newlevel={NEWLEVEL}"
          f" (est 10%)", flush=True)

    # residual colgroup cleanup on all owner tables first
    be = get_backend("oracle", cfg=OR)

    done, skipped, failed = 0, 0, 0
    t0 = time.time()
    for i, qid in enumerate(qids, 1):
        f = dest / f"{qid}.json"
        if not f.exists():
            print(f"[oracle-l1] {qid} has NO L0 file; skipping (measure L0 first)")
            skipped += 1
            continue
        try:
            blk = json.load(open(f))
        except Exception:
            print(f"[oracle-l1] {qid} unreadable; skip"); skipped += 1; continue
        by = blk.get("by_lambda", {})
        if str(NEWLEVEL) in by:
            skipped += 1
            continue
        q = Q.get(qid)
        if q is None:
            skipped += 1; continue
        cands = cand_all.get(qid, [])
        try:
            newblk = measure_query_lambda(be, q, cands, levels=(NEWLEVEL,),
                                          param_tiers=None, outdir=None)
            blk["by_lambda"][str(NEWLEVEL)] = newblk["by_lambda"][str(NEWLEVEL)]
            with open(f, "w") as fh:
                json.dump(blk, fh)
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            with (dest / f"{qid}.err").open("a") as fh:
                fh.write(f"{time.time()}\t{type(e).__name__}: {e}\n")
            print(f"[oracle-l1] {qid} FAILED {type(e).__name__}: {e}", flush=True)
            continue
        if done % 25 == 0:
            el = time.time() - t0
            print(f"[oracle-l1] {i}/{len(qids)} added={done} skip={skipped} "
                  f"fail={failed} elapsed={el:.0f}s <- {qid}", flush=True)

    el = time.time() - t0
    print(f"[oracle-l1] COMPLETE added={done} skipped={skipped} failed={failed} "
          f"total_elapsed={el:.0f}s ({el/60:.1f}m)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
