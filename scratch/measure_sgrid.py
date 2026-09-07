"""S-grid full re-measure driver over all three single-table benchmarks.

2026-09-05: global S_rows grid = [30000, 300000]; both PG (statistics_target
{100,1000}) and Oracle (per-table estimate_percent from S) now realize the SAME
per-table S points in their backends. This driver measures every query of a
benchmark at the S-grid levels (default 0,1) for one backend, writing one JSON
per query under ``results/measure/<bench>/<backend>/``.

Driver strategy (chooses the backend's own measure path automatically):
  - ``measure_query_sampling_m`` dispatches: PG (catalog-mask capable) -> the
    Protocol-M path (~1 scan per λ per query, cheap); Oracle -> falls back to
    Protocol-A (one GATHER per candidate x level, the honest cost).
  - Oracle Protocol-A is serial and must not race the shared catalog: run with a
    single worker (default). PG can optionally run N workers over cloned mirror
    DBs, but for a first correct corpus a single serial worker is fine.

The backend ladder is table-aware (census -> CLIMATE, DMV -> DMV, stats_CEB ->
per-query owner table), so no per-query S plumbing is needed here beyond reading
each query's owning table for the per-level S metadata.

Resumable: a query is skipped if its output file already carries the requested
requested levels. Writes ``_meta.json`` with per-level tiers (S_rows for a
representative ref table per backend; real per-query S lives in each file).

Usage::
    python scratch/measure_sgrid.py --bench census  --backend oracle
    python scratch/measure_sgrid.py --bench census  --backend postgres
    python scratch/measure_sgrid.py --bench stats_ceb_single --backend oracle --limit 5
    python scratch/measure_sgrid.py --bench dmv --backend postgres
    # one backend at a time; levels default 0,1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from extstats2.config import DBConfig, get_backend
from extstats2.core.measure_sampling import (DEFAULT_SAMPLING_LEVELS,
                                           measure_query_sampling_m)

# Per-backend connection (dotted 'table' keys on each engine).
_PG = dict(host="localhost", port=5432, user="postgres", password="postgres",
           dbname="census")
# stats_CEB_single/dmv live in OTHER PG dbnames; caller passes --pgdb.
_OR = dict(host="localhost", port=1521, user="SYSTEM", password="lxf82073077",
           service="FREEPDB1")


def _backend(name: str, pgdb: str):
    if name == "postgres":
        cfg = DBConfig(host="localhost", port=5432, user="postgres",
                       password="postgres", dbname=pgdb)
    else:
        cfg = DBConfig(**_OR)
    return get_backend(name, cfg=cfg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True,
                    choices=["census", "stats_ceb_single", "dmv"])
    ap.add_argument("--backend", required=True, choices=["postgres", "oracle"])
    ap.add_argument("--levels", type=int, nargs="+", default=list(DEFAULT_SAMPLING_LEVELS))
    ap.add_argument("--pgdb", default=None,
                    help="PG database name (census/dmv/stats_ceb_single) holding the table")
    ap.add_argument("--out", default="results")
    ap.add_argument("--limit", type=int, default=0, help="0 = all queries")
    args = ap.parse_args()

    # Resolve the owning PostgreSQL database holding the bench's table(s).
    pgdb = args.pgdb or {"census": "census", "dmv": "dmv",
                         "stats_ceb_single": "stats"}[args.bench]

    from extstats2.bench import load_benchmark
    from extstats2.core.candidates import generate_candidates_per_query
    from extstats2.core.measure_io import (result_dir, Meta, SampleTier,
                                                  write_meta)

    be = _backend(args.backend, pgdb)
    dest = result_dir(Path(args.out), args.bench, args.backend)
    dest.mkdir(parents=True, exist_ok=True)

    Q = {q.qid: q for q in load_benchmark(args.bench)}
    qids = sorted(Q.keys())
    if args.limit:
        qids = qids[: args.limit]
    print(f"[sgrid] bench={args.bench} backend={args.backend} "
          f"levels={list(args.levels)} queries={len(qids)} -> {dest}",
          flush=True)

    cand_all = generate_candidates_per_query([Q[k] for k in qids], arities=(2,))

    # Owner table(s) this bench actually measures (single per query; the set spans
    # every base table the candidate-bearing queries touch -> multi-table benches).
    owner_tables = sorted({cands[0].table for cands in cand_all.values() if cands})

    def _tier_meta(tbl: str, lv: int) -> dict:
        rows = be.sample_rows_at_level(tbl, lv)
        st = be.single_col_target_for_level(tbl, lv)
        ep = be.sample_percent_at_level(tbl, lv)
        return {"S_rows": rows, "single_target": st, "estimate_percent": ep}

    # tiers: a lightweight DECLARATION of which lambda levels exist (downstream
    # only reads tiers[].level). Per-table S is NOT repeated here -- the full,
    # authoritative per-owner-table S lives in extra.table_s_rows. Because the
    # S-grid's realized S depends on each owner table's N (which spans multiple
    # tables on stats_CEB_single), a single tiers.S_rows would be ambiguous/re-
    # dundant; record S only once, per table.
    tiers = [SampleTier(level=lv, S_rows=None, single_target=None,
                        estimate_percent=None) for lv in args.levels]
    # Full per-owner-table S map (single authoritative source of realized S).
    table_s_rows: dict[str, dict] = {}
    for tbl in owner_tables:
        table_s_rows[tbl] = {str(lv): _tier_meta(tbl, lv) for lv in args.levels}

    def _write_meta() -> None:
        write_meta(dest, Meta(bench=args.bench, backend=args.backend, tiers=tiers,
                              param_tiers=tuple(be.representation_param_tiers()),
                              extra={
                                  "method": "S-grid global S_rows [30000,300000]; "
                                            "per-owner-table realized S in table_s_rows",
                                  "levels": list(args.levels),
                                  "owner_tables": owner_tables,
                                  "table_s_rows": table_s_rows}))

    _write_meta()

    done, skipped, failed = 0, 0, 0
    t0 = time.time()
    for i, qid in enumerate(qids, 1):
        q = Q[qid]
        cands = cand_all.get(qid, [])
        # No 2-column candidate -> nothing to repair/extend: a query with no
        # correlation-prone selection pair cannot be improved, and the meta note
        # says no-candidate queries are excluded from downstream means. Skipping
        # also avoids measuring a meaningless baseline on a wrong default table.
        if not cands:
            skipped += 1
            continue
        tbl = cands[0].table            # the query's single owner table
        out_f = dest / f"{qid}.json"
        if out_f.exists():
            try:
                blk = json.load(open(out_f))
                have = {int(k) for k in blk.get("by_lambda", {})}
                if have.issuperset(set(args.levels)):
                    skipped += 1
                    continue
            except Exception:
                pass
        try:
            measure_query_sampling_m(be, q, cands, levels=tuple(args.levels),
                                   param_tiers=None, outdir=dest)
            done += 1
        except Exception as e:  # noqa: BLE001 - keep the run alive
            failed += 1
            with (dest / f"{qid}.err").open("a") as fh:
                fh.write(f"{time.time()}\t{type(e).__name__}: {e}\n")
            print(f"[sgrid] {qid} FAILED {type(e).__name__}: {e}", flush=True)
            continue
        if done % 10 == 0 or done == 1:
            el = time.time() - t0
            print(f"[sgrid] {i}/{len(qids)} ok={done} skip={skipped} "
                  f"fail={failed} ({el/60:.1f}m)<- {qid} on {tbl}",
                  flush=True)

    el = time.time() - t0
    print(f"[sgrid] COMPLETE {args.bench}/{args.backend} "
          f"ok={done} skip={skipped} fail={failed} "
          f"({el/60:.1f}m)", flush=True)
    # leave each measured table in natural state (clean residual colgroups / ext
    # stats on every owner table the run touched)
    try:
        for tbl in owner_tables:
            for s in list(be.list_stats(tbl)):
                be.drop_stat(s)
            be.restore_natural_stats(tbl)
    except Exception:
        pass
    # finalize _meta.json (per-owner-table S map included)
    _write_meta()


if __name__ == "__main__":
    sys.exit(main())
