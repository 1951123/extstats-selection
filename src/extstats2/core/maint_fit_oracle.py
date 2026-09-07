"""Offline maintenance-parameter measurement for Oracle 23ai (2026-09-06).

Mirror of the PostgreSQL estimator (:mod:`extstats2.core.maint_fit`) adapted to
Oracle's DBMS_STATS column-group semantics. Oracle has no standalone extstat DDL;
column groups are GATHERed and share the single scan (FIXED_ONLY), so:

    fixed(t, ℓ)  : one-refresh GATHER of the bare table (natural single-column,
                   SIZE AUTO) at the λ level's realized estimate_percent
                   (enter_sampling_state). = the shared scan cost.
    c_var(t, ℓ)  : marginal per column-group — by Oracle's FIXED_ONLY model the
                   per-group residual under the shared scan is real-but-tiny; we
                   still measure it by the aggregate whole-scan difference (a
                   GATHER maintaining k distinct 2-col groups minus bare, /k).
                   Floored to a small non-zero bound (never 0).

Oracle realizes λ S via ``estimate_percent = 100*min(S,N)/N`` per table; c_var is
per distinct 2-col column-pair capped at k_cap (measure the pair-averaged
marginal over the real deployable 2-col population). Writes the same corpus
artifact model (:mod:`extstats2.core.maint_model`), i.e.
``results/measure/<workload>/oracle/_maint.json``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Sequence

from ..backend.base import StatObject
from ..backend.capabilities import SamplingLevel
from ..backend.oracle import OracleBackend
from .maint_model import MaintParams, write_maint

# Median repeats per timed refresh; cap on # distinct 2-col probe groups.
_DEFAULT_REPEATS = 3
_DEFAULT_K_CAP = 100
_CVAR_FLOOR = 0.001


def _median(times: list[float]) -> float:
    times = sorted(times)
    return times[len(times) // 2]


def _regular_columns_oracle(backend: OracleBackend, table: str) -> list[str]:
    """User columns of ``table`` from the Oracle catalog (for building probe
    column groups). Dotted '.*.table' keys are normalised to the installed name."""
    tname = table.lstrip(".").upper().split(".")[-1]
    schema = backend._owner or "SYSTEM"
    with backend._cur() as cur:
        cur.execute(
            "SELECT column_name FROM all_tab_columns "
            "WHERE owner=:s AND table_name=:t ORDER BY column_id",
            {"s": schema, "t": tname})
        return [r[0] for r in cur.fetchall() if r[0]]


def _two_col_pairs(cols: list[str]) -> list[tuple[str, str]]:
    return [(cols[i], cols[j])
            for i in range(len(cols)) for j in range(i + 1, len(cols))]


def _probe_groups(backend: OracleBackend, table: str, level: int,
                  k_cap: int) -> list[tuple[str, str]]:
    """Distinct 2-col pairs of ``table`` to probe, capped at k_cap (uppercased for
    Oracle catalog lookup consistency)."""
    cols = [c.upper() for c in _regular_columns_oracle(backend, table)]
    return _two_col_pairs(cols)[:k_cap]


def _ep_of(backend: OracleBackend, table: str, level: int) -> float:
    return backend._percent_for(table, level)


def _time_gather(backend: OracleBackend, table: str, level: int,
                 groups: Sequence[tuple[str, str]], buckets: int = 254) -> float:
    """Wall-seconds of ONE GATHER on ``table`` at ``level``'s sampling that builds
    the given column groups (empty = bare/natural single-col only)."""
    tname = table.lstrip(".").upper().split(".")[-1]
    ep = _ep_of(backend, table, level)
    if groups:
        group_lit = " ".join(
            "FOR COLUMNS (%s) SIZE %d" % (",".join('"%s"' % c for c in g), buckets)
            for g in groups)
        mo = f"FOR ALL COLUMNS SIZE AUTO {group_lit}"
    else:
        mo = "FOR ALL COLUMNS SIZE AUTO"
    t0 = time.perf_counter()
    with backend._cur() as cur:
        cur.execute(
            "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
            f"ownname=>'{backend._owner}', tabname=>'{tname}', "
            f"method_opt=>:m, estimate_percent=>:ep, degree=>1); END;",
            {"m": mo, "ep": ep})
    return time.perf_counter() - t0


def _drop_groups(backend: OracleBackend, table: str,
                 groups: Sequence[tuple[str, str]]) -> None:
    tname = table.lstrip(".").upper().split(".")[-1]
    for g in groups:
        expr = "(" + ",".join('"%s"' % c for c in g) + ")"
        with backend._cur() as cur:
            try:
                cur.execute(
                    "BEGIN DBMS_STATS.DROP_EXTENDED_STATS("
                    f"ownname=>'{backend._owner}', tabname=>'{tname}', "
                    "extension=>:e); END;", {"e": expr})
            except Exception:
                pass


def measure_table_maintenance_oracle(backend: OracleBackend, table: str,
                                     level: int, *, k_cap: int = _DEFAULT_K_CAP,
                                     repeats: int = _DEFAULT_REPEATS
                                     ) -> tuple[float, float]:
    """Measure ``(fixed, c_var)`` for Oracle ``table`` at λ ``level``.

    fixed: clean the table (drop groups), then median of ``repeats`` bare GATHERs
    (natural single-column SIZE AUTO) at this level's realized estimate_percent.
    c_var: build the table's distinct 2-col probe groups (k capped), materialise
    once (untimed), then a with-k GATHER maintaining them; c_var = (with_k -
    fixed)/k, floored to a small non-zero bound. Groups dropped afterwards.
    """
    # drop any leftover probe groups from prior runs on this table
    for g in _probe_groups(backend, table, level, k_cap):
        _drop_groups(backend, table, [g])

    # -- fixed: bare (natural) gather at this level's S -------------------
    bare = _time_gather(backend, table, level, [])
    times = [bare]
    for _ in range(repeats - 1):
        times.append(_time_gather(backend, table, level, []))
    fixed = _median(times)

    # -- c_var: aggregate marginal of the distinct-2col group set ---------
    groups = _probe_groups(backend, table, level, k_cap)
    k = len(groups)
    if not k:
        return fixed, 0.0
    # one untimed gather to materialise the groups (they then exist / get maintained)
    _time_gather(backend, table, level, groups)
    withk_times = [_time_gather(backend, table, level, groups)
                   for _ in range(repeats)]
    _drop_groups(backend, table, groups)
    withk = _median(withk_times)
    c_var = max((withk - fixed) / k, _CVAR_FLOOR)
    return fixed, c_var


def fit_oracle_bench(bench: str, owner_tables: Sequence[str], levels: Sequence[int],
                     *, outdir: Path = Path("results"),
                     backend: Optional[OracleBackend] = None,
                     k_cap: int = _DEFAULT_K_CAP, repeats: int = _DEFAULT_REPEATS,
                     write: bool = True) -> MaintParams:
    from ..config import DBConfig, get_backend
    be = backend or get_backend(
        "oracle", cfg=DBConfig(host="localhost", port=1521, user="SYSTEM",
                               password="lxf82073077", service="FREEPDB1"))
    fixed_s, cvar_s = {}, {}
    for table in owner_tables:
        ft, vt = {}, {}
        for level in levels:
            fixed, cvar = measure_table_maintenance_oracle(be, table, level,
                                                           k_cap=k_cap,
                                                           repeats=repeats)
            ft[str(level)] = fixed
            vt[str(level)] = cvar
            print(f"  [or] {bench} {table} L{level}: fixed={fixed:.3f}s "
                  f"c_var={cvar:.2e}s (k<= {k_cap}, repeats={repeats})", flush=True)
        fixed_s[table] = ft
        cvar_s[table] = vt
    params = MaintParams(backend="oracle", fixed_seconds=fixed_s, c_var=cvar_s)
    if write:
        write_maint(outdir, bench, "oracle", params)
    return params
