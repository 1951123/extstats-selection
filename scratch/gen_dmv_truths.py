"""Recompute authoritative ground-truth cardinalities for the BayesCard DMV
workload on OUR DMV snapshot (data in the `dmv` PG database / table `dmv`).

The ``||<truth>`` shipped by BayesCard was computed on the author's different
snapshot and does NOT reproduce on the public original.csv we loaded; per the
repo note we "以数据为准", so we re-run ``SELECT count(*)`` per query.

Reads benchmarks/DMV/queries/query.sql (1965 DSL lines), translates each to PG
SQL (extstats2.bench.dmv), runs the real count on the `dmv` DB (parallel across
processes), and writes benchmarks/DMV/queries/dmv.sql in the canonical format
``<translated_sql>||<truth>`` that bench/dmv.py's load_dmv consumes.

Usage:
  .venv/bin/python -u scratch/gen_dmv_truths.py --limit 100   # smoke
  .venv/bin/python -u scratch/gen_dmv_truths.py               # full
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extstats2.bench.dmv import dsl_line_to_sql
from extstats2.config import DBConfig, get_backend

SRC = ROOT / "benchmarks" / "DMV" / "queries" / "query.sql"
OUT = ROOT / "benchmarks" / "DMV" / "queries" / "dmv.sql"
DB = "dmv"


def _read(sql_list, index):
    be = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres",
        dbname=DB))
    be.conn.autocommit = True
    cur = be.conn.cursor()
    out = []
    for idx in index:
        sql = sql_list[idx]
        cur.execute(sql)
        n = cur.fetchone()[0]
        out.append((idx, n))
    be.conn.close()
    return out


def main(limit, workers, start):
    lines = [l for l in SRC.open() if l.strip() and not l.startswith("--")]
    n = len(lines)
    if limit:
        n = min(limit, n)
    sqls = []
    truths_stale = []
    for ln in lines[:n]:
        s, t = dsl_line_to_sql(ln)
        sqls.append(s)
        truths_stale.append(t)

    t0 = time.perf_counter()
    idx_all = list(range(start, n))
    if workers <= 1:
        res = _read(sqls, idx_all)
    else:
        chunk = [[] for _ in range(workers)]
        for k, i in enumerate(idx_all):
            chunk[k % workers].append(i)
        with mp.get_context("spawn").Pool(workers) as pool:
            parts = pool.starmap(_read, [(sqls, c) for c in chunk])
        res = [pair for part in parts for pair in part]
    res.sort()

    # assemble canonical file (SQL || truth), qid = line number base
    with OUT.open("w") as fh:
        for idx, val in res:
            fh.write(f"{sqls[idx]}||{val}\n")
    # summary/validation
    dt = time.perf_counter() - t0
    mism = sum(1 for idx, v in res if truths_stale[idx] is not None and v != truths_stale[idx])
    changed = sum(1 for idx, v in res
                  if truths_stale[idx] is not None and abs(v - truths_stale[idx]) > 0.01 * max(v, truths_stale[idx]))
    print(f"wrote {OUT} ({len(res)}/{n} queries) in {dt:.0f}s "
          f"({dt/max(len(res),1):.2f}s/q); {mism} differ from BayesCard truth; "
          f"{changed} differ by >1%")
    print(f"first={res[0] if res else None} last={res[-1] if res else None}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--start", type=int, default=0)
    a = ap.parse_args()
    main(a.limit, a.workers, a.start)
