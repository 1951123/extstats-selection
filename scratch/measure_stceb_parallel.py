"""Parallel measurement of stats_CEB_single (632 single-table sub-plans) across
N DB mirrors, per-query multi-table Protocol-M, L0/L1/L2, dense PG param grid.

Each stats_CEB_single query filters ONE base table; candidates are per-query.
Because PG folds unquoted identifiers, candidate tables parsed from camelCase SQL
(postHistory/postLinks) are lowercased to the real catalog relname.

Usage (mirrors stats_m1..mN must exist as clones of `stats`):
  .venv/bin/python -u scratch/measure_stceb_parallel.py --n 8 --out results \\
      --dbprefix stats_m --levels 0 1 2
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_lambda import DEFAULT_LAMBDA_LEVELS
from extstats2.core.measure_lambda_io import LambdaTier, Meta, result_dir, write_meta
from extstats2.core.candidates import CandidateSet


def _worker(args: dict) -> int:
    import multiprocessing as _
    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure_lambda import measure_query_lambda_m
    mirror = args["mirror_db"]
    bench = args["bench"]
    qids = args["qids"]
    be = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres", dbname=mirror))
    Q = {q.qid: q for q in load_benchmark(bench)}
    for s in list(be.list_stats(".posts")):
        be.drop_stat(s)   # opportunistic clean leftovers from clone
    outdir = args["outdir"]
    done = 0
    for qid in qids:
        q = Q.get(qid)
        if q is None:
            continue
        cands = generate_candidates_per_query([q], arities=(2,)).get(qid, [])
        if not cands:
            # no selection predicates -> nothing to build; skip measure entirely
            continue
        norm = [CandidateSet(table=("." + cd.table.lstrip(".").lower()),
                             columns=cd.columns) for cd in cands]
        try:
            measure_query_lambda_m(be, q, norm, levels=tuple(args["levels"]),
                                   param_tiers=None, outdir=outdir)
        except Exception as e:  # keep going on per-query errors; note it
            print(f"[{mirror}] ERR {qid}: {e}", flush=True)
        # cleanup this query's own table leftover stats
        t = norm[0].table
        for s in list(be.list_stats(t)):
            be.drop_stat(s)
        done += 1
        if done % 10 == 0:
            print(f"[{mirror}] {done}/{len(qids)} -> {qid}", flush=True)
    print(f"[{mirror}] DONE {done}", flush=True)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--levels", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--bench", default="stats_ceb_single")
    ap.add_argument("--dbprefix", default="stats_m")
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--processes", type=int, default=None)
    ap.add_argument("--only", default=None, help="comma qids to run (subset)")
    a = ap.parse_args()

    from extstats2.bench import load_benchmark
    Q = load_benchmark(a.bench)
    if a.only:
        qids = [x for x in a.only.split(",")]
    else:
        qids = [q.qid for q in Q]
    shards = {i: [] for i in range(1, a.n + 1)}
    for idx, qid in enumerate(qids):
        shards[(idx % a.n) + 1].append(qid)

    outdir = result_dir(Path(a.out), a.bench, "postgres")
    outdir.mkdir(parents=True, exist_ok=True)
    # meta once (representative table .posts for S rows; approx enough)
    be0 = get_backend("postgres", cfg=DBConfig(
        host="localhost", port=5432, user="postgres", password="postgres",
        dbname=f"{a.dbprefix}1"))
    tiers = []
    probe_tab = ".posts"
    for lv in a.levels:
        tiers.append(LambdaTier(
            level=lv,
            S_rows=be0.lambda_sampling_rows(probe_tab, lv),
            single_target=be0.single_col_target_for_level(probe_tab, lv),
            estimate_percent=None))
    write_meta(outdir, Meta(bench=a.bench, backend="postgres", tiers=tiers,
                            param_tiers=be0.representation_param_tiers()))
    tasks = [{"mirror_db": f"{a.dbprefix}{i}", "qids": shards[i], "outdir": str(outdir),
              "levels": list(a.levels), "bench": a.bench} for i in range(1, a.n + 1)]
    # drop empty shards (only relevant when --only small)
    tasks = [t for t in tasks if t["qids"]]
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    n_proc = a.processes or min(len(tasks), a.n)
    print(f"[main] {len(qids)} queries / {len(tasks)} mirrors, {n_proc} procs -> {outdir}")
    if len(tasks) == 1:
        n = _worker(tasks[0]); print("[main] DONE", n)
    else:
        with ctx.Pool(processes=n_proc) as pool:
            res = pool.map(_worker, tasks)
        print("[main] DONE per-worker", res)
    print("[main] files:", len(list(outdir.glob("st.*.json"))))

if __name__ == "__main__":
    main()
