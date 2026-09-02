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
DEFAULT_CAPACITY_LADDER = {
    0: 100,
    1: 1000,
    2: 10000,
}

# Key PostgreSQL uses for a plan node's estimated row count in EXPLAIN JSON.
_ROW_KEY = "Plan Rows"


# ---------------------------------------------------------------------------
# Protocol-M (catalog-mask) primitives — PostgreSQL
#
# Port of v1's ``measure_mask.py`` low-level helpers onto the v2 StatObject /
# capability abstraction. Protocol-M avoids a per-candidate ANALYZE: all of a
# table's candidate extended statistics are built by ONE ANALYZE; each is then
# measured by NULL-masking every *other* statistic's payload in
# ``pg_statistic_ext_data`` and EXPLAINing (a NULL payload makes the planner
# ignore the statistic, without error). Payloads are backed up to a temporary
# table and restored in-place by same-type ``pg_mcv_list`` assignment (there is
# no bytea cast for driver round-trip).
# ---------------------------------------------------------------------------


class PgPayloadBackup:
    """NULL-maskable snapshot of a set of extended-statistic payloads.

    Mirrors v1 ``measure_mask.py``: objects are grouped by capability kind and
    their ``pg_statistic_ext_data`` payload rows copied into per-kind temporary
    tables typed to the payload column's own type (``pg_mcv_list`` & friends,
    which have no bytea cast). Masking NULLs the live payload; restoring
    ``UPDATE ... SET col = backup.payload ...`` is type-safe and done entirely
    server-side.
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
        # PG's catalog-mask (Protocol-M) catalog primitives are implemented
        # (see PgCatalogDriver / PgPayloadBackup), but the measure-path that
        # *uses* them (build-all-then-mask scheduler in core/measure) is not yet
        # wired, so we do not yet advertise Protocol-M through protocol(None).
        # Flip to True once measure_query dispatches on Protocol-M.
        return False

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

        PostgreSQL's plain ``ANALYZE`` builds normal single-column histograms /
        stats. This represents the honest "no extended statistics" baseline; it
        is a parity helper mirroring ``OracleBackend.restore_natural_stats`` so
        the cross-backend harness measures both engines from natural stats.
        """
        with self.conn.cursor() as cur:
            cur.execute(f"ANALYZE {_clean_table(table)}")

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

    # Calibrated on Census `climate` (2.46M rows, warm cache), measured
    # 2026-09-02: bare ANALYZE is near-linear in `statistics_target` up to the
    # full-table-scan saturation point, with slope ~0.00256 s/target.
    _W_PER_TARGET = 0.00256          # s per statistics_target unit (linear region)
    _VAR_PER_STAT_T1000 = 0.02       # s per added statistic @ statistics_target 1000
    _RELTUPLES_CACHE: dict[str, float] = {}

    def _reltuples(self, table: str) -> float:
        """Estimated row count of ``table`` from pg_class (lazily cached)."""
        base = _clean_table(table).rpartition(".")[2]
        cached = self._RELTUPLES_CACHE.get(base)
        if cached is not None:
            return cached
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples FROM pg_class c WHERE c.relname = %s",
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
