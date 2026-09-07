"""Smoke: measure a couple of stats_CEB_single queries at L0 (and optionally L1)
on one mirror DB with the per-query multi-table path, using dense param grid.
Confirms per-table candidate generation + Protocol-M measure + writes qid JSON.
Usage:
  .venv/bin/python -u scratch/smoke_stceb.py --db stats_m1 --qids st.1,st.2 --levels 0 [1]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from extstats2.config import DBConfig, get_backend
from extstats2.bench import load_benchmark
from extstats2.core.candidates import generate_candidates_per_query, CandidateSet
from extstats2.core.measure_sampling import DEFAULT_SAMPLING_LEVELS, measure_query_sampling_m
from extstats2.core.measure_io import result_dir, write_meta, Meta, SampleTier
from extstats2.backend.postgres import PostgresBackend as _PB  # noqa

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="stats_m1")
    ap.add_argument("--qids", default="st.1,st.2,st.3")
    ap.add_argument("--levels", type=int, nargs="+", default=[0])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    be = get_backend("postgres", cfg=DBConfig(host="localhost", port=5432,
                     user="postgres", password="postgres", dbname=a.db))
    Q = {q.qid: q for q in load_benchmark("stats_ceb_single")}
    dest = result_dir(Path(a.out or (ROOT / "results" / "measure")), "stats_ceb_single", "postgres")
    dest.mkdir(parents=True, exist_ok=True)
    # write meta once (levels)
    table_src = ".posts"   # representative for meta numbers
    tiers = []
    for lv in a.levels:
        st = be.single_col_target_for_level(table_src, lv)
        rows = be.sample_rows_at_level(table_src, lv)
        tiers.append(SampleTier(level=lv, S_rows=rows, single_target=st, estimate_percent=None))
    write_meta(dest, Meta(bench="stats_ceb_single", backend="postgres", tiers=tiers,
                          param_tiers=be.representation_param_tiers()))
    from extstats2.core.candidates import CandidateSet
    for qid in a.qids.split(","):
        q = Q.get(qid)
        if q is None:
            print("missing", qid); continue
        cands = generate_candidates_per_query([q], arities=(2,)).get(qid, [])
        # PG folds unquoted identifiers to lowercase; the single-table SQL uses
        # camelCase (postHistory/postLinks) but the real catalog tables are
        # lowercase -> normalize candidate tables to the folded (real) form.
        norm = []
        for cd in cands:
            real = cd.table if cd.table.startswith('"') else ("." + cd.table.lstrip('.').lower())
            norm.append(CandidateSet(table=real, columns=cd.columns))
        cands = norm
        t = cands[0].table if cands else None
        print(f"{qid}: table={t} n_cand={len(cands)}")
        if not cands:
            continue
        block = measure_query_sampling_m(be, q, cands, levels=tuple(a.levels),
                                       param_tiers=None, outdir=dest)
        # cleanup this query's own table leftover stats
        for s in list(be.list_stats(t)):
            be.drop_stat(s)
        slot0 = block["by_lambda"].get(str(a.levels[0]))
        print(f"  -> L{a.levels[0]} baseline_q={slot0['baseline']['qerror']:.3f} "
              f"n_cand_rows={len(slot0['candidates'])}")
    print("smoke done")

if __name__ == "__main__":
    main()
