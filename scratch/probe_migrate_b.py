"""Probe for Experiment (B): does the single-workload chosen extended-stat set
(deployed on the shared base tables) actually help the multi-table stats_CEB
join workload?

Steps:
  1) Phase-1 MILP on stats_ceb_single @ L1, budget 80KB -> chosen (colset,param)
     + authoritative colset->owning table (via serving single-table qid).
  2) Overlap check: which chosen colsets are deployable onto a join's per-table
     filter columns (i.e. can at most improve a join input's cardinality).
  3) Baseline join card-error (already EXPLAINed) vs post flag below.

Pure analysis (no DB writes). Read-only grounding.
"""
from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass
from extstats2.bench import load_benchmark
from extstats2.core.predicates import predicate_columns


def qtab(q):
    pc = predicate_columns(q)
    return (".%s" % next(iter(pc)).lstrip(".").lower()) if len(pc) == 1 else None


def main(level="1", budget=80000):
    corpus = ROOT / "results" / "per_lambda"
    meta, blocks = load_lambda_problem(corpus, "stats_ceb_single", "postgres")
    phys, opts, qbs = build_inner_at_level(blocks, level, skip_worse_than_baseline=True)
    res = solve_ilp(phys, list(opts), [float(v) for v in qbs], budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, objective="mean")
    Qs = load_benchmark("stats_ceb_single")
    qtab_s = {q.qid: qtab(q) for q in Qs}
    qids = list(blocks)
    cs_tab = {}
    for i, qid in enumerate(qids):
        t = qtab_s.get(qid)
        for key in res.chosen[i]:
            if "|" in key:
                cols = tuple(sorted(key.split("|")[1].split(",")))
                cs_tab[cols] = t  # authoritative owning table
    # chosen by owning table
    sel = defaultdict(list)
    for ps in res.selected_stats:
        sel[cs_tab.get(tuple(ps.columns))].append(tuple(ps.columns))
    print(f"Phase-1 L{level}@{budget}: n_sel={len(res.selected_stats)} "
          f"pred_mean={res.mean_qerror:.4f}; distinct colsets={len(cs_tab)}")
    # join per-table filter cols
    J = load_benchmark("stats_ceb")
    jf = defaultdict(set)
    for q in J:
        for t, cols in predicate_columns(q).items():
            jf[t].update(cols)
    # column alias sets: joins reference lower-case physical names; single cols
    # come from schema so already lower-case. Normalise '.postHistory'->'.posthistory'.
    def norm(t):
        return t.split(".")[0] + "." + t.split(".")[1].lower() if "." in t else t

    hit = 0
    table_hit = defaultdict(int)
    for t, lst in sel.items():
        jcols = jf.get(norm(t) if t else "", set())
        for cs in lst:
            if set(cs) <= jcols:
                hit += 1
                table_hit[t] += 1
    print(f"deployable-to-a-join-filter chosen colsets: {hit}/{len(res.selected_stats)}")
    for t in sorted(table_hit):
        print(f"    {t}: {table_hit[t]} deployed colsets usable as join input filter")


if __name__ == "__main__":
    main()
