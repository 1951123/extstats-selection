"""Optimizer consumer for the per-λ (sampling-first) premeasure output (§7bis).

Reads ``results/per_lambda/<workload>/<backend>/`` (per-query ``by_lambda`` files
+ ``_meta.json``) and for each λ tier assembles the *inner* selection problem:
   - per-λ no-ext baseline ``qbase_i = by_lambda[level].baseline.qerror`` (same-S
     reference), and
   - candidate readings ``(colset, param) → qerror`` offered at that λ (params
     already bounded by ``p <= S_λ/300`` at measure time).
It then solves the sparse one-stat-sufficiency MILP (per-query cap 1) under a
storage budget for that λ, and reports the achievable mean q-error + selected
stats. The outer loop over λ (adding ``Σ_t ρ_t f_t(λ)`` per-table fixed cost) is
layered on top by the caller / a convenience search here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from .measure_lambda_io import read_meta, read_query_measure, result_dir
from .optimize import (Option, OptimizerClass, PhysicalStat, solve_ilp)

OBJECTIVE_MEAN = "mean"


def load_lambda_problem(outdir: Path, workload: str, backend: str):
    """Load a workload's saved per-λ results as (meta, per_query_blocks)."""
    d = result_dir(outdir, workload, backend)
    meta = read_meta(d)
    blocks = {}
    for qid in sorted(p.stem for p in d.glob("*.json") if p.stem != "_meta"):
        b = read_query_measure(d, qid)
        if b is not None:
            blocks[qid] = b
    return meta, blocks


def _col_identity(cand: dict):
    """Canonical key for a candidate row's physical stat: its sorted columns."""
    return tuple(sorted(cand["cols"]))


def build_inner_at_level(
    blocks: dict,
    level: str,
    *,
    skip_worse_than_baseline: bool = True,
) -> tuple[list, list, list]:
    """Build (phys_stats, queries_options, qbase_per_query) for one λ ``level``.

    Mirrors :func:`optimize.build_problem` but per-λ: the baseline for each query
    is that λ's no-ext baseline, and physical stats are (table, colset) each at a
    representation ``param`` (quantised into ``PhysicalStat.level`` = the param).
    """
    stat_index: dict[str, int] = {}
    phys_stats: list[PhysicalStat] = []
    queries_options: list[list[Option]] = []
    qbase_list: list[float] = []

    for qid, block in blocks.items():
        slot = block["by_lambda"].get(level)
        if slot is None:
            continue
        base = float(slot["baseline"]["qerror"])
        qbase_list.append(base)
        opts: list[Option] = []
        for cd in slot.get("candidates", []):
            cols = tuple(sorted(cd["cols"]))
            param = int(cd["param"])
            qerr = float(cd["qerror"])
            if skip_worse_than_baseline and qerr >= base:
                continue
            table = ""  # candidates carry cols; table not stored per row -> derive from columns only (single-table bench)
            # NB: single-table benchmark => table not in row; treat as one table.
            key = f"{'|'.join(cols)}|P{param}"
            if key not in stat_index:
                stat_index[key] = len(phys_stats)
                phys_stats.append(PhysicalStat(
                    table=table if table else "", columns=cols,
                    level=param, cost=int(cd["size_bytes"]),
                    maint_cost=float(cd.get("maint_var", 0.0)),
                ))
            opts.append(Option(stat_index=stat_index[key],
                               qerror=qerr, level=param,
                               query=qid, cand="|".join(cols)))
        queries_options.append(opts)

    return phys_stats, queries_options, qbase_list


def inner_optimal_at_level(blocks, level, budget_bytes, *,
                           objective=OBJECTIVE_MEAN,
                           maint_budget: Optional[float] = None,
                           ) -> tuple[Optional[ILPResult], list, list]:
    """Solve the inner selection at one λ under a storage (``budget_bytes``)
    and, optionally, a maintenance budget (``maint_budget``) hard constraint.

    With ``maint_budget=None`` the maintenance cost is reported but NOT enforced
    (counted in ``res.total_maint``). When set, ``sum_s maint_cost(s)*y_s <= M``
    is added (additive VAR model; the per-table FIXED component is handled
    separately at the outer / MaintProfile layer).
    """
    phys, opts, qbases = build_inner_at_level(blocks, level)
    if not opts:
        return None, phys, qbases
    res = solve_ilp(phys, opts, qbases, budget_bytes,
                    maint_budget=maint_budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1, objective=objective)
    return res, phys, qbases


def search_lambda(outdir: Path, workload: str, backend: str,
                  budget_bytes: int, *, maint_budget: Optional[float] = None,
                  fixed_per_table: Optional[dict] = None,
                  rho: float = 0.0) -> dict:
    """Outer search over λ: for each tier solve the inner MILP under
    ``budget_bytes`` (storage) and (if given) ``maint_budget`` (maintenance hard
    cap), and additionally add Σ_t ρ·f_t(λ) per-table fixed if rho/fixed given.

    Returns per-level outcome rows: {level, mean_qerror(baseline), mean_qerror(deployed),
    n_selected, total_bytes, total_maint, selected_summary}.
    """
    meta, blocks = load_lambda_problem(outdir, workload, backend)
    levels = [str(t.level) for t in (meta.tiers if meta else [])]
    out: dict[str, dict] = {}
    for level in levels:
        res, phys, qbases = inner_optimal_at_level(
            blocks, level, budget_bytes, maint_budget=maint_budget)
        if res is None:
            out[level] = {"status": "no-candidates", "baseline_mean": float(np.mean(qbases)) if qbases else None}
            continue
        # per-query baseline mean at this λ
        base_mean = float(np.mean([b for b in qbases if b == b]))
        # total per-table fixed = sum over distinct tables of f_t(λ) (all rows share one table here)
        n_tables = len({p.table for p in phys}) if any(p.table for p in phys) else 1
        fixed = 0.0
        if fixed_per_table is not None and level in fixed_per_table:
            fixed = float(fixed_per_table[level]) * (n_tables or 1) * rho
        out[level] = {
            "baseline_mean": base_mean,
            "deployed_mean": res.mean_qerror,
            "total_with_fixed": float(res.mean_qerror) + fixed,
            "budget_bytes": budget_bytes,
            "maint_budget": maint_budget,
            "n_selected": len(res.selected_stats),
            "total_bytes": res.total_bytes,
            "total_maint": res.total_maint,
            "qerror_per_query": res.qerror_per_query,
            "selected": [f"{'|'.join(p.columns)}:P{p.level}" for p in res.selected_stats],
        }
    return out
