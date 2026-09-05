"""Parallel census per-lambda Protocol-M measurement via DB mirrors.

Isolation model
---------------
Protocol-M measurement mutates table-global PG catalog state (enter λ-state sets
all single-column targets + ANALYZE; create/drop extended stats; catalog-mask
NULLs/restores shared pg_statistic_ext_data rows). Running two such measurements
concurrently on the SAME table races and corrupts each other's state.

To parallelize safely we clone the census DB into N mirrors (``census_m1..mN``,
each an isolated full copy of ``climate``) and run ONE worker per mirror. Each
worker exclusively owns its table copy => no shared-catalog race; the N workers
run concurrently on however many cores the host has.

This worker writes ONLY ``<qid>.json`` (per-query), never ``_meta.json`` (the
main process writes meta once before/after) => no file-race on the shared results
dir across workers.

Usage::
    python scratch/measure_census_parallel.py --n 8 \
        --out results/per_lambda/census/postgres \
        --dbprefix census_m --table .climate --arities 2
(discard old corpus first; a fresh ``_meta.json`` and 468 ``query.*.json`` land in
``--out`` after the run.)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import (DEFAULT_LAMBDA_LEVELS,
                                           measure_query_lambda_m)
from extstats2.core.measure_lambda_io import (LambdaTier, Meta, write_meta,
                                              result_dir)


def _worker(args_serial: dict) -> int:
    """Process one mirror: measure its shard of query ids. Runs in a child proc."""
    mirror_db = args_serial["mirror_db"]
    qids = args_serial["qids"]
    outdir = args_serial["outdir"]
    table = args_serial["table"]
    levels = tuple(args_serial["levels"])
    bench = args_serial["bench"]

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query

    be = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres",
        dbname=mirror_db))
    if not be.supports_catalog_mask():
        raise RuntimeError(f"{mirror_db}: no Protocol-M catalog-mask")

    # clean any cloned leftovers, restore natural single-col baseline once
    for s in list(be.list_stats(table)):
        be.drop_stat(s)

    Q = {q.qid: q for q in load_benchmark(bench)}
    dest = Path(outdir)
    dest.mkdir(parents=True, exist_ok=True)

    done = 0
    for qid in qids:
        q = Q.get(qid)
        if q is None:
            continue
        cands = generate_candidates_per_query([q], arities=(2,)).get(qid, [])
        # candidate-bearing metric: skip a query with no arity-2 candidate.
        if not cands:
            continue
        # resumable: skip if this query's file already carries all requested levels
        qf = dest / f"{qid}.json"
        if qf.exists():
            try:
                blk = json.load(open(qf))
                have = {int(k) for k in blk.get("by_lambda", {})}
                if have.issuperset(set(levels)):
                    continue
            except Exception:
                pass
        # measure (Protocol-M path since catalog-mask True) -> writes <qid>.json
        try:
            measure_query_lambda_m(be, q, cands, levels=levels, param_tiers=None,
                                   outdir=dest)
        except Exception as e:
            print(f"[{mirror_db}] ERR {qid}: {e}", flush=True)
        # ensure we measured the requested levels (cleanup between queries)
        for s in list(be.list_stats(table)):
            be.drop_stat(s)
        done += 1
        if done % 5 == 0 or done == 1:
            print(f"[{mirror_db}] {done}/{len(qids)} -> {qid}", flush=True)
    print(f"[{mirror_db}] DONE {done} queries", flush=True)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dbprefix", default="census_m")
    ap.add_argument("--table", default=".climate")
    ap.add_argument("--arities", type=int, nargs="+", default=[2])
    ap.add_argument("--levels", type=int, nargs="+", default=list(DEFAULT_LAMBDA_LEVELS))
    ap.add_argument("--bench", default="census")
    ap.add_argument("--processes", type=int, default=None)
    args = ap.parse_args()

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query
    Q = load_benchmark(args.bench)
    # candidate-bearing only (matches measure_sgrid/dmv_parallel main metric)
    qids = [q.qid for q in Q
            if generate_candidates_per_query([q], arities=(2,)).get(q.qid, [])]
    # shard round-robin across mirrors
    shards: dict[int, list[str]] = {i: [] for i in range(1, args.n + 1)}
    for idx, qid in enumerate(qids):
        shards[(idx % args.n) + 1].append(qid)

    outdir = result_dir(Path(args.out), args.bench, "postgres")
    outdir.mkdir(parents=True, exist_ok=True)

    # write _meta once (main process only)
    be0 = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres",
        dbname=f"{args.dbprefix}1"))
    table_src = args.table
    # de-dup'd meta shape (matches scratch/measure_sgrid.py + dmv_parallel):
    # tiers declare only the lambda LEVELS that exist; the authoritative per-table
    # realized S grid lives in extra.table_s_rows (PG: S_rows/single_target;
    # estimate_percent is an Oracle-only notion -> null on PG).
    tiers = [LambdaTier(level=lv, S_rows=None, single_target=None,
                        estimate_percent=None) for lv in args.levels]
    table_s_rows = {table_src: {str(lv): {
        "S_rows": be0.lambda_sampling_rows(table_src, lv),
        "single_target": be0.single_col_target_for_level(table_src, lv),
        "estimate_percent": None} for lv in args.levels}}
    write_meta(outdir, Meta(bench=args.bench, backend="postgres", tiers=tiers,
                            param_tiers=be0.representation_param_tiers(),
                            extra={"method": "S-grid global S_rows [30000,300000]; "
                                             "per-owner-table realized S in table_s_rows",
                                   "levels": list(args.levels),
                                   "owner_tables": [table_src],
                                   "table_s_rows": table_s_rows}))

    tasks = []
    for i in range(1, args.n + 1):
        tasks.append({
            "mirror_db": f"{args.dbprefix}{i}",
            "qids": shards[i],
            "outdir": str(outdir),
            "table": args.table,
            "levels": list(args.levels),
            "bench": args.bench,
        })

    n_proc = args.processes or args.n
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    print(f"[main] {len(qids)} queries over {args.n} mirrors, {n_proc} procs -> {outdir}")
    with ctx.Pool(processes=n_proc) as pool:
        results = pool.map(_worker, tasks)
    print(f"[main] DONE. per-mirror query counts: {results}")
    print(f"[main] qid files now: {len(list(outdir.glob('query.*.json')))}")


if __name__ == "__main__":
    main()
