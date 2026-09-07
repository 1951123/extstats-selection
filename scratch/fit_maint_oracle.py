"""Run the Oracle maintenance-parameter fit and write _maint.json per bench corpus.

Usage:  python scratch/fit_maint_oracle.py [--benches census stats_ceb_single dmv]
        [--out results] [--k-cap 100] [--repeats 3]
Reads owner tables + levels from results/measure/{bench}/oracle/_meta.json, times
GATHERs on the live Oracle 23ai, writes _maint.json to results/measure/{bench}/oracle/.

NOTE: Oracle is a single DB; do not run while a background measure (e.g. dmv/oracle
sgrid) is active on the same instance, and a bench with no oracle _meta yet (e.g.
census/oracle) is skipped.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.maint_fit_oracle import fit_oracle_bench
from extstats2.core.measure_io import result_dir, read_meta

_ORACLE = dict(host="localhost", port=1521, user="SYSTEM",
               password="lxf82073077", service="FREEPDB1")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benches", nargs="+", default=["stats_ceb_single"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--k-cap", type=int, default=100)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    be = get_backend("oracle", cfg=DBConfig(**_ORACLE))
    for bench in args.benches:
        d = result_dir(Path(args.out), bench, "oracle")
        meta = read_meta(d)
        if meta is None:
            print(f"[fit-or] {bench}: no oracle _meta.json at {d} -> skip", flush=True)
            continue
        owner_tables = meta.extra.get("owner_tables") or []
        levels = [t.level for t in meta.tiers]
        if not owner_tables or not levels:
            print(f"[fit-or] {bench}: empty owner_tables/levels -> skip", flush=True)
            continue
        fit_oracle_bench(bench, owner_tables, levels, outdir=Path(args.out),
                         backend=be, k_cap=args.k_cap, repeats=args.repeats)
        print(f"[fit-or] {bench}: wrote {d / '_maint.json'}", flush=True)


if __name__ == "__main__":
    main()
