"""Per-query candidate-column-combination distribution for the DMV workload.

Uses the translated DMV queries (benchmarks/DMV/queries/dmv.sql if present, else
translates query.sql on the fly via bench.dmv). Candidates = per-query, per-table
combination generation (same as stats_CEB_single / census measurement): every
2-column (and optionally 3-column) subset of the query's filtered columns.

Reports (mirroring the census / stats_CEB_single distribution notes):
  - # queries, # candidate-bearing, # zero-candidate
  - predicate-columns-per-query summary
  - candidates-per-query at arity2 and arity2+3 (mean/med/p90/max)
  - workload-wide distinct colsets (by table) and how many queries each colset
    would serve (useful for what+how-much and 1-stat-sufficiency observations)

Read-only; does not touch the DB.
"""
from __future__ import annotations

import sys
from pathlib import Path
from collections import Counter, defaultdict

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extstats2.bench import load_benchmark
from extstats2.core.predicates import predicate_columns
from extstats2.core.candidates import iter_candidates_for_query


def pct(lines):
    return f"{100 * lines / total:.1f}%" if total else "n/a"


def main(argv):
    global total
    a2 = "--arity3" in argv
    Q = load_benchmark("dmv")
    total = len(Q)
    ncols = []
    perq2 = []
    perq23 = []
    n_zero = 0
    service = Counter()          # colset -> #queries that mention it (subset service)
    for q in Q:
        pred = predicate_columns(q)
        cols = set().union(*pred.values()) if pred else set()
        ncols.append(len(cols))
        cands2 = list(iter_candidates_for_query(q, arities=(2,)))
        cands23 = list(iter_candidates_for_query(q, arities=(2, 3)))
        perq2.append(len(cands2))
        perq23.append(len(cands23))
        if not cands2:
            n_zero += 1
        for c in cands23:
            service[(c.table, c.columns)] += 1

    ncols = np.asarray(ncols, int)
    a2 = np.asarray(perq2, int)
    a23 = np.asarray(perq23, int)
    print(f"DMV: {total} queries; candidate-bearing(arity2>0)="
          f"{total - n_zero} ({pct(total - n_zero)}); zero-candidate={n_zero}")
    print(f"filtered-cols/query: mean={ncols.mean():.2f} med={np.median(ncols)} "
          f"p90={np.percentile(ncols, 90)} max={ncols.max()}")
    print(f"arity-2 candidates/query: mean={a2.mean():.2f} med={np.median(a2)} "
          f"p90={np.percentile(a2, 90)} max={a2.max()}")
    print(f"arity-2,3 candidates/query: mean={a23.mean():.2f} med={np.median(a23)} "
          f"p90={np.percentile(a23, 90)} max={a23.max()}")
    # distinct colsets across workload and service counts
    print(f"distinct (table,colset) across workload: {len(service)}")
    sv = np.asarray(list(service.values()), int)
    print(f"queries-served per distinct colset: mean={sv.mean():.2f} "
          f"med={np.median(sv)} max={sv.max()}")
    bytab = defaultdict(int)
    for (tab, _cs), n in service.items():
        bytab[tab] += 1
    for tab in sorted(bytab, key=lambda t: -bytab[t]):
        print(f"   table {tab}: {bytab[tab]} distinct colsets across workload")
    # top colsets by workload prevalence
    top = service.most_common(8)
    print("\ntop prevalent colsets:")
    for (tab, cs), n in top:
        print(f"   {cs} on {tab}: served in {n} queries")


if __name__ == "__main__":
    main(sys.argv[1:])
