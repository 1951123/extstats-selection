"""Refresh a result dir's _meta.json with the full per-owner-table S-grid map.

The S-grid measure driver (scratch/measure_sgrid.py) writes a single ref-table
`tiers` line. For a multi-table bench (stats_CEB_single) the S-grid tier's
realized S differs per owner table (each owner's N decides {full}/{30000,full}/
{30000,300000}), so the ref-table summary alone is misleading. This refresher
rewrites _meta.json for an existing (bench, backend) corpus, adding:
  extra.owner_tables   : sorted owner tables actually present in the measured files
  extra.table_s_rows   : {owner_table: {level: {S_rows,single_target,estimate_percent}}}
keeping `tiers` = the S-grid summary for the chosen representative ref table.

Read-only against the DB (only NUM_ROWS-lookups via the backend) — safe to run
while a measure job writes *query* files into the same dir (we only rewrite the
meta file atomically).

Usage::
    python scratch/refresh_sgrid_meta.py --bench stats_ceb_single --backend oracle
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_io import result_dir

_PG = dict(host="localhost", port=5432, user="postgres", password="postgres")
_OR = dict(host="localhost", port=1521, user="SYSTEM", password="lxf82073077",
           service="FREEPDB1")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True,
                    choices=["census", "stats_ceb_single", "dmv"])
    ap.add_argument("--backend", required=True, choices=["postgres", "oracle"])
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    pgdb = {"census": "census", "dmv": "dmv", "stats_ceb_single": "stats"}[args.bench]
    if args.backend == "postgres":
        cfg = DBConfig(**{**_PG, "dbname": pgdb})
    else:
        cfg = DBConfig(**_OR)
    be = get_backend(args.backend, cfg=cfg)

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure_io import Meta, SampleTier, write_meta

    dest = result_dir(Path(args.out), args.bench, args.backend)
    if not dest.exists():
        print(f"no corpus dir {dest}"); sys.exit(1)

    Q = {q.qid: q for q in load_benchmark(args.bench)}
    qids = sorted(Q.keys())
    cand_all = generate_candidates_per_query([Q[k] for k in qids], arities=(2,))
    # owner tables actually present in on-disk measured json files
    present = sorted(
        {cands[0].table for qid, cands in cand_all.items()
         if cands and (dest / f"{qid}.json").exists()})
    # fall back to all bench owner tables if none measured yet (e.g. job pending)
    owner_tables = present or sorted(
        {cands[0].table for cands in cand_all.values() if cands})

    levels = [0, 1]   # S-grid levels
    def _tier(tbl, lv):
        return {"S_rows": be.sample_rows_at_level(tbl, lv),
                "single_target": be.single_col_target_for_level(tbl, lv),
                "estimate_percent": be.sample_percent_at_level(tbl, lv)}

    # tiers: lightweight declaration of which lambda levels exist (downstream
    # reads only tiers[].level); the real per-owner-table S is in table_s_rows.
    tiers = [SampleTier(level=lv, S_rows=None, single_target=None,
                        estimate_percent=None) for lv in levels]
    table_s_rows = {t: {str(lv): _tier(t, lv) for lv in levels}
                    for t in owner_tables}

    write_meta(dest, Meta(bench=args.bench, backend=args.backend, tiers=tiers,
                          param_tiers=tuple(be.representation_param_tiers()),
                          extra={"method": "S-grid global S_rows [30000,300000]; "
                                           "per-owner-table realized S in table_s_rows",
                                 "levels": levels,
                                 "owner_tables": owner_tables,
                                 "table_s_rows": table_s_rows}))
    print(f"refreshed {dest}/_meta.json (owner_tables={owner_tables})")
    print(json.dumps(table_s_rows, indent=1))


if __name__ == "__main__":
    main()
