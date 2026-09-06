"""Shared loader for Phase-2 tests: build census L1 chosen-colset context.

Returns:
  dict with keys:
    chosen_list : list[ChosenStat] (over chosen colsets from generic Phase-1)
    cols_order  : list of colset-tuples aligned to chosen_list index
    applicable  : qid -> tuple of chosen indices (colset subset of query preds)
    base_by_qid : qid -> baseline q-error (over  468 queries)
    predicted_mean : Phase-1 predicted mean
    qids_all
"""
from __future__ import annotations
import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]   # project root (file under scratch/)
sys.path.insert(0, str(ROOT / "src"))


def load_ctx(level="1", budget=100000):
    from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
    from extstats2.core.optimize import solve_ilp, OptimizerClass
    from extstats2.core.optimize_pg_order import ChosenStat
    from extstats2.bench import load_benchmark
    from extstats2.core.measure_lambda_io import read_query_measure

    meta, blocks = load_lambda_problem(ROOT / "results" / "measure", "census", "postgres")
    phys, opts, qbases = build_inner_at_level(blocks, level, skip_worse_than_baseline=True)
    res = solve_ilp(phys, list(opts), [float(v) for v in qbases], budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, objective="mean")
    chosen_colsets = {tuple(ps.columns): ps.level for ps in res.selected_stats}
    cols_order = sorted(chosen_colsets.keys())
    corpus = ROOT / "results" / "measure" / "census" / "postgres"
    queries = {q.qid: q for q in load_benchmark("census")}
    qids_all = sorted(queries.keys(), key=lambda x: int(x.split(".")[1]))
    candidx = {}
    for q in qids_all:
        blk = read_query_measure(corpus, q)
        slot = blk.get("by_lambda", {}).get(level) if blk else None
        if not slot:
            continue
        pm = {}
        for cd in slot.get("candidates", []):
            k = tuple(sorted(cd["cols"]))
            pm.setdefault(k, {})[int(cd["param"])] = float(cd["qerror"])
        candidx[q] = pm

    chosen_list = []
    for c in cols_order:
        param = chosen_colsets[c]
        skey = f"|{','.join(sorted(c))}|P{param}"
        qerd = {}
        for q in qids_all:
            if q not in candidx:      # no file / no-candidate query (S-grid skips it)
                continue
            ev = candidx[q].get(tuple(sorted(c)), {}).get(param)
            if ev is not None:
                qerd[q] = ev
        chosen_list.append(ChosenStat(columns=c, stat_key=skey, qerr_by_query=qerd))

    def qpred(sql):
        m = re.search(r"WHERE\s+(.*)", sql)
        return {cc for cc in re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*(?:=|>=|<=|<>|<|>)", m.group(1))} if m else set()

    applicable = {}
    for q in qids_all:
        apps = tuple(i for i, c in enumerate(cols_order) if set(c) <= qpred(queries[q].sql))
        if apps:
            applicable[q] = apps
    qids_order = list(blocks.keys())
    base_by_qid = {qid: float(b) for qid, b in zip(qids_order, qbases)}
    return dict(chosen_list=chosen_list, cols_order=cols_order, applicable=applicable,
                base_by_qid=base_by_qid, predicted_mean=res.mean_qerror,
                qids_all=qids_all, total_bytes=res.total_bytes)
