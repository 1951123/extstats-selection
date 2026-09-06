"""Run the PG maintenance-parameter fit and write _maint.json per bench corpus.

Usage:  python scratch/fit_maint_postgres.py [--benches census dmv stats_ceb_single]
        [--out results] [--k 8] [--repeats 3]
Reads owner tables + levels from each bench's results/measure/{bench}/postgres/_meta.json,
times fixed/c_var on the live PG engine, and writes _maint.json. PG DB per bench:
census->census, dmv->dmv, stats_ceb_single->stats.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.maint_fit import fit_pg_bench
from extstats2.core.measure_lambda_io import result_dir, read_meta

# (bench -> pg database holding its table(s))
_BENCH_DB = {"census": "census", "dmv": "dmv", "stats_ceb_single": "stats"}


def _corpus_meta(outdir, bench, backend="postgres"):
    d = result_dir(Path(outdir), bench, backend)
    m = read_meta(d)
    return d, m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benches", nargs="+", default=["census", "dmv", "stats_ceb_single"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--db", default=None, help="override pg db for all benches")
    args = ap.parse_args()

    for bench in args.benches:
        corpus_dir, meta = _corpus_meta(args.out, bench)
        if meta is None:
            print(f"[fit-pg] {bench}: no _meta.json at {corpus_dir} -> skip", flush=True)
            continue
        owner_tables = meta.extra.get("owner_tables") or []
        levels = [t.level for t in meta.tiers]
        if not owner_tables or not levels:
            print(f"[fit-pg] {bench}: empty owner_tables/levels in meta -> skip", flush=True)
            continue
        pgdb = args.db or _BENCH_DB[bench]
        be = get_backend("postgres",
                         cfg=DBConfig(host="localhost", port=5432, user="postgres",
                                      password="postgres", dbname=pgdb))
        print(f"[fit-pg] {bench}: pgdb={pgdb} tables={owner_tables} "
              f"levels={levels} k={args.k} repeats={args.repeats}", flush=True)
        fit_pg_bench(bench, pgdb, owner_tables, levels, outdir=Path(args.out),
                     backend=be, k=args.k, repeats=args.repeats, write=True)
        print(f"[fit-pg] {bench}: wrote {corpus_dir / '_maint.json'}", flush=True)


if __name__ == "__main__":
    main()
