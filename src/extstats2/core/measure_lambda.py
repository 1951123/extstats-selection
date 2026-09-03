"""Sampling-first (λ-first) measurement driver (§7bis).

For each query, for each λ-tier ``level``:
  1. ``backend.enter_lambda_state(table, level)``  -> all single columns at
     ``S/300``, no extended stat  => measure the per-λ no-ext baseline ``e^0(S)``.
  2. For each candidate (colset) and each representation param ``p <= S/300``:
     create an MCV object, set its target to ``p``, ANALYZE (the established
     single-col λ-state already forces the deep shared scan), and record
     estimate / q-error / size / per-stat maintenance / fidelity.

This gives, per query, a ``by_lambda`` dict whose outer key is the λ tier and
each slot carries BOTH its no-ext baseline and the candidate readings measured in
that same λ-state — the same-``S`` fair pairing the optimizer consumes.

Results are written **one JSON file per query** via
:mod:`extstats2.core.measure_lambda_io`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..backend.base import Backend, StatObject
from ..backend.capabilities import Capacity
from .candidates import CandidateSet
from .measure_lambda_io import (LambdaTier, Meta, result_dir, write_meta,
                                write_query_measure)
from .queries import BenchQuery


# Lambda tiers (scan-depth levels) and ext-stat representation-param tiers are
# TWO INDEPENDENT axes, joined only by the lattice cap ``p <= S_lambda/300``.
# Lambda levels come from the backend ladder (= target -> S); the param grid is a
# separate set of representation values the OR optimizes per (colset, lambda).
# A low representation param (e.g. 25/50: v1 found ext representation matters at
# low detail) is offered at ANY lambda whose cap S/300 >= p -- NOT by pushing
# lambda below 30000 (that would drive single columns below the default 100 and
# degrade the base/fidelity).
#
# NOTE (2026-09-03): the param grid is NO LONGER a single cross-backend list.
# It is per-backend representation sampling points (PG = its native scalar
# ``attstattarget`` range; Oracle = an engine-faithful tiny grid, see
# ``Backend.representation_param_tiers``). ``DEFAULT_PARAM_TIERS`` is kept only
# as a PG-flavoured fallback; callers should pass ``param_tiers=None`` to use
# the active backend's own grid.
DEFAULT_PARAM_TIERS: tuple[int, ...] = (25, 50, 100, 1000, 10000)

#: Active experiment λ-tiers during the fast-measurement phase. L2 (full scan,
#: ~22 s/gather on Oracle) is dropped for now to keep experiments fast; it can be
#: restored later by passing ``levels=(0, 1, 2)`` when a deterministic absolute
#: baseline / final paper figures are needed.
DEFAULT_LAMBDA_LEVELS: tuple[int, ...] = (0, 1)


def _primary_capability(backend: Backend):
    caps = [c for c in backend.supported_capabilities()
            if c.supported and c.name == "mcv"]
    if not caps:
        # fall back to any supported (primary-ish) capability
        caps = [c for c in backend.supported_capabilities() if c.supported]
    return caps[0]


def _resolve_param_tiers(backend: Backend,
                         param_tiers: Optional[tuple[int, ...]],
                         table: str) -> tuple[int, ...]:
    """Resolve the representation-param grid: caller override, else per-backend."""
    if param_tiers is not None:
        return tuple(param_tiers)
    return tuple(backend.representation_param_tiers(table) or ())


def measure_query_lambda(
    backend: Backend,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    levels: tuple[int, ...] = DEFAULT_LAMBDA_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    outdir: Optional[Path] = None,
) -> dict:
    """Measure ``query`` across λ tiers; return its ``by_lambda`` block and, if
    ``outdir`` given, write it as ``<outdir>/<qid>.json``.

    ``param_tiers=None`` uses the active backend's own representation grid
    (:meth:`Backend.representation_param_tiers`); levels default to
    :data:`DEFAULT_LAMBDA_LEVELS` (L2 off during the fast-experiment phase)."""
    cap_obj = _primary_capability(backend)
    by_lambda: dict[str, dict] = {}
    # Determine the query table from the first candidate (single-table benchs).
    table = candidates[0].table if candidates else ".climate"
    pgrid = _resolve_param_tiers(backend, param_tiers, table)

    for level in levels:
        rows = backend.lambda_sampling_rows(table, level)
        single_tgt = backend.single_col_target_for_level(table, level)
        cap_p = backend.max_param_at_level(table, level)

        # 1) per-λ no-ext baseline in the λ-state
        backend.enter_lambda_state(table, level)
        # ensure no leftover extended stats silently affect the baseline
        for s in list(backend.list_stats(table)):
            backend.drop_stat(s)
        be = backend.estimate(query)
        baseline = {"estimate": be.estimate,
                    "qerror": be.qerror if be.qerror is not None else float("nan")}

        slot_cands = []
        n = backend.num_rows(table) or 1.0
        # 2) candidates within param <= cap
        for cand in candidates:
            for p in pgrid:
                if cap_p is not None and p > cap_p:
                    continue  # Direction B: param above this λ's lattice cap
                # name unique per (table, cols, level, param)
                cols_t = "_".join(str(c).lower() for c in cand.columns)
                name = f"ext_m_{table.rpartition('.')[2]}_{cols_t}_l{level}_{p}"
                obj = StatObject(table=cand.table, columns=cand.columns,
                                 capability=cap_obj, capacity=Capacity(level,
                                 label=f"L{level}-p{p}"),
                                 name=name)
                backend.create_stat(obj)
                try:
                    backend.build_stat_param(obj, p)
                    est = backend.estimate(query)
                    size = backend.stat_size_bytes(obj)
                    mv = backend.stat_maintain_var(obj)
                    lam_q = (query.ground_truth / n) * (rows or 0.0) \
                        if query.ground_truth else None
                    slot_cands.append({
                        "cols": list(cand.columns), "param": p,
                        "estimate": est.estimate,
                        "qerror": est.qerror if est.qerror is not None else float("nan"),
                        "lambda_q": lam_q,
                        "size_bytes": size, "maint_var": mv,
                    })
                finally:
                    backend.drop_stat(obj)

        by_lambda[str(level)] = {
            "S_rows": rows, "single_target": single_tgt,
            "baseline": baseline, "candidates": slot_cands,
        }

    block = {"qid": query.qid, "actual": query.ground_truth,
             "by_lambda": by_lambda}
    if outdir is not None:
        write_query_measure(outdir, block)
    return block


def measure_query_lambda_m(
    backend: Backend,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    levels: tuple[int, ...] = DEFAULT_LAMBDA_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    outdir: Optional[Path] = None,
) -> dict:
    """Protocol-M (catalog-mask) per-λ measurement; same output shape as
    ``measure_query_lambda`` but de-amortizes the fixed scan.

    For each λ tier: ``enter_lambda_state`` gives the no-ext baseline ``e^0`` in
    the S_λ state; then ALL (candidate, param) objects are created and built in a
    SINGLE ANALYZE (``build_stat_params_batch`` — each object set to its own
    param, shared by the established S_λ scan). Each (candidate, param) is then
    read by masking every other object's payload (Protocol-M), rather than
    re-ANALYZE per candidate (Protocol-A). Same-sample mask == drop is verified,
    so this measures the same q-error with far fewer scans (per-λ once, not per
    candidate).

    Same-sample mask == drop is verified
    (repo memory 2026-09-03; q.184 M==A est/qerr exactly), so this measures the
    same state/per-λ q-error as Protocol-A but with ~1 scan per λ instead of
    #(cand,param) scans.

    Deterministic per-λ: each tier uses its OWN S_λ scan (NOT a shared max-λ
    scan), so it does NOT reproduce v1 Protocol-M's large-sample bias.
    """
    if not backend.supports_catalog_mask():
        # Not a mask-capable backend: fall back to Protocol-A semantics.
        return measure_query_lambda(backend, query, candidates, levels=levels,
                                    param_tiers=param_tiers, outdir=outdir)

    cap_obj = _primary_capability(backend)
    table = candidates[0].table if candidates else ".climate"
    pgrid = _resolve_param_tiers(backend, param_tiers, table)
    by_lambda: dict[str, dict] = {}
    driver = backend.catalog_driver()

    for level in levels:
        rows = backend.lambda_sampling_rows(table, level)
        single_tgt = backend.single_col_target_for_level(table, level)
        cap_p = backend.max_param_at_level(table, level)

        # 1) per-λ no-ext baseline in the λ-state
        backend.enter_lambda_state(table, level)
        for s in list(backend.list_stats(table)):
            backend.drop_stat(s)
        be = backend.estimate(query)
        baseline = {"estimate": be.estimate,
                    "qerror": be.qerror if be.qerror is not None else float("nan")}

        # 2) materialize every (cand, param) object that fits the lattice cap
        objs: list[StatObject] = []
        for cand in candidates:
            for p in pgrid:
                if cap_p is not None and p > cap_p:
                    continue
                cols_t = "_".join(str(c).lower() for c in cand.columns)
                name = f"ext_m_{table.rpartition('.')[2]}_{cols_t}_l{level}_{p}"
                obj = StatObject(table=cand.table, columns=cand.columns,
                                 capability=cap_obj, capacity=Capacity(level,
                                 label=f"L{level}-p{p}"),
                                 name=name)
                backend.create_stat(obj)
                objs.append((obj, p))

        bkp = None
        try:
            # ONE ANALYZE builds ALL objects from this λ's S_λ scan (Protocol-M)
            backend.build_stat_params_batch(objs)
            n = backend.num_rows(table) or 1.0
            all_objs = [o for o, _ in objs]
            bkp = driver.backup_payloads(all_objs)
            slot_cands = []
            for obj, p in objs:
                bkp.mask_all_but({obj})         # NULL every other object
                try:
                    est = backend.estimate(query)
                    size = backend.stat_size_bytes(obj)
                    mv = backend.stat_maintain_var(obj)
                    lam_q = (query.ground_truth / n) * (rows or 0.0) \
                        if query.ground_truth else None
                    slot_cands.append({
                        "cols": list(obj.columns), "param": p,
                        "estimate": est.estimate,
                        "qerror": est.qerror if est.qerror is not None else float("nan"),
                        "lambda_q": lam_q,
                        "size_bytes": size, "maint_var": mv,
                    })
                finally:
                    bkp.restore()
        finally:
            if bkp is not None:
                try:
                    bkp.close()
                except Exception:
                    pass
            for obj, _ in objs:
                try:
                    backend.drop_stat(obj)
                except Exception:
                    pass

        by_lambda[str(level)] = {
            "S_rows": rows, "single_target": single_tgt,
            "baseline": baseline, "candidates": slot_cands,
        }

    block = {"qid": query.qid, "actual": query.ground_truth,
             "by_lambda": by_lambda}
    if outdir is not None:
        write_query_measure(outdir, block)
    return block


def measure_workload_lambda(
    backend: Backend,
    queries: list[BenchQuery],
    cands_by_q: dict[str, list[CandidateSet]],
    *,
    levels: tuple[int, ...] = DEFAULT_LAMBDA_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    workload: str = "default",
    outdir: Path,
) -> None:
    """Measure a workload into ``<outdir>/per_lambda/<workload>/``.

    Writes ``_meta.json`` + one ``<qid>.json`` per query, namespaced by workload
    and backend (``<outdir>/per_lambda/<workload>/<backend>/``) so neither workload
    nor DBMS engine (which may hold the same columns but different native params)
    collide. ``param_tiers=None`` records the active backend's own representation
    grid; levels default to :data:`DEFAULT_LAMBDA_LEVELS` (L2 off).
    """
    dest = result_dir(outdir, workload, backend.name())
    dest.mkdir(parents=True, exist_ok=True)
    # meta describing the λ tiers actually realized (per the first table's row
    # count via a reference table; native params recorded by level).
    tiers: list[LambdaTier] = []
    table = None
    for cl in cands_by_q.values():
        if cl:
            table = cl[0].table
            break
    # Record the effective representation grid in _meta (backend-native unless
    # the caller explicitly overrode it).
    if table is not None:
        meta_pgrid = _resolve_param_tiers(backend, param_tiers, table)
    else:
        meta_pgrid = tuple(param_tiers) if param_tiers is not None else ()
    for level in levels:
        if table is not None:
            single_tgt = backend.single_col_target_for_level(table, level)
            rows = backend.lambda_sampling_rows(table, level)
            ep = backend.lambda_sampling_percent(table, level)
        else:
            single_tgt = rows = ep = None
        tiers.append(LambdaTier(level=level, S_rows=rows,
                                single_target=single_tgt, estimate_percent=ep))
    write_meta(dest, Meta(bench=workload, backend=backend.name(), tiers=tiers,
                          param_tiers=meta_pgrid))

    for query in queries:
        cands = cands_by_q.get(query.qid, [])
        measure_query_lambda(backend, query, cands, levels=levels,
                             param_tiers=param_tiers, outdir=dest)
