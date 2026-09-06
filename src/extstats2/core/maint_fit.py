"""Offline maintenance-parameter measurement for PostgreSQL (2026-09-06).

This is the DB-timed estimator that FILLS the pure :mod:`extstats2.core.maint_model`
schema (``_maint.json``) with real per-(table, λ-level) numbers, separate from the
per-query measure run. It realizes the decided linear model::

    fixed(t, ℓ)  = real one-refresh scan seconds of bare ``t`` at λ level ``ℓ``
                   (single columns at S/300, no extended statistic)
    c_var(t, ℓ)  = real marginal per-extended-statistic seconds on ``t`` at ``ℓ``

Protocol (PostgreSQL)
---------------------
Maintenance of a deployed set is a single per-table ``ANALYZE`` that scans
``S = 300 * target`` rows (single columns pinned to the λ target ``S/300``) and
computes the table's extended statistics. Timing protocol per ``(table, ℓ)``:

1. Establish the λ-state: ``enter_lambda_state(table, level)`` (sets every single
   column's ``attstattarget`` to ``S/300`` and ANALYZEs once). ``fixed`` is taken
   as the median wall-time of ``n`` further bare ``ANALYZE t`` calls (no DDL in the
   timed loop — ALTER/SET happen once up front, so the timed op is the recurring
   maintenance scan itself).
2. Create ``k`` representative 2-column extended statistics on the table, in the
   same λ-state; ``t_with_k`` = median time of an ``ANALYZE t`` that maintains them
   (one shared scan). Drop the ``k`` stats afterwards.
3. ``c_var = max(0, (t_with_k − fixed)/k)`` — an aggregate whole-scan difference,
   NOT a fragile per-stat timer (the marginal per-stat residual under a shared
   scan is tiny and noisy; dividing a k-stat delta by k gives a much more stable
   estimate than timing a single statistic).

Repeats are median-pooled to damp warm-cache/OS jitter. Each table is left in its
bare natural state, and the corpus ``_maint.json`` is written via
:func:`extstats2.core.maint_model.write_maint`.

Note: DB timings are inherently warm/noisy; treat the fitted numbers as order-of-
magnitude calibration, not µs-exact. ``k`` (the probe stat count) and ``repeats``
are tunable for the desired delta stability on each table's size.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Sequence

from ..backend.base import Backend, StatObject
from ..backend.capabilities import Capacity
from ..backend.postgres import PostgresBackend
from .maint_model import MaintParams, write_maint

# Cap on the number of DISTINCT 2-col probe statistics used to measure c_var.
# A larger k gives a bigger, more stable aggregate delta (higher SNR); capped at
# 100 to stay representative and bound the one-time DDL.
_DEFAULT_K_CAP = 100
# Median repeats for each timed refresh.
_DEFAULT_REPEATS = 3
# Representative statistic kind to build in the probe.
_PROBE_KIND = "mcv"


def _median(times: list[float]) -> float:
    times = sorted(times)
    return times[len(times) // 2]


def _table_sql(table: str) -> str:
    """Qualified identifier for a dotted corpus table key (``.climate`` -> climate,
    keep the rest; the PG catalog lower-cases, matching backend _clean_table)."""
    return table.lstrip(".")


def _regular_columns(table: str, backend: PostgresBackend) -> list[str]:
    """Regular (user, non-system) columns of ``table`` for building probe stats."""
    cols = backend._regular_columns(table)
    return [c for c in cols if not c.startswith("__")]


def _two_col_pairs(cols: list[str]) -> list[tuple[str, str]]:
    """All distinct unordered 2-column pairs of ``cols``."""
    return [(cols[i], cols[j])
            for i in range(len(cols)) for j in range(i + 1, len(cols))]


def _probe_stats_for_table(backend: Backend, table: str, level: int,
                           k_cap: int) -> list[StatObject]:
    """One distinct-column-pair mcv probe stat per 2-col pair of ``table``, capped
    at ``k_cap``; returns ``[]`` if the table has <2 regular columns (no deployable
    2-col extstat, so no per-stat marginal to measure)."""
    mcv = next((c for c in backend.supported_capabilities() if c.name == _PROBE_KIND),
               None)
    if mcv is None:
        return []
    pairs = _two_col_pairs(_regular_columns(table, backend))[:k_cap]
    objs = []
    for idx, (c0, c1) in enumerate(pairs):
        objs.append(StatObject(
            table=table, columns=(c0, c1), capability=mcv,
            capacity=Capacity(level, label=f"L{level}-maint"),
            name=f"__ext_maint_{_table_sql(table)}_{level}_{idx}"))
    return objs


def _time_analyze_once(backend: PostgresBackend, table: str) -> float:
    """One bare ``ANALYZE table`` wall-seconds (single cols already in the λ-state).
    Free of DDL — measures the recurring maintenance scan itself."""
    tn = _table_sql(table)
    with backend.conn.cursor() as cur:
        t0 = time.perf_counter()
        cur.execute(f"ANALYZE {tn}")
        return time.perf_counter() - t0


#: Floor for a measured per-stat marginal (sec). We still genuinely measure
#: c_var (paired-delta whole-scan marginal / k), but any measured value below
#: this is treated as exactly this floor (decision 2026-09-06): a sub-millisecond
#: per-stat marginal is below reliable wall-clock resolution on fast/small tables
#: and physically cannot be ≤ 0, so below 0.001s we store the 0.001 bound rather
#: than a noisy near-zero that would silently zero / distort the maint-var term.
_CVAR_FLOOR = 0.001


def measure_table_maintenance_pg(backend: PostgresBackend, table: str, level: int,
                                 *, k_cap: int = _DEFAULT_K_CAP,
                                 repeats: int = _DEFAULT_REPEATS,
                                 analyze_only: bool = False
                                 ) -> tuple[float, float]:
    """Measure ``(fixed, c_var)`` seconds for one PG ``table`` at λ ``level``.

    ``fixed``  = median of ``repeats`` BARE ``ANALYZE`` runs (single columns at the
    λ target ``S/300``, no ext stat) = one-refresh shared-scan seconds.

    ``c_var``  = the mean per-extstat marginal over the table's DISTINCT 2-column
    pairs, resolved by PAIRED per-round deltas: each round times a bare ANALYZE
    then an ANALYZE carrying ``k`` probe column-group stats (one per distinct
    2-col pair, ``k = min(k_cap, #pairs)``) back-to-back (same cache state,
    cancelling slow drift); c_var = median of ``repeats`` deltas / ``k``. Using
    distinct pairs (not stacked identical ones) makes it the pair-averaged
    marginal over the real deployable 2-col population. A measured value
    < :data:`_CVAR_FLOOR` (0.001s) is treated as exactly 0.001s (decision
    2026-09-06).

    A table with <2 regular columns has no deployable 2-col extended statistic,
    so there is no per-stat marginal to measure -> returns ``(fixed, 0.0)``.

    PROTOCOL (n=0 vs n=k, the correct marginal): ``fixed`` is timed with NO
    extended statistic present (bare ANALYZE at the λ single-col target); then the
    ``k`` distinct probe column-groups are created, computed once (untimed), and
    ``with_k`` is timed as ANALYZE passes that maintain those existing stats. Only
    then is c_var = (with_k - fixed)/k. (Timing both sides after creating the stats
    would make the "bare" pass also maintain them -> a ~0 fake delta.)
    """
    tgt = backend._target_for_level(table, level)
    if tgt is None:
        raise KeyError(f"level {level} not realised on {table!r}")
    backend.enter_lambda_state(table, level) if not analyze_only else None

    # ---- fixed @ n=0 (no ext stats) -------------------------------------
    _time_analyze_once(backend, table)         # warm (untimed)
    bare_times = [_time_analyze_once(backend, table) for _ in range(repeats)]
    fixed = _median(bare_times)

    # ---- c_var: marginal added by maintaining k existing stats ----------
    objs = _probe_stats_for_table(backend, table, level, k_cap)
    k = len(objs)
    per_stat = 0.0
    if k:
        for o in objs:
            backend.create_stat(o)
        try:
            param = max(1, int(tgt))
            with backend.conn.cursor() as cur:
                for o in objs:
                    cur.execute(
                        f"ALTER STATISTICS \"{o.name}\" SET STATISTICS {param}")
            _time_analyze_once(backend, table)     # compute the k stats once (untimed)
            withk_times = [_time_analyze_once(backend, table)
                           for _ in range(repeats)]
            with_k = _median(withk_times)
        finally:
            for o in objs:
                backend.drop_stat(o)
        per_stat = max((with_k - fixed) / k, _CVAR_FLOOR)
    return fixed, per_stat


def fit_pg_bench(bench: str, pgdb: str, owner_tables: Sequence[str],
                 levels: Sequence[int], *, outdir: Path = Path("results"),
                 backend: Optional[PostgresBackend] = None,
                 k_cap: int = _DEFAULT_K_CAP, repeats: int = _DEFAULT_REPEATS,
                 write: bool = True) -> MaintParams:
    """Fit PG maintenance params for one benchmark corpus and persist ``_maint.json``.

    ``owner_tables`` (corpus dotted keys), ``levels`` (realised λ levels) come from
    the corpus ``_meta.json``. ``backend`` may be supplied (already aimed at
    ``pgdb``); otherwise constructs one. Returns :class:`MaintParams`.
    """
    from ..config import DBConfig, get_backend
    be = backend or get_backend(
        "postgres", cfg=DBConfig(host="localhost", port=5432, user="postgres",
                                 password="postgres", dbname=pgdb))
    fixed_s, cvar_s = {}, {}
    for table in owner_tables:
        ft, vt = {}, {}
        for level in levels:
            fixed, cvar = measure_table_maintenance_pg(be, table, level,
                                                       k_cap=k_cap, repeats=repeats)
            ft[str(level)] = fixed
            vt[str(level)] = cvar
            print(f"  [pg] {bench} {table} L{level}: "
                  f"fixed={fixed:.3f}s c_var={cvar:.2e}s "
                  f"(2col-cap={k_cap}, repeats={repeats})", flush=True)
        fixed_s[table] = ft
        cvar_s[table] = vt
    params = MaintParams(backend="postgres", fixed_seconds=fixed_s, c_var=cvar_s)
    if write:
        write_maint(outdir, bench, "postgres", params)
    return params
