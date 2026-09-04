"""Parallel DMV per-lambda Protocol-M measurement via DB mirrors.

DMV is single-table (the ``dmv`` table in the ``dmv_<n>`` mirrors). Queries are
1958-translated single-table COUNT predicates (categorical equality/IN). Because
PG folds unquoted identifiers this is pure single-table; no case normalization
needed (unlike stats_CEB_single's camelCase postHistory/postlinks).

Per the project's candidate-bearing main metric we SKIP queries that have no
arity-2 candidate (the 39 single-column/no-predicate ones) and measure the rest
(~1926 candidate-bearing), mirroring stats_CEB_single's 180/632 decision so the
reported workload-level means are comparable.

Isolation: Protocol-M mutates per-table PG catalog state, so measurement is fanned
out one worker per DMV clone (``dmv_m1..mN``); each worker owns its table copy.

Usage::
    # create mirrors first, e.g. from the loaded `dmv` db:
    #   for i in 1..N: CREATE DATABASE dmv_m$i TEMPLATE dmv
    python scratch/measure_dmv_parallel.py --n 8 \\
        --out results/per_lambda \\
        --dbprefix dmv_m --bench dmv \\
        --table .dmv --arities 2 --levels 0 1
(discard a stale corpus under results/per_lambda/dmv/postgres first.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import (DEFAULT_LAMBDA_LEVELS,
                                           measure_query_lambda_m)
from extstats2.core.measure_lambda_io import LambdaTier, Meta, result_dir, write_meta


def _worker(args: dict) -> int:
    import multiprocessing as _

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import (CandidateSet,
                                           generate_candidates_per_query)
    from extstats2.core.measure_lambda import measure_query_lambda_m

    mirror = args["mirror_db"]
    bench = args["bench"]
    table = args["table"]
    qids = args["qids"]
    outdir = args["outdir"]
    levels = tuple(args["levels"])
    be = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres", dbname=mirror))
    if not be.supports_catalog_mask():
        raise RuntimeError(f"{mirror}: no catalog-mask Protocol-M")
    Q = {q.qid: q for q in load_benchmark(bench)}
    for s in list(be.list_stats(table)):
        be.drop_stat(s)

    done = skipped = 0
    for qid in qids:
        q = Q.get(qid)
        if q is None:
            continue
        cands = generate_candidates_per_query([q], arities=(2,)).get(qid, [])
        if not cands:
            skipped += 1            # candidate-bearing metric: drop 1-col/no-pred
            continue
        cands = [CandidateSet(table=table, columns=cd.columns) for cd in cands]
        try:
            measure_query_lambda_m(be, q, cands, levels=levels,
                                   param_tiers=None, outdir=outdir)
        except Exception as e:
            print(f"[{mirror}] ERR {qid}: {e}", flush=True)
        for s in list(be.list_stats(table)):
            be.drop_stat(s)
        done += 1
        if done % 10 == 0:
            print(f"[{mirror}] {done}/{len(qids)} -> {qid}", flush=True)
    print(f"[{mirror}] DONE {done} measured, {skipped} skipped", flush=True)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "results" / "per_lambda"))
    ap.add_argument("--dbprefix", default="dmv_m")
    ap.add_argument("--table", default=".dmv")
    ap.add_argument("--arities", type=int, nargs="+", default=[2])
    ap.add_argument("--levels", type=int, nargs="+", default=list(DEFAULT_LAMBDA_LEVELS))
    ap.add_argument("--bench", default="dmv")
    ap.add_argument("--processes", type=int, default=None)
    args = ap.parse_args()

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query

    # candidate-bearing only (main metric)
    raw = load_benchmark(args.bench)
    keep = []
    for q in raw:
        cands = generate_candidates_per_query([q], arities=(2,)).get(q.qid, [])
        if cands:
            keep.append(q.qid)
    print(f"[main] dmv total={len(raw)} candidate-bearing to measure={len(keep)}")
    shards: dict[int, list[str]] = {i: [] for i in range(1, args.n + 1)}
    for idx, qid in enumerate(keep):
        shards[(idx % args.n) + 1].append(qid)

    outdir = result_dir(Path(args.out), args.bench, "postgres")
    outdir.mkdir(parents=True, exist_ok=True)

    be0 = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres",
        dbname=f"{args.dbprefix}1"))
    if not be0.supports_catalog_mask():
        print("[main] WARN: no catalog-mask on mirror1; mirrors may not exist")
    tiers = []
    for lv in args.levels:
        st = be0.single_col_target_for_level(args.table, lv)
        rows = be0.lambda_sampling_rows(args.table, lv)
        tiers.append(LambdaTier(level=lv, S_rows=rows, single_target=st,
                                estimate_percent=None))
    write_meta(outdir, Meta(bench=args.bench, backend="postgres", tiers=tiers,
                            param_tiers=be0.representation_param_tiers()))
    print(f"[main] meta written; {len(shards[1])}.. qids per mirror")

    tasks = []
    for i in range(1, args.n + 1):
        tasks.append({
            "mirror_db": f"{args.dbprefix}{i}", "bench": args.bench,
            "qids": shards[i], "outdir": str(outdir), "table": args.table,
            "levels": list(args.levels),
        })
    n_proc = args.processes or len(tasks)
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    print(f"[main] {len(keep)} qids over {args.n} mirrors, {n_proc} procs -> {outdir}")
    with ctx.Pool(processes=n_proc) as pool:
        res = pool.map(_worker, tasks)
    print("[main] per-mirror done:", res)

    files = list(outdir.glob("dmv.*.json"))
    print(f"[main] corpus files under {outdir}: {len(files)}")


if __name__ == "__main__":
    main()
