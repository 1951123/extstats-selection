"""LEGACY M4 cross-backend validation (old capacity-level era, uses
core.measure + capacity level 0).  It answers an old question (do PG & Oracle
behave alike under 1-column-group, capacity-level-0 one-stat repair); it is NOT
the current S-grid evaluation path.  Keep for reference / historical comparisons.

Cross-backend validation (M4).

Runs the *same* core phases on the same underlying data through two different
backends (PostgreSQL + Oracle) and compares the results, to demonstrate that
the abstraction in ``backend/base.py`` is genuinely general:

1. **measure** a shared slice of the Census workload on each backend
   (same `climate` table, 2,458,285 rows on both), probing the primary `mcv`
   capability at capacity level 0;
2. assemble each backend's measurements into the v1-compatible phase-1 shape;
3. run the **identical** ``solve_ilp`` on both, selecting statistics under a
   storage budget;
4. compare: baseline q-error alignment across backends, the *set of selected
   column-groups* (should agree — it is a property of the correlated *data*,
   not the engine), and the achieved mean q-error / maintenance choices.

Because both engines see the *same rows*, any agreement in the *structure* of
the answer (which correlated column-sets to buy) is evidence that the core
selection model is backend-independent; residual disagreement in *absolute*
q-error / cost is expected (different optimizer histogram defaults and sample
sizes) and is reported, not hidden.

Usage (from repo root, with both DBs reachable)::

    PY-ORACLE ...
    python -m extstats2.eval.cross_compare --k 3 --budget 0
    python -m extstats2.eval.cross_compare --k 3 --budget 200000 --out results/cross_pg_oracle.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..config import DBConfig, get_backend
from ..core.candidates import generate_candidates_per_query

# Dev-environment connection handles; mirror the integration tests.
_PG = DBConfig(host="localhost", port=5432, user="postgres",
               password="postgres", dbname="census")
_OR = DBConfig(host="localhost", port=1521, user="SYSTEM",
               password="lxf82073077", service="FREEPDB1")

_BACKENDS = {"postgres": _PG, "oracle": _OR}


def _load_queries(bench_name: str = "census"):
    from ..bench import load_benchmark
    return load_benchmark(bench_name)


def _measure_backend(backend_name: str, queries, k: int,
                     capacity_levels=(0,), restore_natural=True):
    """Measure `k` Census queries on one backend; return per-query phase-1
    result dicts plus baseline/query metadata.

    ``restore_natural`` (default True) re-establishes each engine's *natural*
    per-column statistics before measuring, so the "no extended stats" baseline
    reflects a normal deployment on each backend (fair cross-backend compare).
    """
    cfg = _BACKENDS[backend_name]
    be = get_backend(backend_name, cfg=cfg)
    # clean slate on the tables we touch (census -> climate)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    if restore_natural:
        be.restore_natural_stats(".climate")
    results = []
    n_measured = 0
    for q in queries:
        cands = generate_candidates_per_query([q])[q.qid]
        if len(cands) < 1:
            continue
        if n_measured >= k:
            break
        n_measured += 1
        from ..core.measure import measure_query
        mes = measure_query(be, q, cands, capacity_levels=capacity_levels)
        # -> phase-1 result dict (v1-compatible)
        cand_out = {}
        for ckey, cm in mes.candidates.items():
            levels_out = {}
            for level, lv in cm.levels.items():
                levels_out[str(level)] = {
                    "estimate": lv["estimate"],
                    "qerror": lv["qerror"],
                    "qerror_repeats": lv.get("qerror_repeats"),
                    "size_bytes": lv["size_bytes"],
                    "maint_cost": lv["maint_cost"],
                    "level": level,
                }
            cand_out[ckey] = {
                "table": cm.table,
                "columns": list(cm.columns),
                "levels": levels_out,
            }
        results.append({
            "qid": mes.qid,
            "bench": mes.bench,
            "qerror_base": mes.qerror_base,
            "estimate_base": mes.estimate_base,
            "actual": mes.actual,
            "candidates": cand_out,
        })
    # clean up (defensive)
    for s in list(be.list_stats(".climate")):
        be.drop_stat(s)
    return be, results, n_measured


def _run_ilp(backend, results, budget_bytes, maint_budget=None):
    """Run the shared core ILP over one backend's measurements (all mcv L0).

    The optimization objective is fixed by the solver class (cap=1 / exact
    arithmetic-mean via SPARSE_LINEAR here); there is no objective switch.

    ``maint_budget`` is None by default -> no maintenance constraint (pure
    storage-budget selection). When a finite budget is given, we pass both the
    Y-two-layer MaintProfile and the budget so the solver adds the per-table
    table-activated fixed-cost constraint (the shared fixed GATHER/ANALYZE cost
    per activated table).
    """
    from ..core.optimize import (
        OptimizerClass, MaintProfile, build_problem, select_optimizer_class,
        solve_ilp)

    phase1 = {"results": results}
    phys_stats, queries_options, qerror_base = build_problem(phase1, qerror_mode="first")
    props = backend.structural_props()
    opt_class = select_optimizer_class(props)
    # solve_ilp enforces `sum cost <= budget`, so `0` does NOT mean "unlimited";
    # map an unlimited/0 request to a large-but-finite budget.
    eff_budget = budget_bytes if budget_bytes and budget_bytes > 0 else (1 << 60)
    profile = None
    if maint_budget is not None:
        profile = MaintProfile()
        for t in sorted({ps.table for ps in phys_stats}):
            profile.table_base_tiers[t] = backend.table_maintain_tiers(t)
    # The sparse-linear class materialises its exactly-linear objective via the
    # per-query "at most one statistic" cap; pass per_query_cap=1 for it.
    per_query_cap = 1 if opt_class == OptimizerClass.SPARSE_LINEAR else None
    res = solve_ilp(
        phys_stats, queries_options, qerror_base,
        budget_bytes=eff_budget,
        maint_budget=maint_budget,
        maint_profile=profile,
        per_query_cap=per_query_cap,
        optimizer_class=opt_class,
    )
    return backend, res


def compare(backends=("postgres", "oracle"), k=3, budget=0, bench_name="census",
            maint_budget=None):
    queries = _load_queries(bench_name)
    out = {"bench": bench_name, "queries": {"requested_k": k,
                                            "used": {}},
           "backends": {}}
    per_backend = {}
    for bname in backends:
        be, results, used = _measure_backend(bname, queries, k)
        backend_results = results
        # maintenance model: report per-table fixed tiers (same core, per backend)
        from ..core.optimize import MaintProfile
        prof = MaintProfile()
        for t in sorted({r["table"] for r in (c for r in backend_results
                                              for c in r["candidates"].values())}):
            prof.table_base_tiers[t] = be.table_maintain_tiers(t)
        out["backends"][bname] = {
            "queries_measured": used,
            "maint_tiers": {t: list(v) for t, v in prof.table_base_tiers.items()},
            "maint_structure": be.structural_props().maint_structure,
            "phase1": backend_results,
        }
        _, res = _run_ilp(be, backend_results, budget, maint_budget=maint_budget)
        selected = [
            {"table": ps.table,
             "columns": list(ps.columns),
             "level": ps.level,
             "cost_bytes": ps.cost,
             "maint_var": ps.maint_cost}
            for ps in res.selected_stats
        ]
        stats_by_query = []
        for i, qid in enumerate([r["qid"] for r in backend_results]):
            stats_by_query.append({"qid": qid, "qerr": res.qerror_per_query[i],
                                   "baseline": res.baseline_per_query[i]})
        out["backends"][bname]["ilp"] = {
            "objective": "mean",
            "budget_bytes": budget,
            "optimizer_class": res.__class__.__name__,
            "status": str(res.status),
            "mean_qerror": float(res.mean_qerror),
            "total_bytes": int(res.total_bytes),
            "total_maint": float(res.total_maint),
            "selected_stats": selected,
            "per_query": stats_by_query,
        }
        per_backend[bname] = {
            "phase1": backend_results,
            "ilp": out["backends"][bname]["ilp"],
        }
    # ---- cross-backend agreement -----------------------------------------
    comp = _compare_backends(out["backends"])
    out["comparison"] = comp
    return out, per_backend


def _compare_backends(backends: dict):
    """Structural agreement of ILP outcomes across the backends present."""
    comp = {"per_query_baseline_ratio": {},
            "selected_column_sets": {}, "agreement": None}
    names = list(backends.keys())
    if len(names) < 2:
        return comp
    a, b = names[0], names[1]
    # baseline parity on the shared qids
    def _base(qid, name):
        for r in backends[name]["phase1"]:
            if r["qid"] == qid:
                return r["qerror_base"]
        return None
    shared_qids = sorted({r["qid"] for r in backends[a]["phase1"]}
                         & {r["qid"] for r in backends[b]["phase1"]})
    for qid in shared_qids:
        ba, bb = _base(qid, a), _base(qid, b)
        if ba and bb:
            comp["per_query_baseline_ratio"][qid] = {
                a: round(ba, 3), b: round(bb, 3),
                "ratio_a_b": round(ba / bb, 3)}
    # selected column-set per backend (table + sorted columns)
    def _colset(name):
        return {(ps["table"], tuple(sorted(ps["columns"])))
                for ps in backends[name]["ilp"]["selected_stats"]}
    sa = _colset(a)
    sb = _colset(b)
    comp["selected_column_sets"][a] = sorted([(t, list(c)) for t, c in sa])
    comp["selected_column_sets"][b] = sorted([(t, list(c)) for t, c in sb])
    comp["agreement"] = {
        "column_sets_equal": sa == sb,
        "shared_count": len(sa & sb),
        "only_in_" + a: sorted([(t, list(c)) for t, c in (sa - sb)]),
        "only_in_" + b: sorted([(t, list(c)) for t, c in (sb - sa)]),
    }
    comp["mean_qerror"] = {
        a: round(backends[a]["ilp"]["mean_qerror"], 3),
        b: round(backends[b]["ilp"]["mean_qerror"], 3),
    }
    return comp


def _dump(out, path: Optional[str]):
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2))
        print(f"[cross_compare] wrote {p}")


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backends", nargs="+", default=["postgres", "oracle"],
                    choices=["postgres", "oracle"])
    ap.add_argument("--k", type=int, default=3,
                    help="number of census queries to measure per backend")
    ap.add_argument("--budget", type=int, default=0,
                    help="storage budget bytes (0 = unlimited)")
    ap.add_argument("--maint-budget", type=float, default=None,
                    help="maintenance budget (seconds/refresh). None = no "
                         "maintenance constraint (pure storage selection).")
    ap.add_argument("--bench", default="census",
                    choices=["census", "stats_ceb_single"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out, _ = compare(backends=tuple(args.backends), k=args.k,
                     budget=args.budget, bench_name=args.bench,
                     maint_budget=args.maint_budget)
    _dump(out, args.out)
    comp = out["comparison"]
    print("\n=== CROSS-BACKEND COMPARISON (M4) ===")
    for name, bdata in out["backends"].items():
        ilp = bdata["ilp"]
        print(f"\n[{name}] measured queries: {bdata['queries_measured']}")
        print(f"  mean baseline q-error -> selected mean q-error: "
              f"{_mean_base(bdata):.2f} -> {ilp['mean_qerror']:.2f} "
              f"({_mean_base(bdata)/ilp['mean_qerror']:.2f}x better)")
        print(f"  selected stats ({len(ilp['selected_stats'])}):")
        for ps in ilp["selected_stats"]:
            print(f"    {ps['table']}{tuple(ps['columns'])} "
                  f"L{ps['level']} cost={ps['cost_bytes']}B")
    if "agreement" in comp:
        ag = comp["agreement"]
        print("\n[agreement] selected column-sets equal across backends: ",
              ag["column_sets_equal"])
        print("  shared:", ag["shared_count"],
              "only_pg:", ag.get("only_in_postgres"),
              "only_or:", ag.get("only_in_oracle"))
    return 0


def _mean_base(bdata):
    bs = [r["qerror_base"] for r in bdata["phase1"]]
    return sum(bs) / len(bs) if bs else 0.0


if __name__ == "__main__":
    sys.exit(_main())
