"""Sample-first (S-grid) measurement driver (§7bis).

The PRIMARY axis is the requested sampling level ``S`` (config ``SAMPLING_LEVELS``).
Per table the realized scan is ``min(S, N_t)``; the fraction
``lambda = min(S, N_t)/N_t`` is DERIVED (a reporting/fidelity metric), not a
search axis.

For each query, for each sampling level ``level``:
  1. ``backend.enter_sampling_state(table, level)``  -> all single columns at
     ``S/300``, no extended stat  => measure the per-level no-ext baseline ``e^0(S)``.
  2. For each candidate (colset) and each representation param ``p <= S/300``:
     create an MCV object, set its target to ``p``, ANALYZE (the established
     single-column sampling state already forces the deep shared scan), and record
     estimate / q-error / size / per-stat maintenance / fidelity.

This gives, per query, a ``by_lambda`` dict keyed by sampling level; each slot
carries BOTH its no-ext baseline and the candidate readings measured in that
same level's sampling state — the same-``S`` fair pairing the optimizer consumes.

NOTE on ``maint_var`` (2026-09-06): each slot's ``maint_var`` is currently a
*placeholder* — the backend's closed-form model constant (PG: ~0.002 at L0 /
~0.02 at L1 per 2-col stat; Oracle: a flat ``_VAR_PER_STAT_S``), NOT a timed
per-extstat measurement. It is deliberately kept as a first-class per-slot field
so a future real per-extstat measurement (e.g. timing the marginal refresh of one
(``colset``, ``param``) object) can replace it as a drop-in at the single
``mv = backend.stat_maintain_var(obj)`` seam inside each measure driver. Until
then, treat ``maint_var`` as a model estimate without measured meaning — **it is
never read by the maintainance curves**: those drive the maint axis purely from
the measured ``c_var``/``fixed`` in ``_maint.json`` (see docs/measure.md §3.1),
overriding any corpus ``maint_var``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..backend.base import Backend, StatObject
from ..backend.capabilities import SamplingLevel
from .candidates import CandidateSet
from .measure_io import (SampleTier, Meta, result_dir, write_meta,
                                write_query_measure)
from .queries import BenchQuery


# Sampling levels (requested sample rows S) and ext-stat representation-param
# values (p) are TWO INDEPENDENT axes, joined only by the lattice cap
# ``p <= S/300``. The PRIMARY axis is the sampling level L (config
# ``SAMPLING_LEVELS``): per table the realized scan is ``min(S_L, N_t)`` and the
# fraction ``lambda = min(S_L, N_t)/N_t`` is DERIVED (a reporting metric), not a
# search axis. The representation-param grid is a separate set of values the OR
# optimizes per (colset, level).
# A low representation param (e.g. 25/50: v1 found ext representation matters at
# low detail) is offered at ANY sampling level whose cap S/300 >= p -- NOT by
# choosing a scan below 30000 rows (that would drive single columns below the
# default 100 and degrade the base/fidelity).
#
# The representation-param grid is per-backend points (PG = its native scalar
# ``attstattarget`` range; Oracle = an engine-faithful tiny grid, see
# ``Backend.representation_param_tiers``). Callers pass ``param_tiers=None`` to
# use the active backend's own grid. The grid is dense (not pre-trimmed from
# hindsight): each level caps it via S/300 and the optimizer dominance-prunes
# synonyms at solve time.

#: Active experiment sampling levels (indices into the S-grid). The two levels
#: L0/L1 realize requested sample rows 30k / 300k (see config ``SAMPLING_LEVELS``);
#: the canonical drivers in scratch/ pass these by default.
DEFAULT_SAMPLING_LEVELS: tuple[int, ...] = (0, 1)


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


def measure_query_sampling(
    backend: Backend,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    levels: tuple[int, ...] = DEFAULT_SAMPLING_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    outdir: Optional[Path] = None,
) -> dict:
    """Measure ``query`` across sampling levels; return its ``by_lambda`` block
    and, if ``outdir`` given, write it as ``<outdir>/<qid>.json``.

    ``param_tiers=None`` uses the active backend's own representation grid
    (:meth:`Backend.representation_param_tiers`); levels default to
    :data:`DEFAULT_SAMPLING_LEVELS` (the L0/L1 scan-depth tiers)."""
    cap_obj = _primary_capability(backend)
    by_lambda: dict[str, dict] = {}
    # Determine the query table from the first candidate (single-table benchs).
    table = candidates[0].table if candidates else ".climate"
    pgrid = _resolve_param_tiers(backend, param_tiers, table)

    for level in levels:
        rows = backend.sample_rows_at_level(table, level)
        single_tgt = backend.single_col_target_for_level(table, level)
        cap_p = backend.max_param_at_level(table, level)

        # 1) per-level no-ext baseline in the sampling state
        backend.enter_sampling_state(table, level)
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
                    continue  # param above this sampling level's lattice cap
                # name unique per (table, cols, level, param)
                cols_t = "_".join(str(c).lower() for c in cand.columns)
                name = f"ext_m_{table.rpartition('.')[2]}_{cols_t}_l{level}_{p}"
                obj = StatObject(table=cand.table, columns=cand.columns,
                                 capability=cap_obj, sampling_level=SamplingLevel(level,
                                 label=f"L{level}-p{p}"),
                                 name=name)
                backend.create_stat(obj)
                try:
                    backend.build_stat_param(obj, p)
                    est = backend.estimate(query)
                    size = backend.stat_size_bytes(obj)
                    # SEAM (placeholder): maint_var = model constant for now; a
                    # future real per-extstat measurement would replace this
                    # call with a timed marginal cost into this same per-slot
                    # field.
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


def measure_query_sampling_m(
    backend: Backend,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    levels: tuple[int, ...] = DEFAULT_SAMPLING_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    outdir: Optional[Path] = None,
) -> dict:
    """Protocol-M (catalog-mask) measurement per sampling level; same output
    shape as ``measure_query_sampling`` but de-amortizes the fixed scan.

    For each sampling level L: ``enter_sampling_state`` gives the no-ext baseline
    ``e^0`` in the level's sampling state; then ALL (candidate, param) objects are
    created and built in a SINGLE ANALYZE (``build_stat_params_batch`` — each
    object set to its own param, sharing the established S_L scan). Each
    (candidate, param) is then read by masking every other object's payload
    (Protocol-M), rather than re-ANALYZE per candidate (Protocol-A).

    Same-sample mask == drop is verified (repo memory 2026-09-03; q.184 M==A
    est/qerr exactly), so this measures the same per-level q-error as Protocol-A
    but with ~1 scan per level instead of #(cand,param) scans.

    Deterministic per level: each level uses its OWN S_L scan (NOT a shared
    max-S scan), so it does NOT reproduce v1 Protocol-M's large-sample bias.
    """
    if not backend.supports_catalog_mask():
        # Not a mask-capable backend: fall back to Protocol-A semantics.
        return measure_query_sampling(backend, query, candidates, levels=levels,
                                    param_tiers=param_tiers, outdir=outdir)

    cap_obj = _primary_capability(backend)
    table = candidates[0].table if candidates else ".climate"
    pgrid = _resolve_param_tiers(backend, param_tiers, table)
    by_lambda: dict[str, dict] = {}
    driver = backend.catalog_driver()

    for level in levels:
        rows = backend.sample_rows_at_level(table, level)
        single_tgt = backend.single_col_target_for_level(table, level)
        cap_p = backend.max_param_at_level(table, level)

        # 1) per-level no-ext baseline in the sampling state
        backend.enter_sampling_state(table, level)
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
                                 capability=cap_obj, sampling_level=SamplingLevel(level,
                                 label=f"L{level}-p{p}"),
                                 name=name)
                backend.create_stat(obj)
                objs.append((obj, p))

        bkp = None
        try:
            # ONE ANALYZE builds ALL objects from this level's S_L scan (Protocol-M)
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
                    # SEAM (placeholder): see the sibling call in the
                    # Protocol-A driver — maint_var is a model constant today,
                    # kept per-slot so a real measure can be dropped in later.
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


def measure_workload_sampling(
    backend: Backend,
    queries: list[BenchQuery],
    cands_by_q: dict[str, list[CandidateSet]],
    *,
    levels: tuple[int, ...] = DEFAULT_SAMPLING_LEVELS,
    param_tiers: Optional[tuple[int, ...]] = None,
    workload: str = "default",
    outdir: Path,
    use_protocol_m: bool = False,
    skip_existing: bool = True,
) -> None:
    """Measure a workload into ``<outdir>/measure/<workload>/``.

    Writes ``_meta.json`` + one ``<qid>.json`` per query, namespaced by workload
    and backend (``<outdir>/measure/<workload>/<backend>/``) so neither workload
    nor DBMS engine (which may hold the same columns but different native params)
    collide. ``param_tiers=None`` records the active backend's own representation
    grid; levels default to :data:`DEFAULT_SAMPLING_LEVELS` (L0/L1). When
    ``use_protocol_m`` is true and the backend can catalog-mask, per-query
    measurement uses :func:`measure_query_sampling_m` (one shared ANALYZE per
    sampling level) instead of per-candidate Protocol-A.

    ``skip_existing`` (default True) makes the run **idempotent/incremental**:
    a query whose ``<qid>.json`` already exists in the destination is skipped
    (a file is written only *after* all requested sampling levels complete, so
    presence of the file means the query is fully measured). This lets an
    interrupted full-workload run be resumed without re-measuring completed
    queries.
    """
    dest = result_dir(outdir, workload, backend.name())
    dest.mkdir(parents=True, exist_ok=True)
    # meta describing the SamplingTiers actually realized (per the first table's
    # row count via a reference table; native params recorded by level).
    tiers: list[SampleTier] = []
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
            rows = backend.sample_rows_at_level(table, level)
            ep = backend.sample_percent_at_level(table, level)
        else:
            single_tgt = rows = ep = None
        tiers.append(SampleTier(level=level, S_rows=rows,
                                single_target=single_tgt, estimate_percent=ep))
    write_meta(dest, Meta(bench=workload, backend=backend.name(), tiers=tiers,
                          param_tiers=meta_pgrid))

    measurer = measure_query_sampling_m if (
        use_protocol_m and backend.supports_catalog_mask()) else measure_query_sampling
    done = skipped = 0
    for query in queries:
        cands = cands_by_q.get(query.qid, [])
        out_file = dest / f"{query.qid}.json"
        if skip_existing and out_file.exists():
            skipped += 1
            if skipped <= 25 or skipped % 50 == 0:
                print(f"[skip] {query.qid} already measured (skipping)")
            continue
        measurer(backend, query, cands, levels=levels,
                 param_tiers=param_tiers, outdir=dest)
        done += 1
        if done == 1 or done % 10 == 0:
            print(f"[run] measured {done} new queries this invocation "
                  f"(qid={query.qid})", flush=True)
