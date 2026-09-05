"""A/B: Oracle AUTO_SAMPLE_SIZE (full-scan) vs our 1% ladder-L0, per query.

The 1% corpus (results/per_lambda/census/oracle/*.json) measured each candidate
at estimate_percent=1. Probe above showed AUTO_SAMPLE_SIZE => 100% (full scan)
on Oracle for these tables, and that 1% is a *smaller* effective sample on the
big CLIMATE table (24.7K rows, no colgroup histogram for CASEID,DAGE) while
AUTO gives the full 2.46M + real HYBRID 254-bucket colgroup histograms.

This probe re-measures a handful of representative census qids with an
OracleBackend whose ladder maps L0 -> estimate_percent=AUTO (0), so baseline
(natural stats) AND the colgroup candidates are all full-scan/AUTO. It then
compares each qid's AUTO baseline+best against the recorded 1% corpus values,
to expose whether delegating Oracle how-much to AUTO changes the headline
(baseline AND repair) vs our shallow 1%.

Restores tables to clean AUTO natural stats at the end. Writes to
scratch/out_auto_probe/ (NOT the real results tree).

Usage:  python scratch/probe_oracle_auto_measure.py [--qid query.184] [--ep 0|1|10|100]
        --ep 0   -> Oracle AUTO_SAMPLE_SIZE (full scan)   [default for this script]
        --ep 10  -> 10% estimate_percent tier (the missing middle grid point)
        --ep 1   -> 1% tier (matches existing corpus L0)
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import measure_query_lambda
from extstats2.bench import load_benchmark
from extstats2.core.candidates import generate_candidates_per_query

OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
              password="lxf82073077", service="FREEPDB1")

TABLE = ".climate"

REP = ["query.184", "query.61", "query.62", "query.59", "query.465"]

def ladder_for(ep: float) -> dict:
    """Ladder mapping every level to the same single estimate_percent (one-point grid)."""
    return {0: {"estimate_percent": ep, "buckets": 254},
            1: {"estimate_percent": ep, "buckets": 254},
            2: {"estimate_percent": ep, "buckets": 254}}


def load_1pct(qid):
    f = Path("results/per_lambda/census/oracle") / (qid + ".json")
    if not f.exists():
        return None
    d = json.load(open(f))
    bl = d["by_lambda"]["0"]["baseline"]["qerror"]
    cands = d["by_lambda"]["0"]["candidates"]
    best = min((c["qerror"] for c in cands
                if c["qerror"] == c["qerror"]), default=float("nan"))
    return bl, best


def summarize(block):
    bl = block["by_lambda"]["0"]["baseline"]["qerror"]
    cands = block["by_lambda"]["0"]["candidates"]
    best = min((c["qerror"] for c in cands
                if c["qerror"] == c["qerror"]), default=float("nan"))
    return bl, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qid", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ep", type=float, default=0.0,
                    help="estimate_percent to measure as the single tier "
                         "(0 = AUTO/full; 1/10/100 = manual %). Default 0 (AUTO).")
    args = ap.parse_args()
    ep = args.ep
    ep_label = "AUTO" if ep == 0 else f"{ep:g}%"

    Q = {q.qid: q for q in load_benchmark("census")}
    if args.qid:
        qids = [args.qid] if args.qid in Q else []
    else:
        qids = [q for q in REP if q in Q][: args.limit] if args.limit else \
               [q for q in REP if q in Q]
    print(f"qids: {qids}  ep={ep_label}", flush=True)
    if not qids:
        print("no qids"); return

    be = get_backend("oracle", cfg=OR)
    be._ladder = ladder_for(ep)

    # clean slate
    for s in list(be.list_stats(TABLE)):
        be.drop_stat(s)

    cand_all = generate_candidates_per_query([Q[k] for k in qids], arities=(2,))
    out = Path(f"scratch/out_auto_probe/{ep_label}")
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'qid':<11}{'1% base':>10}{ep_label+' base':>12}  "
          f"{'1% best':>9}{ep_label+' best':>12}  time")
    print("-" * 70)
    for i, qid in enumerate(qids, 1):
        q = Q[qid]
        cands = cand_all.get(qid, [])
        t0 = time.time()
        try:
            block = measure_query_lambda(be, q, cands, levels=(0,),
                                         param_tiers=None, outdir=out)
        except Exception as e:  # noqa: BLE001
            print(f"{qid:<11}  {ep_label} measure FAILED: {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        abl, abest = summarize(block)
        p = load_1pct(qid)
        if p is None:
            p1bl, p1best = float("nan"), float("nan")
        else:
            p1bl, p1best = p
        print(f"{qid:<11}{p1bl:>10.2f}{abl:>12.2f}  "
              f"{p1best:>9.2f}{abest:>12.2f}  {dt:5.0f}s", flush=True)

    # leave tables clean (full AUTO natural, no groups) -> enter_lambda_state in the
    # AUTO measure already produced natural AUTO stats as the LAST state for each q,
    # but candidate groups were dropped per-candidate; drop any leftover + natural.
    for s in list(be.list_stats(TABLE)):
        be.drop_stat(s)
    be.restore_natural_stats(TABLE, estimate_percent=0)
    print("\n[cleanup] tables restored to AUTO natural stats.")


if __name__ == "__main__":
    main()
