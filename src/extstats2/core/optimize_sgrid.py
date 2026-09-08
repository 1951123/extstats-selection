"""Optimizer consumer for the per-Sampling-level (S-grid) premeasure output.

Reads ``results/measure/<workload>/<backend>/`` (per-query ``by_lambda`` files
+ ``_meta.json``) and for each sampling level ``S`` assembles the *inner*
selection problem:
   - the level's no-ext baseline ``qbase_i = by_lambda[level].baseline.qerror``
     (same-S reference), and
   - candidate readings ``(colset, param) → qerror`` offered at that level (params
     already bounded by ``p <= S/300`` at measure time).
It then solves the sparse one-stat-sufficiency MILP (per-query cap 1) under a
storage budget for that level, and reports the achievable mean q-error + selected
stats. The outer loop over sampling levels ``S`` (adding ``Σ_t ρ_t f_t(S)``
per-table fixed cost) is layered on top by the caller / a convenience search here.

λ is derived per table as ``min(S, N_t)/N_t`` and is a reporting metric, not the
axis searched here (the axis is ``S ∈ SAMPLING_LEVELS``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from .measure_io import list_qids, read_meta, read_query_measure, result_dir
from .optimize import (Option, OptimizerClass, PhysicalStat, solve_ilp)


def load_sgrid_problem(outdir: Path, workload: str, backend: str):
    """Load a workload's saved per-sampling-level results as (meta, per_query_blocks)."""
    d = result_dir(outdir, workload, backend)
    meta = read_meta(d)
    blocks = {}
    for qid in list_qids(d):          # skips _meta.json and _maint.json
        b = read_query_measure(d, qid)
        if b is not None:
            blocks[qid] = b
    return meta, blocks


def _col_identity(cand: dict):
    """Canonical key for a candidate row's physical stat: its sorted columns."""
    return tuple(sorted(cand["cols"]))


def _lambda_q_of_slot(block: dict, level: str) -> Optional[float]:
    """Return this (query, sampling level)'s stored ``lambda_q`` (= actual·λ),
    or None if the slot has no candidate carrying it.

    ``lambda_q`` is a per-(query, level) scalar — identical across every
    candidate of the slot — so we read it once from the first candidate.
    """
    slot = block.get("by_lambda", {}).get(level)
    if not slot:
        return None
    cands = slot.get("candidates") or []
    for cd in cands:
        lq = cd.get("lambda_q")
        if lq is not None:
            try:
                return float(lq)
            except (TypeError, ValueError):
                return None
    return None


def _fidelity_weight(lambda_q: Optional[float], floor: Optional[float]) -> float:
    """Truncation-linear fidelity confidence (model.md §3a, default off).

    ``floor`` None (default) => w=1 (identiy — no behavior change). Otherwise
    ``w = min(1, lambda_q/floor)``: lambda_q >= floor => fully credible (w=1);
    lambda_q -> 0 => not credited (w->0). Missing ``lambda_q`` => w=1.
    """
    if floor is None or lambda_q is None or lambda_q < 0:
        return 1.0
    if lambda_q >= floor:
        return 1.0
    if floor <= 0:
        return 1.0
    return max(0.0, min(1.0, lambda_q / floor))


def build_inner_at_level(
    blocks: dict,
    level: str,
    *,
    skip_worse_than_baseline: bool = True,
    qid_table: Optional[dict] = None,
    fidelity_floor: Optional[float] = None,
    out_weights: Optional[list] = None,
) -> tuple[list, list, list]:
    """Build (phys_stats, queries_options, qbase_per_query) for one sampling
    level ``level``.

    Mirrors :func:`optimize.build_problem` per sampling level: the baseline for
    each query is that level's no-ext baseline, and physical stats are (table,
    colset) each at a representation ``param``.

    NOTE on ``PhysicalStat.level``: here it carries the REPRESENTATION parameter
    ``p`` (e.g. 25/50/100...), NOT the outer sampling level ``L`` (0/1). The
    outer loop over ``L`` selects which ``by_lambda[level]`` slot we feed in;
    within a slot ``PhysicalStat.level = param`` just distinguishes candidate
    objects by their representation cost. The generic kernel treats ``level`` as
    an opaque discrete index.

    ``qid_table`` (optional): ``{qid: table}`` for multi-table workloads. The
    slots carry candidate *columns* but not a per-row table, so without it all
    stats are tagged ``table=""`` (single-table assumption). When supplied, each
    candidate's table is taken from its owning query and physical stats are keyed
    by ``(table, cols, param)`` so a multi-table problem gets per-table stats (and
    per-table measured maintenance can be attached).

    Fidelity soft-penalty (model.md §3a, default off): ``fidelity_floor`` = k
    (None => off => no weights). When on, per kept query we compute its
    (query, level) fidelity confidence ``w = min(1, lambda_q/k)`` from the
    slot's ``lambda_q`` and, if ``out_weights`` is supplied, append ``w`` in
    LOCK-STEP with ``queries_options`` so the caller can pass
    ``query_weight`` to :func:`optimize.solve_ilp`. ``out_weights`` is a
    side-channel to avoid changing this function's 3-tuple return contract for
    existing callers.
    """
    stat_index: dict[str, int] = {}
    phys_stats: list[PhysicalStat] = []
    queries_options: list[list[Option]] = []
    qbase_list: list[float] = []

    for qid, block in blocks.items():
        # A query with true cardinality 0 has no well-defined q-error (est/0);
        # drop it from the optimization problem (only DMV hits this, 2/1926).
        if block.get("actual", 1) == 0:
            continue
        slot = block["by_lambda"].get(level)
        if slot is None:
            continue
        base = float(slot["baseline"]["qerror"])
        if base != base:      # NaN baseline (e.g. est/0 when truth==0) -> skip
            continue
        qbase_list.append(base)
        if out_weights is not None:
            out_weights.append(
                _fidelity_weight(_lambda_q_of_slot(block, level), fidelity_floor)
            )
        qtable = (qid_table or {}).get(qid, "")
        opts: list[Option] = []
        for cd in slot.get("candidates", []):
            cols = tuple(sorted(cd["cols"]))
            param = int(cd["param"])
            qerr = float(cd["qerror"])
            if skip_worse_than_baseline and qerr >= base:
                continue
            table = qtable
            # key by (table, cols, param) so different tables never share a stat
            key = f"{table}|{'|'.join(cols)}|P{param}"
            if key not in stat_index:
                stat_index[key] = len(phys_stats)
                phys_stats.append(PhysicalStat(
                    table=table, columns=cols,
                    level=param, cost=int(cd["size_bytes"]),
                    maint_cost=float(cd.get("maint_var", 0.0)),
                ))
            opts.append(Option(stat_index=stat_index[key],
                               qerror=qerr, level=param,
                               query=qid, cand="|".join(cols)))
        queries_options.append(opts)

    return phys_stats, queries_options, qbase_list


def inner_optimal_at_level(blocks, level, budget_bytes, *,
                           maint_budget: Optional[float] = None,
                           fidelity_floor: Optional[float] = None,
                           ) -> tuple[Optional[ILPResult], list, list]:
    """Solve the inner selection at one sampling level under a storage
    (``budget_bytes``) and, optionally, a maintenance budget (``maint_budget``)
    hard constraint.

    Always the cap=1 / exact arithmetic-mean formulation (SPARSE_LINEAR); the
    optimization objective is fixed — no objective switch here.

    With ``maint_budget=None`` the maintenance cost is reported but NOT enforced
    (counted in ``res.total_maint``). When set, ``sum_s maint_cost(s)*y_s <= M``
    is added (additive VAR model; the per-table FIXED component is handled
    separately at the outer / MaintProfile layer).

    Fidelity soft-penalty (model.md §3a): ``fidelity_floor`` k None (default) =>
    off (weights all 1, identical to before); a finite k turns on the
    truncation-linear confidence weight ``w=min(1, lambda_q/k)`` per query and
    passes ``query_weight`` to the solver (credits a query's benefit only by w;
    reported q-error decode stays on true Deltas).
    """
    w_accum: list[float] = []
    phys, opts, qbases = build_inner_at_level(
        blocks, level, fidelity_floor=fidelity_floor, out_weights=w_accum)
    if not opts:
        return None, phys, qbases
    res = solve_ilp(phys, opts, qbases, budget_bytes,
                    maint_budget=maint_budget,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1,
                    query_weight=w_accum if fidelity_floor is not None else None)
    return res, phys, qbases


def search_sgrid(outdir: Path, workload: str, backend: str,
                  budget_bytes: int, *, maint_budget: Optional[float] = None,
                  fixed_per_table: Optional[dict] = None,
                  rho: float = 0.0,
                  fidelity_floor: Optional[float] = None) -> dict:
    """Outer search over the S-grid of sampling levels: for each level ``L``
    solve the inner MILP under ``budget_bytes`` (storage) and (if given)
    ``maint_budget`` (maintenance hard cap), and additionally add
    ``Σ_t ρ·f_t(L)`` per-table fixed if rho/fixed given.

    ``fidelity_floor`` (k, default None = off) enables the fidelity soft-penalty
    (model.md §3a): a finite k threads per-query ``w=min(1,lambda_q/k)`` into
    each level's inner solve.

    Returns per-level outcome rows: {level, mean_qerror(baseline), mean_qerror(deployed),
    n_selected, total_bytes, total_maint, selected_summary}.
    """
    meta, blocks = load_sgrid_problem(outdir, workload, backend)
    levels = [str(t.level) for t in (meta.tiers if meta else [])]
    out: dict[str, dict] = {}
    for level in levels:
        res, phys, qbases = inner_optimal_at_level(
            blocks, level, budget_bytes, maint_budget=maint_budget,
            fidelity_floor=fidelity_floor)
        if res is None:
            out[level] = {"status": "no-candidates", "baseline_mean": float(np.mean(qbases)) if qbases else None}
            continue
        # per-query baseline mean at this sampling level
        base_mean = float(np.mean([b for b in qbases if b == b]))
        # total per-table fixed = sum over distinct tables of f_t(L);
        # (single-table scope today: all rows share one table here)
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
