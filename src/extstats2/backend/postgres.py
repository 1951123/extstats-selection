"""PostgreSQL 16 backend (Milestone M2 — real implementation).

Ports v1's PostgreSQL-specific logic onto the v2 backend abstraction:

- cardinality estimation via ``EXPLAIN (FORMAT JSON)`` (was ``estimate.py``),
- ``CREATE / DROP / ALTER STATISTICS`` DDL and ``ANALYZE`` (was ``stats.py`` +
  ``measure.py``),
- per-object on-disk size via ``pg_statistic_ext_data`` (was ``measure.stat_size_bytes``),
- Protocol-A isolation (drop/rebuild); Protocol-M catalog-mask is a later
  enhancement over ``pg_statistic_ext_data`` (was ``measure_mask.py``),
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
    Capability("dependency", "dependencies", "statistics_target", True),
    Capability("ndistinct", "ndistinct", "statistics_target", True),
    Capability("mcv", "mcv", "statistics_target", True),
]

# Per-kind payload column in pg_statistic_ext_data.
_KIND_DATA_COL = {
    "dependency": "stxddependencies",
    "ndistinct": "stxdndistinct",
    "mcv": "stxdmcv",
}

# Leading ``SELECT COUNT(*)`` (case-insensitive).
_SELECT_COUNT_RE = re.compile(r"(?is)^\s*SELECT\s+COUNT\(\*\)\s+")

# Default capacity ladder: abstract level index -> statistics_target.
DEFAULT_CAPACITY_LADDER = {
    0: 100,
    1: 1000,
    2: 10000,
}

# Key PostgreSQL uses for a plan node's estimated row count in EXPLAIN JSON.
_ROW_KEY = "Plan Rows"


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

    def has_protocol_m(self) -> bool:
        # PG *can* support catalog-mask (Protocol-M), but the current M2 isolate()
        # implements the universal Protocol-A (drop/rebuild). Flip to True once
        # the Protocol-M CatalogDriver over pg_statistic_ext_data is implemented.
        return False

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

    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        """Set per-object targets, then ANALYZE each distinct base table once."""
        tables = sorted({obj.table for obj in objs})
        target = self._native_target(capacity)
        with self.conn.cursor() as cur:
            for obj in objs:
                cur.execute(
                    f"ALTER STATISTICS {self._q(obj.name)} "
                    f"SET STATISTICS {int(target)}"
                )
        self._set_default_target(target)
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
        table_base = _clean_table(table).rpartition(".")[2]
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
                WHERE c.relname = %s
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

    def maintain_cost(self, obj: StatObject) -> float:
        """Estimate the deployed one-refresh ANALYZE cost of ``obj`` (seconds).

        Additive fixed+variable model (v1 §1.7 [O1] / §5.3):
          fixed ≈ 22s at t10000, scaled by target ratio;  var scales with target
          and column count. A model estimate for the ILP maintenance budget.
        """
        target = self._native_target(obj.capacity)
        fixed = 22.0 * (target / 10000.0)
        var = 0.001 * target * (1.0 + 0.1 * max(len(obj.columns) - 2, 0))
        return float(fixed + var)

    @staticmethod
    def _q(name: str) -> str:
        """Double-quote a statistic object name."""
        return f'"{name}"'


# ---------------------------------------------------------------------------
# Estimation helpers (ported from v1 estimate.py)
# ---------------------------------------------------------------------------

def _clean_table(table: str) -> str:
    """Strip a leading dot from a table name (v1 keys are ``.table``)."""
    return table.lstrip(".") or table


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
