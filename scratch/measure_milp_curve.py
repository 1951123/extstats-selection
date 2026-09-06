"""Unified MILP "budget vs quality" sweep over the S-grid corpora (per backend).

One runner, one JSON schema, for both budget kinds, over all three benches
(census=climate, dmv=dmv single-table; stats_ceb_single multi-table) and either
backend (postgres|oracle; corpus per results/measure/{bench}/{backend}/).

JSON (one file per backend+bench+budget kind):
  {
    "bench", "backend":"postgres",
    "budget": {"kind":"storage"|"maint","unit":"bytes"|"seconds-per-refresh"},
    "levels": ["0","1"],
    "baseline": {"0":{mean,geo,max},"1":{...}},          # no-ext baseline per level
    "fixed_sec": {"0":...,"1":...} | null,               # maint kind only
    "per_level": {"0": [ {budget, mean, geo, max, n_selected, stored_bytes,
                          maint_sec, (opt) n_tables_active, (opt) choose_tables} ... ],
                  "1": [...]},
    "argmin_over_level": [ {budget, choose_level, mean, geo, max, n_selected,
                            stored_bytes, maint_sec} ... ]      # cross-level min
  }
File: results/curves/{backend}/milp_{storage|maint}_sgrid_{bench}.json

All solves: cap=1 one-stat, SPARSE_LINEAR, objective=mean, candidate-bearing set.
  storage : budget = stored-bytes cap (maint unconstrained).
  maint   : budget = refresh-seconds; per refresh each ACTIVATED table pays its
            fixed shared-scan cost once + per-stat var. [PG-modelled; maint on
            oracle is not wired yet — the runner refuses it.]
            - census/dmv single table: fixed[lv] + additive var <= M.
            - stats_ceb_single: per-active-table enumeration (storage unconstrained).
Usage (default backend=postgres):
  .venv/bin/python -u scratch/measure_milp_curve.py --kind storage --bench dmv \
      --grid 2000,5000,20000,50000,100000,200000,400000
  .venv/bin/python -u scratch/measure_milp_curve.py --kind storage --bench stats_ceb_single \
      --backend oracle --grid 2000,5000,20000,50000,100000,200000,400000
  .venv/bin/python -u scratch/measure_milp_curve.py --kind maint --bench census \
      --grid 0.1,0.2,0.3,0.5,1.0,2.0,3.0,4.0,6.0,8.0
"""
from __future__ import annotations
import argparse, json, math, time
from dataclasses import replace
from itertools import combinations
from pathlib import Path
import numpy as np
from extstats2.core.optimize_lambda import load_lambda_problem, build_inner_at_level
from extstats2.core.optimize import solve_ilp, OptimizerClass
from extstats2.core.maint_model import read_maint
from extstats2.bench import load_benchmark
from extstats2.core.predicates import predicate_columns

ROOT = Path(__file__).resolve().parents[1]
HUGE = 10**12
# Maintenance params come ONLY from the measured corpus artifact _maint.json
# (fixed_seconds / c_var per [table][level]). There are NO closed-form fallback
# constants here: applying a maintenance constraint requires measured params
# (else MaintNotMeasuredError). The storage kind needs no maintenance params.

# Per (bench -> owner table): single-table benches have exactly one owner table.
_BENCH_OWNER = {"census": ".climate", "dmv": ".dmv"}


class MaintNotMeasuredError(RuntimeError):
    """Raised when a maintenance cost is requested without measured _maint.json."""


def _load_maint(bench, backend):
    """Measured maintenance params for this (bench, backend). Raises if none.

    Policy (2026-09-06): a maintenance-cost constraint is only allowed after the
    model params have actually been measured (_maint.json). No closed-form
    fallback — if you want to constrain on maintenance, measure it first.
    """
    try:
        mp = read_maint(Path(ROOT) / "results" / "measure", bench, backend)
    except Exception as e:  # pragma: no cover - defensive
        mp = None
    if mp is None:
        raise MaintNotMeasuredError(
            f"maintenance constraint requested for {bench}/{backend} but "
            f"_maint.json is missing (results/measure/{bench}/{backend}/_maint."
            f"json). Measure it first (fit_maint_postgres.py) before using a "
            f"maintenance budget.")
    return mp


def _require_fixed(mp, table, lv) -> float:
    """Measured fixed(t, lv); raises if that (table, level) was not measured.

    Table keys are compared in a canonical *lowercase* form: the corpus
    _maint.json may key camelCase tables (.postHistory/.postLinks) while
    callers/config here use lowercase (.posthistory/.postlinks).
    """
    v = mp.fixed(table.lower(), lv) if table else None
    if v is None:
        raise MaintNotMeasuredError(
            f"fixed(t={table!r}, level={lv}) not measured in _maint.json; "
            f"cannot apply a maintenance constraint without it. Measure it first.")
    return v


def _require_cvar(mp, table, lv) -> float:
    """Measured c_var(t, lv); raises if that (table, level) was not measured."""
    v = mp.var(table.lower(), lv) if table else None
    if v is None:
        raise MaintNotMeasuredError(
            f"c_var(t={table!r}, level={lv}) not measured in _maint.json; "
            f"cannot apply a maintenance constraint without it. Measure it first.")
    return v


def _geo(vals):
    a = np.asarray([float(x) for x in vals], float)
    return float(np.exp(np.mean(np.log(np.maximum(a, 1e-12)))))


def _summ(arr):
    a = np.asarray([float(x) for x in arr if x is not None and x == x], float)
    return {"mean": float(a.mean()), "geo": _geo(a), "max": float(a.max())}


def qtab(q):
    pcs = predicate_columns(q)
    return (".%s" % next(iter(pcs)).lstrip(".").lower()) if len(pcs) == 1 else None


def _solve(phys, opts, qb, storage_b, maint_b):
    return solve_ilp(phys, opts, [float(v) for v in qb], storage_b,
                     maint_budget=maint_b,
                     optimizer_class=OptimizerClass.SPARSE_LINEAR,
                     per_query_cap=1, objective="mean")


def _point(B, res):
    qq = np.asarray([float(v) for v in res.qerror_per_query], float)
    ok = qq[qq == qq]                      # drop NaN
    return {"budget": B,
            "mean": float(np.mean(ok)) if ok.size else None,
            "geo": _geo(ok) if ok.size else None,
            "max": float(np.max(ok)) if ok.size else None,
            "n_selected": len(res.selected_stats),
            "stored_bytes": float(res.total_bytes),
            "maint_sec": float(res.total_maint) if res.total_maint is not None else None}


def _build(blocks, level, qt=None):
    return build_inner_at_level(blocks, level, skip_worse_than_baseline=True,
                                qid_table=qt)


def storage_curve(blocks, level, grid):
    phys, opts, qb = _build(blocks, level)
    rows = []
    for B in grid:
        t0 = time.perf_counter()
        res = _solve(phys, opts, qb, int(B), None)
        p = _point(int(B), res); p["solve_s"] = time.perf_counter() - t0
        rows.append(p)
    return rows


def maint_single(blocks, level, grid, fixed, c_var_per_stat):
    """Single-table maintenance curve: each refresh pays fixed once + per-stat
    c_var (additive over selected stats). ``fixed`` and ``c_var_per_stat`` are
    per-λ values (measured from _maint.json when available)."""
    phys, opts, qb = _build(blocks, level)
    # PhysicalStat is frozen: rebuild with the per-stat measured c_var (order
    # preserved so opts' stat_index references to phys stay valid).
    phys = [replace(p, maint_cost=c_var_per_stat) for p in phys]
    rows = []
    for M in grid:
        if M <= fixed + 1e-9:
            src = baseline_of(blocks, level)
            rows.append({"budget": M, "mean": src["mean"], "geo": src["geo"],
                         "max": src["max"], "n_selected": 0, "stored_bytes": 0.0,
                         "maint_sec": 0.0, "_baseline_only": True})
            continue
        res = _solve(phys, opts, qb, HUGE, float(M - fixed))
        rows.append(_point(M, res))
    return rows


def baseline_of(blocks, level):
    _p, _o, qb = _build(blocks, level)
    return _summ([float(v) for v in qb])


def maint_multitable(blocks, level, grid, mp):
    """stats_CEB_single maintenance curve: enumerates active-table subsets; each
    active table t pays its measured fixed(t, level) once, and each selected stat
    is charged its OWN measured c_var(table(t), level) (via per-query qid->table),
    so the additive var sum over a subset is Σ_t c_var(t,ℓ)·n_t — per-table real.

    Maintenance is only constrained with MEASURED params (_maint.json): fixed and
    c_var come from _require_fixed/_require_cvar which raise if a (table,level)
    is unmeasured (no closed-form). Query-stats with no single-table map are never
    in a table subset, so they are left untouched (never selected).
    """
    Q = load_benchmark("stats_ceb_single")
    qt = {q.qid: qtab(q) for q in Q}
    qids = list(blocks)
    # build with qid->table so each PhysicalStat carries its real owner table
    phys, opts, qb = _build(blocks, level, qt)
    q_table = [qt[q] for q in qids]
    base_by_q = {qid: float(b) for qid, b in zip(qids, qb)}
    # the set of activateable tables = the tables actually MEASURED in _maint.json
    # (canonical lowercase), not a hardcoded list.
    measured_tables = sorted({t.lower() for t in mp.c_var})

    # attach each mapped stat its measured per-table c_var (frozen->replace; keep
    # order so opts indices stay valid); unmapped (table='') stats stay untouched
    # since they never belong to a measured-table subset.
    phys = [replace(p, maint_cost=_require_cvar(mp, p.table, int(level))) if p.table
            else p for p in phys]

    rows = []
    for M in grid:
        best = None
        for r in range(len(measured_tables) + 1):
            for S in combinations(measured_tables, r):
                fS = sum(_require_fixed(mp, t, int(level)) for t in S)
                if fS > M + 1e-9:
                    continue
                keep = [i for i, t in enumerate(q_table) if t in S]
                sub = [opts[i] for i in keep]
                qq_full = np.asarray([base_by_q[q] for q in qids], float)
                if sub:
                    res = _solve(phys, sub, [float(qb[i]) for i in keep], HUGE,
                                 float(M - fS))
                    ach = dict(zip([qids[i] for i in keep], res.qerror_per_query))
                    qq_full = np.asarray([ach[q] if q in ach else base_by_q[q]
                                          for q in qids], float)
                    n_sel = len(res.selected_stats); var = float(res.total_maint or 0.0)
                else:
                    n_sel, var = 0, 0.0
                rm = float(np.mean(qq_full))
                if best is None or rm < best[0] - 1e-12:
                    best = (rm, tuple(sorted(S)), n_sel, var,
                            float(_geo(qq_full)), float(np.max(qq_full)))
        rm, tables, n_sel, var, g, mx = best
        rows.append({"budget": M, "mean": rm, "geo": g, "max": mx,
                     "choose_tables": list(tables), "n_tables_active": len(tables),
                     "n_selected": n_sel, "maint_sec": var})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=["storage", "maint"])
    ap.add_argument("--bench", required=True,
                    choices=["census", "dmv", "stats_ceb_single"])
    ap.add_argument("--backend", default="postgres", choices=["postgres", "oracle"])
    ap.add_argument("--grid", required=True)
    a = ap.parse_args()
    grid = [float(x) for x in a.grid.split(",") if x.strip()]
    kind, bench, backend = a.kind, a.bench, a.backend
    if kind == "maint" and backend == "oracle":
        raise SystemExit(
            "maint+oracle not wired yet: the maint cost legs here are PG-modeled "
            "(PG ANALYZE fixed/var). Oracle maint must consume "
            "oracle.table_maintain_tiers/stat_maintain_var; run storage first.")
    blocks = load_lambda_problem(ROOT / "results" / "measure", bench,
                                 backend)[1]
    # measured maintenance params are REQUIRED for a maint constraint; _load_maint
    # raises if _maint.json is absent (no closed-form fallback).
    mp = _load_maint(bench, backend) if kind == "maint" else None

    baseline, per_level = {}, {}
    owner = _BENCH_OWNER.get(bench)
    for lv in ("0", "1"):
        baseline[lv] = baseline_of(blocks, lv)
        iv = int(lv)
        if kind == "storage":
            rows = storage_curve(blocks, lv, grid)
        elif bench == "stats_ceb_single":
            rows = maint_multitable(blocks, lv, grid, mp)
        else:
            fixed = _require_fixed(mp, owner, iv)
            cvar = _require_cvar(mp, owner, iv)
            rows = maint_single(blocks, lv, grid, fixed, cvar)
        per_level[lv] = rows

    argmin = []
    for i in range(len(grid)):
        cand = []
        for lv in ("0", "1"):
            r = per_level[lv][i]
            m = r.get("mean") if not r.get("_baseline_only") else baseline[lv]["mean"]
            cand.append((lv, m))
        lv, m = min(cand, key=lambda x: (x[1] if x[1] is not None else math.inf))
        r = per_level[lv][i]
        def take(k, fb):
            return r.get(k) if r.get(k) is not None else fb
        row = {"budget": grid[i], "choose_level": lv, "mean": m if m is not None else baseline[lv]["mean"],
               "geo": take("geo", baseline[lv]["geo"]),
               "max": take("max", baseline[lv]["max"]),
               "n_selected": r.get("n_selected", 0),
               "stored_bytes": r.get("stored_bytes", 0.0) or 0.0,
               "maint_sec": r.get("maint_sec", 0.0) or 0.0}
        if "choose_tables" in r:
            row["choose_tables"] = r["choose_tables"]
            row["n_tables_active"] = r["n_tables_active"]
        argmin.append(row)

    unit = "bytes" if kind == "storage" else "seconds-per-refresh"
    # report the per-level fixed actually used (measured owner fixed for single-table)
    fixed_sec_out = None
    if kind == "maint" and bench != "stats_ceb_single" and owner:
        fixed_sec_out = {str(lv): _require_fixed(mp, owner, int(lv)) for lv in (0, 1)}
    out = {"bench": bench, "backend": backend,
           "budget": {"kind": kind, "unit": unit},
           "levels": ["0", "1"], "baseline": baseline,
           "fixed_sec": fixed_sec_out,
           "per_level": per_level, "argmin_over_level": argmin}
    # Per-backend subtree (PG and Oracle curves never collide):
    odir = ROOT / "results" / "curves" / backend
    odir.mkdir(parents=True, exist_ok=True)
    of = odir / f"milp_{kind}_sgrid_{bench}.json"
    of.write_text(json.dumps(out, indent=1))

    print(f"{bench} / {kind} (unit={unit})")
    for lv in ("0", "1"):
        print(f"  baseline L{lv} mean={baseline[lv]['mean']:.3f} geo={baseline[lv]['geo']:.3f}")
    for row in argmin:
        print(f"  b={row['budget']:>9}: L{row['choose_level']} "
              f"mean={row['mean']:.4f} geo={row['geo']:.4f} "
              f"sel={row['n_selected']:>4} tables={row.get('n_tables_active','-')}")
    print("wrote", of)


if __name__ == "__main__":
    main()
