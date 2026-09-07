"""PostgreSQL 16 backend (Milestone M2 — real implementation).

Implements the v2 backend abstraction for PostgreSQL:

- cardinality estimation via ``EXPLAIN (FORMAT JSON)``,
- ``CREATE / DROP / ALTER STATISTICS`` DDL and ``ANALYZE``,
- per-object on-disk size via ``pg_statistic_ext_data``,
- Protocol-A isolation (drop/rebuild); Protocol-M catalog-mask acceleration
  over ``pg_statistic_ext_data``,
- a fixed + variable maintenance-cost model for ``maintain_cost``.

All PostgreSQL-specific SQL and catalog knowledge lives here (never in ``core/``).
The abstraction seams are documented in ``docs/architecture.md`` §5.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Optional

import psycopg
from psycopg import Connection

from ..config import DBConfig
from ..core.queries import BenchQuery
from .base import (
    Backend,
    Estimate,
    IsolationCtx,
    MaintStructure,
    StatObject,
    StructuralProps,
)
from .capabilities import Capability, Capacity

# ---------------------------------------------------------------------------
# Capabilities & defaults
# ---------------------------------------------------------------------------

_CAPABILITIES = [
    Capability("dependency", "dependencies", "statistics_target", True, False),
    Capability("ndistinct", "ndistinct", "statistics_target", True, False),
    Capability("mcv", "mcv", "statistics_target", True, True),  # primary
]

# Per-kind payload column in pg_statistic_ext_data.
_KIND_DATA_COL = {
    "dependency": "stxddependencies",
    "ndistinct": "stxdndistinct",
    "mcv": "stxdmcv",
}
# Payload column's native type per capability kind (<> an SQL `%TYPE` reference,
# which is only valid inside PL/pgSQL).
_KIND_DATA_TYPE = {
    "dependency": "pg_dependencies",
    "ndistinct": "pg_ndistinct",
    "mcv": "pg_mcv_list",
}

# Leading ``SELECT COUNT(*)`` (case-insensitive).
_SELECT_COUNT_RE = re.compile(r"(?is)^\s*SELECT\s+COUNT\(\*\)\s+")

# Default capacity ladder: abstract level index -> statistics_target.
#
# 2026-09-05 S-grid policy: lambda tiers are defined by SAMPLE ROWS S, not by a
# DBMS-native knob. The global S_rows grid is [30000, 300000]; PG realizes S via
# statistics_target = S/300, so the ladder is target {100, 1000} <-> S {30000,
# 300000}. Sampling saturates at the table row count N (min(300*target, N)), which
# makes the realized per-table points follow the S-grid cases automatically:
#   N < 30000               -> both levels collapse to full N  (1 distinct point)
#   30000 <= N < 300000     -> L0=30000 partial, L1=full N     ({30000, full})
#   N >= 300000             -> L0=30000, L1=300000            ({30000, 300000})
# The former L2 target 10000 (S up to 3M) is dropped: its S exceeds the S-grid
# 300000 cap and is never realized on any table.
DEFAULT_CAPACITY_LADDER = {
    0: 100,
    1: 1000,
}

# Key PostgreSQL uses for a plan node's estimated row count in EXPLAIN JSON.
_ROW_KEY = "Plan Rows"


# ---------------------------------------------------------------------------
# Protocol-M (catalog-mask) primitives — PostgreSQL
#
# Protocol-M avoids a per-candidate ANALYZE: all of a table's candidate
# extended statistics are built by ONE ANALYZE; each is then
# measured by NULL-masking every *other* statistic's payload in
# ``pg_statistic_ext_data`` and EXPLAINing (a NULL payload makes the planner
# ignore the statistic, without error). Payloads are backed up to a temporary
# table and restored in-place by same-type ``pg_mcv_list`` assignment (there is
# no bytea cast for driver round-trip).
#
# Contrast — Protocol-A (the universal fallback this backend also provides,
# see ``isolate`` / ``build_stats``): each measured (candidate, level) pays ONE
# ANALYZE of its own table to materialise it ("build"). Removing that statistic
# to get a no-extstat estimate is a pure DROP STATISTICS (catalog delete) — the
# planner then reverts to the *still-present* single-column stats without any
# re-ANALYZE; no second scan is spent re-materialising the candidate. A later
# re-ANALYZE only happens to restore a table's OTHER, originally-present extended
# statistics (or a fair natural single-column baseline), i.e. isolation cleanup,
# not the candidate's own measurement. So Protocol-A is also "one materialising
# ANALYZE per measured candidate"; Protocol-M just de-amortises many candidates
# into shared scans.
# ---------------------------------------------------------------------------


class PgPayloadBackup:
    """NULL-maskable snapshot of a set of extended-statistic payloads.

    Objects are grouped by capability kind and their ``pg_statistic_ext_data``
    payload rows copied into per-kind temporary tables typed to the payload
    column's own type (``pg_mcv_list`` & friends, which have no bytea cast).
    Masking NULLs the live payload; restoring ``UPDATE ... SET col =
    backup.payload ...`` is type-safe and done entirely server-side.
    """

    def __init__(self, conn: Connection, objs: list[StatObject], prefix: str):
        self._conn = conn
        self._prefix = prefix
        # kind -> [(oid, col)]
        self._by_kind: dict[str, list[tuple[int, str]]] = {}
        for o in objs:
            name = o.capability.name if o.capability is not None else "mcv"
            col = _KIND_DATA_COL[name]
            oid = self._stat_oid(o)
            if oid is not None:
                self._by_kind.setdefault(name, []).append((oid, col))
        self._backed_up = False

    def _stat_oid(self, obj: StatObject) -> Optional[int]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT oid FROM pg_statistic_ext WHERE stxname = %s",
                (obj.name,))
            row = cur.fetchone()
            return row[0] if row else None

    def _backup_name(self, kind: str) -> str:
        return f"{self._prefix}_{kind}"

    def _ensure_backup(self) -> None:
        if self._backed_up:
            return
        for kind, pairs in self._by_kind.items():
            tbl = self._backup_name(kind)
            col = pairs[0][1]  # same col for a kind
            typ = _KIND_DATA_TYPE[kind]
            with self._conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {tbl}")
                cur.execute(
                    f"CREATE TEMP TABLE {tbl} (stxoid oid, payload {typ})")
            for oid, _ in pairs:
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {tbl} (stxoid, payload) "
                        f"SELECT stxoid, {col} FROM pg_statistic_ext_data "
                        f"WHERE stxoid = %s AND {col} IS NOT NULL", (oid,))
        self._backed_up = True

    def _keep_oids(self, keep: set[StatObject]) -> set[int]:
        keep_oids: set[int] = set()
        for k in keep:
            oid = self._stat_oid(k)
            if oid is not None:
                keep_oids.add(oid)
        return keep_oids

    def mask_all_but(self, keep: set[StatObject]) -> None:
        """NULL every backed-up payload except those in ``keep``."""
        self._ensure_backup()
        keep_oids = self._keep_oids(keep)
        for kind, pairs in self._by_kind.items():
            col = pairs[0][1]
            for oid, _ in pairs:
                if oid in keep_oids:
                    continue
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE pg_statistic_ext_data SET {col} = NULL "
                        f"WHERE stxoid = %s", (oid,))

    def restore(self) -> None:
        if not self._backed_up:
            return
        for kind, pairs in self._by_kind.items():
            tbl = self._backup_name(kind)
            col = pairs[0][1]
            with self._conn.cursor() as cur:
                cur.execute(
                    f"UPDATE pg_statistic_ext_data d SET {col} = b.payload "
                    f"FROM {tbl} b WHERE d.stxoid = b.stxoid")
        self._backed_up = False

    def close(self) -> None:
        for kind in list(self._by_kind):
            with self._conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {self._backup_name(kind)}")
        self._by_kind = {}


class PgCatalogDriver:
    """CatalogDriver (see backend/catalog.py) over PostgreSQL catalogs."""

    def __init__(self, backend: "PostgresBackend"):
        self._backend = backend

    @property
    def conn(self) -> Connection:
        return self._backend.conn

    def _clean(self, table: str) -> str:
        return table.lstrip(".") if table else table

    def lookup_oid(self, obj: StatObject) -> Optional[int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT oid FROM pg_statistic_ext WHERE stxname = %s", (obj.name,))
            row = cur.fetchone()
            return row[0] if row else None

    def size_bytes(self, obj: StatObject) -> int:
        return self._backend.stat_size_bytes(obj)

    def list_on_table(self, table: str) -> list[StatObject]:
        return self._backend.list_stats(table)

    def backup_payloads(self, objs: list[StatObject]) -> PgPayloadBackup:
        tag = id(objs) & 0xFFFF
        name = f"_ext_mask_{tag}"
        return PgPayloadBackup(self.conn, list(objs), name)


class PostgresBackend(Backend):
    """PostgreSQL 16 backend. Holds its own connection and capacity ladder."""

    def __init__(
        self,
        cfg: Optional[DBConfig] = None,
        capacity_ladder: Optional[dict[int, int]] = None,
    ):
        self._cfg = cfg or DBConfig.from_env()
        # backend-owned capacity ladder: level index -> statistics_target
        self._ladder = dict(capacity_ladder or DEFAULT_CAPACITY_LADDER)
        self._conn: Optional[Connection] = None
        # tables whose regular (single) columns we have already pinned to
        # _SINGLE_COL_STATISTICS_TARGET on this connection (cache: pin once).
        self._pinned_single: set[str] = set()

    # -- connection --------------------------------------------------------

    def _dsn(self) -> str:
        c = self._cfg
        parts = [f"host={c.host}", f"port={c.port}", f"user={c.user}",
                 f"dbname={c.dbname}"]
        if c.password:
            parts.append(f"password={c.password}")
        return " ".join(parts)

    def _conn_open(self) -> Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn())
            self._conn.autocommit = True
        return self._conn

    @property
    def conn(self) -> Connection:
        """The backend's live psycopg connection (autocommit)."""
        return self._conn_open()

    # -- metadata ----------------------------------------------------------

    def name(self) -> str:
        return "postgres"

    def supported_capabilities(self) -> list[Capability]:
        return list(_CAPABILITIES)

    def structural_props(self) -> StructuralProps:
        # PG supports the sparse-linear class and column-disjoint pruning
        # (verified in v1: one-stat sufficiency + global_disjoint predicts 1.000),
        # ANALYZE cost is a fixed sample base + additive per-stat update, capacity
        # is per-statistic (statistics_target). Objective: mean (extend later).
        return StructuralProps(
            sparse_one_stat=True,
            disjoint_supported=True,
            maint_structure=MaintStructure.FIXED_VAR,
            capacity_model="per_stat",
            supports_objectives=("mean",),
        )

    def supports_catalog_mask(self) -> bool:
        return True

    def catalog_driver(self) -> PgCatalogDriver:
        """Return this backend's Protocol-M catalog driver."""
        return PgCatalogDriver(self)

    # -- capacity ----------------------------------------------------------

    def set_capacity(self, capacity: Capacity) -> None:
        """Set ``default_statistics_target`` to this capacity's native value."""
        target = self._native_target(capacity)
        self._set_default_target(target)

    def _native_target(self, capacity: Capacity) -> int:
        """Map an abstract capacity level to a statistics_target."""
        if capacity.level not in self._ladder:
            raise KeyError(
                f"capacity level {capacity.level!r} not in ladder {self._ladder}"
            )
        return int(self._ladder[capacity.level])

    def _set_default_target(self, target: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"SET default_statistics_target = {int(target)}")

    # -- lifecycle / DDL ---------------------------------------------------

    def create_stat(self, obj: StatObject) -> None:
        """Create the statistic (idempotent: drops an existing object first)."""
        cols = ", ".join(obj.columns)
        kind = obj.capability.native_kind if obj.capability else "mcv"
        with self.conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {self._q(obj.name)}")
            cur.execute(
                f"CREATE STATISTICS {self._q(obj.name)} ({kind}) "
                f"ON {cols} FROM {_clean_table(obj.table)}"
            )

    def drop_stat(self, obj: StatObject) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {self._q(obj.name)}")

    def restore_natural_stats(self, table: str, **_) -> None:
        """Restore PG's natural per-column statistics baseline (single ANALYZE).

        Single columns are pinned to their default target (100, ALTER COLUMN) so
        the "no extended statistics" baseline is a deterministic, per-column
        natural state; a plain ANALYZE then rebuilds it. A parity helper
        mirroring ``OracleBackend.restore_natural_stats``.
        """
        self._ensure_single_columns_pinned(table)
        with self.conn.cursor() as cur:
            cur.execute(f"ANALYZE {_clean_table(table)}")

    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        """Set the extended-stat target, pin single columns to 100, ANALYZE once.

        Decided semantics: regular single columns stay at their pinned target
        (100); only the extended statistics' own target varies via
        ``ALTER STATISTICS ... SET STATISTICS``. We do NOT raise the global
        ``default_statistics_target`` to the extended target (that would also
        resample every single column and couple single-col statistics to the
        capacity axis).
        """
        tables = sorted({obj.table for obj in objs})
        target = self._native_target(capacity)
        for tbl in tables:
            self._ensure_single_columns_pinned(tbl)
        with self.conn.cursor() as cur:
            for obj in objs:
                cur.execute(
                    f"ALTER STATISTICS {self._q(obj.name)} "
                    f"SET STATISTICS {int(target)}"
                )
        for tbl in tables:
            with self.conn.cursor() as cur:
                cur.execute(f"ANALYZE {_clean_table(tbl)}")

    def build_stat_param(self, obj: StatObject, param: int) -> None:
        """Build one statistic ``obj`` at an explicit ``param`` in the current
        (λ) state: ``ALTER STATISTICS ... SET STATISTICS <param>`` + one ANALYZE.

        In the λ-first model the table's single columns are already at the λ-state
        target ``S/300`` (set by :meth:`enter_lambda_state`), which already forces
        the depth; setting the object's own target to ``param`` (≤ ``S/300``) just
        controls its retained representation without changing the shared scan.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"ALTER STATISTICS {self._q(obj.name)} "
                f"SET STATISTICS {int(param)}"
            )
            cur.execute(f"ANALYZE {_clean_table(obj.table)}")

    def build_stat_params_batch(self, objs_params: list[tuple[StatObject, int]]) -> None:
        """Protocol-M build: set each object's OWN target, then ONE ANALYZE per
        table builds them all from the established (λ-state) shared scan.

        The single columns are already at ``S/300`` (via :meth:`enter_lambda_state`),
        so ``targrows`` is already the deep ``S``; each object's own
        ``SET STATISTICS p`` (``p ≤ S/300``) only sets its retained representation
        without re-scanning for each one. This is the batch step that de-amortizes
        Protocol-A's per-object ANALYZE into a single shared scan.
        """
        tables = sorted({obj.table for obj, _ in objs_params})
        for tbl in tables:
            self._ensure_single_columns_pinned(tbl)
        with self.conn.cursor() as cur:
            for obj, param in objs_params:
                cur.execute(
                    f"ALTER STATISTICS {self._q(obj.name)} "
                    f"SET STATISTICS {int(param)}"
                )
        for tbl in tables:
            with self.conn.cursor() as cur:
                cur.execute(f"ANALYZE {_clean_table(tbl)}")

    # -- estimation --------------------------------------------------------

    def estimate(self, query: BenchQuery) -> Estimate:
        sql = _rewrite_count_to_select(query.sql)
        plan = _explain_json(self.conn, sql)
        est = int(plan[0]["Plan"][_ROW_KEY])
        return Estimate(estimate=est, raw=plan, actual=query.ground_truth)

    # -- catalog -----------------------------------------------------------

    def stat_size_bytes(self, obj: StatObject) -> int:
        col = _KIND_DATA_COL.get(
            obj.capability.name if obj.capability else "mcv", "stxdmcv"
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(pg_column_size(se.{col}), 0) AS size
                FROM pg_statistic_ext s
                JOIN pg_statistic_ext_data se ON se.stxoid = s.oid
                WHERE s.stxname = %s
                """,
                (obj.name,),
            )
            row = cur.fetchone()
        if row is None:
            return 0
        value = row.get("size") if isinstance(row, dict) else row[0]
        return int(value or 0)

    def list_stats(self, table: str) -> list[StatObject]:
        """Return statistics currently present on ``table`` (with real columns).

        Resolves each statistic's column set from ``pg_statistic_ext.stxkeys``
        (int2vector of attribute numbers) via ``pg_attribute``.
        """
        table_base = _relname(table)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.stxname, a.attnames
                FROM pg_statistic_ext s
                JOIN pg_class c ON c.oid = s.stxrelid
                JOIN LATERAL (
                    SELECT array_agg(att.attname ORDER BY ord) AS attnames
                    FROM unnest(s.stxkeys::int2[]) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute att
                      ON att.attrelid = s.stxrelid AND att.attnum = k.attnum
                ) a ON true
                WHERE c.relname = lower(%s)
                """,
                (table_base,),
            )
            rows = cur.fetchall()
        out: list[StatObject] = []
        for row in rows:
            name = row[0] if not isinstance(row, dict) else row["stxname"]
            cols_raw = row[1] if not isinstance(row, dict) else row["attnames"]
            cols = tuple(cols_raw) if cols_raw else ()
            out.append(
                StatObject(
                    table=table, columns=cols, capability=_CAPABILITIES[2],
                    capacity=Capacity(0), name=name,
                )
            )
        return out

    # -- isolation (Protocol-A) ----------------------------------------------

    def isolate(self, keep: set[StatObject], table: str) -> IsolationCtx:
        """Protocol-A isolation: leave only ``keep`` effective while measuring,
        then restore the database exactly to its pre-isolation state.

        On enter: drop every existing stat on ``table`` not in ``keep``, and
        create any ``keep`` stat that does not already exist.
        On exit (finally): drop any stat that was *newly created* for this
        isolation, then rebuild the originally-present stats that were dropped.

        (Protocol-M catalog-mask is a later acceleration replacing this.)
        """
        table = _clean_table(table)

        @contextmanager
        def _ctx():
            present_orig = {s.name: s for s in self.list_stats(table)}
            new_names: list[str] = []
            dropped: list[StatObject] = []

            # drop existing stats not in keep
            for name, s in present_orig.items():
                if s.name not in {k.name for k in keep}:
                    self.drop_stat(s)
                    dropped.append(s)
            # create keep stats that are not already present
            keep_names = {k.name for k in keep}
            for k in keep:
                if k.name not in present_orig:
                    self.create_stat(k)
                    new_names.append(k.name)
            try:
                yield
            finally:
                # 1) drop newly-created keep stats (cleanup)
                for nm in new_names:
                    dummy = StatObject(table=table, columns=(), capability=_CAPABILITIES[2],
                                       capacity=Capacity(0), name=nm)
                    self.drop_stat(dummy)
                # 2) rebuild originally-present stats that we dropped
                for s in dropped:
                    self.create_stat(s)
                if dropped:
                    max_cap = max((s.capacity for s in dropped),
                                  key=lambda c: c.level, default=Capacity(0))
                    self.build_stats(dropped, max_cap)
        return _ctx()

    # -- maintenance cost ---------------------------------------------------

    # Calibrated on Census `climate` (2.46M rows, warm cache), measured
    # 2026-09-02: bare ANALYZE is near-linear in `statistics_target` up to the
    # full-table-scan saturation point, with slope ~0.00256 s/target.
    _W_PER_TARGET = 0.00256          # s per statistics_target unit (linear region)
    _VAR_PER_STAT_T1000 = 0.02       # s per added statistic @ statistics_target 1000
    _RELTUPLES_CACHE: dict[str, float] = {}
    # Decided deployment semantics (2026-09-02): single (regular) columns stay
    # pinned at this statistics_target via `ALTER TABLE ... ALTER COLUMN SET
    # STATISTICS`; only the *extended* statistic's target varies. targrows then
    # has a floor of ~300*this per the ANALYZE scan.
    _SINGLE_COL_STATISTICS_TARGET = 100

    # ---- single-column target control ----------------------------------
    def _regular_columns(self, table: str) -> list[str]:
        """Names of ``table``'s regular (non-system) columns (dotted '.' ok)."""
        base = _relname(table)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT a.attname FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = lower(%s) AND a.attnum > 0 AND NOT a.attisdropped "
                "AND a.attidentity = '' AND a.attgenerated = ''",
                (base,))
            return [r[0] for r in cur.fetchall()]

    def _set_all_columns_target(self, table: str, tgt: int, analyze: bool = True) -> None:
        """Set every regular column's ``attstattarget`` to ``tgt`` and match the
        session default; optionally ANALYZE once to realize the depth.

        This is the PG realization of a λ-state: with all single columns at ``tgt``
        (== ``S/300``) they are the max, so ``targrows >= 300*tgt = S``.
        """
        tgt = int(tgt)
        with self.conn.cursor() as cur:
            cur.execute(f"SET default_statistics_target = {tgt}")
            for col in self._regular_columns(table):
                try:
                    cur.execute(
                        f"ALTER TABLE {_clean_table(table)} "
                        f"ALTER COLUMN {col} SET STATISTICS {tgt}")
                except Exception:
                    pass
            if analyze:
                cur.execute(f"ANALYZE {_clean_table(table)}")

    def _ensure_single_columns_pinned(self, table: str) -> None:
        """Pin every regular column to ``_SINGLE_COL_STATISTICS_TARGET`` (100),
        per (connection, table), cached (legacy 100-pin; kept for back-compat).
        Does NOT ANALYZE — callers ANALYZE when they build."""
        base = _relname(table)
        if base in self._pinned_single:
            return
        self._set_all_columns_target(
            table, self._SINGLE_COL_STATISTICS_TARGET, analyze=False)
        self._pinned_single.add(base)

    def _reltuples(self, table: str) -> float:
        """Estimated row count of ``table`` from pg_class (lazily cached)."""
        base = _relname(table)
        cached = self._RELTUPLES_CACHE.get(base)
        if cached is not None:
            return cached
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples FROM pg_class c WHERE c.relname = lower(%s)",
                (base,),
            )
            row = cur.fetchone()
        n = float(row[0] if row and row[0] else 0)
        self._RELTUPLES_CACHE[base] = n
        return n

    def _fixed_analyze(self, table: str, target: int) -> float:
        """One-refresh *fixed* ANALYZE cost for ``table`` at ``target`` (seconds).

        Ramp then full-table-scan saturation, calibrated on Census `climate`:
          fixed = w_per_target * min(target, reltuples/300)
        This is paid ONCE per activated table (at its max selected target),
        not per statistic.
        """
        n = max(self._reltuples(table), 1.0)
        t_sat = n / 300.0            # sampling hits a full-table scan here
        return float(self._W_PER_TARGET * min(target, t_sat))

    def table_maintain_tiers(self, table: str) -> tuple[float, ...]:
        """Fixed cost per target tier (index = abstract level 0,1,2,...).

        ``base[k]`` = one-refresh ANALYZE fixed cost if the highest target level
        selected on ``table`` is `k` (targrows = max target ⇒ sampling is shared
        across all stats on the table).
        """
        n_levels = max(self._ladder) + 1 if self._ladder else 1
        out = []
        for lvl in range(n_levels):
            if lvl not in self._ladder:
                out.append(out[-1] if out else 0.0)
                continue
            tgt = self._ladder[lvl]
            out.append(self._fixed_analyze(table, tgt))
        return tuple(out)

    def stat_maintain_var(self, obj: StatObject) -> float:
        """Marginal per-statistic refresh cost (payload update), seconds.

        Small relative to the shared fixed scan; scales with target and arity.
        """
        t = self._native_target(obj.capacity)
        arity_f = 1.0 + 0.1 * max(len(obj.columns) - 2, 0)
        return float(self._VAR_PER_STAT_T1000 * (t / 1000.0) * arity_f)

    def sample_rows_per_level(self, table: str, level: int) -> Optional[float]:
        """Expected ANALYZE sample rows at a capacity tier (targrows semantics).

        With single columns pinned to ``_SINGLE_COL_STATISTICS_TARGET`` (=100),
        ANALYZE scans ``targrows = max(300*100, 300*target)`` (the single-col
        default floors it), capped at the table's row count for a full scan.
        Returns ``None`` for an unknown level / row count.
        """
        if level not in self._ladder:
            return None
        target = int(self._ladder[level])
        targrows = 300.0 * max(target, int(self._SINGLE_COL_STATISTICS_TARGET))
        n = self._reltuples(table)
        if n <= 0:
            return None
        return float(min(targrows, n))

    def num_rows(self, table: str) -> Optional[float]:
        n = self._reltuples(table)
        return None if n <= 0 else float(n)

    # -- λ-first (sampling-first, §7bis) realization ----------------------

    def _target_for_level(self, table: str, level: int) -> Optional[int]:
        """PG native statistics_target for λ-tier ``level`` (== S_level/300)."""
        if level not in self._ladder:
            return None
        n = self._reltuples(table)
        if n <= 0:
            return None
        # S_level = min(300*target, N); its column handle = S/l300 = min(t, N/300)
        t_raw = int(self._ladder[level])
        return int(min(t_raw, n / 300.0))

    def lambda_sampling_rows(self, table: str, level: int) -> Optional[float]:
        """S_level = rows ANALYZEd at λ-tier ``level`` with all single columns at
        the λ target (no 100 floor; λ-first) — ``= min(300*target, N)``."""
        if level not in self._ladder:
            return None
        n = self._reltuples(table)
        if n <= 0:
            return None
        return float(min(300.0 * int(self._ladder[level]), n))

    def representation_param_tiers(self, table: str | None = None) -> tuple[int, ...]:
        """PG represents ext-stat detail as a scalar ``attstattarget``.

        Returns a deliberately DENSE general grid. Rationale (2026-09-03,
        architecture decision): the representation param at which a (colset, λ)
        saturates is data-driven and not known *a priori*, so the measurement grid
        is kept dense rather than pre-trimmed from hindsight (v1/offline analysis
        must not be used to cut the measurement range — that would bake in a
        post-hoc assumption). Each λ's lattice cap ``S/300`` automatically drops
        params above the λ's admissible depth; the optimizer then dominance-prunes
        plateau-synonymous / dominated params per (query, colset, λ) at solve time.
        Coverage spans low-cardinality (census climate: saturates <= ~50-100) up to
        high-cardinality (stats_CEB: needs 1000), so the same grid serves both.

        UPPER BOUND (2026-09-05, S-grid policy): the global S_rows grid caps the
        max sampling depth at S_max = 300_000 rows, so the max meaningful
        ``statistics_target`` is ``S_max/300 = 1000``. A param > 1000 would require
        S = 300·param > 300 000 rows, which the S-grid never realizes on any table —
        so tiers above 1000 (2500/5000/10000) are dropped from the grid."""
        return (5, 10, 25, 50, 100, 250, 500, 1000)

    def max_param_at_level(self, table: str, level: int) -> Optional[float]:
        """Lattice cap ``S_level/300`` = the λ single-column handle (PG)."""
        t = self._target_for_level(table, level)
        return float(t) if t is not None else None

    def single_col_target_for_level(self, table: str, level: int) -> Optional[int]:
        """The exact integer to set every single column's ``attstattarget`` to in
        order to realize λ-tier ``level`` (== max_param_at_level)."""
        return self._target_for_level(table, level)

    def enter_lambda_state(self, table: str, level: int) -> None:
        """Realize λ-tier ``level``: set ALL single columns to
        ``attstattarget = S/300`` and ANALYZE once (no extended stat present).

        After this, ``estimate`` = no-ext per-λ baseline ``e^0(S_level)``; an
        extended object later built at ``param ≤ S_level/300`` shares this depth.
        """
        tgt = self._target_for_level(table, level)
        if tgt is None:
            raise KeyError(f"level {level} not in ladder {self._ladder}")
        self._set_all_columns_target(table, tgt, analyze=True)

    @staticmethod
    def _q(name: str) -> str:
        """Double-quote a statistic object name."""
        return f'"{name}"'


# ---------------------------------------------------------------------------
# Estimation helpers
# ---------------------------------------------------------------------------

def _clean_table(table: str) -> str:
    """Strip a leading dot from a table name (v1 keys are ``.table``)."""
    return table.lstrip(".") or table


def _relname(table: str) -> str:
    """The PostgreSQL relation name PG actually sees for a bench table key.

    Unquoted identifiers fold to lowercase in PG (``FROM postHistory`` resolves
    to ``posthistory``), so metadata/catalog lookups by ``pg_class.relname`` must
    likewise match the lowercased table key (bench keys may be camelCase, e.g.
    ``.postHistory``). Used only for catalog reads; DDL/SQL keep the caller's
    spelling because PG case-folds it identically."""
    return _clean_table(table).rpartition(".")[2].lower()


def _rewrite_count_to_select(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    rewritten = _SELECT_COUNT_RE.sub("SELECT ", sql, count=1)
    if rewritten == sql:
        raise ValueError(f"Could not rewrite COUNT(*) query: {sql!r}")
    return rewritten


def _explain_json(conn: Connection, sql: str):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql)
        row = cur.fetchone()
    if isinstance(row, dict):
        return row.get("QUERY PLAN") or next(iter(row.values()))
    return row[0]
