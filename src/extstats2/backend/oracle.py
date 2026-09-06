"""Oracle backend (Milestone M3 — real implementation).

Oracle has no PostgreSQL-style ``CREATE STATISTICS`` objects.  The native
mechanism for *extended / multi-column statistics* is the **column group**
created through ``DBMS_STATS``:

- ``DBMS_STATS.GATHER_TABLE_STATS(..., METHOD_OPT => 'FOR COLUMNS (a,b) SIZE n')``
  builds a multi-column (column-group) histogram.  The optimizer uses it
  directly when estimating selectivity of predicates on ``(a,b)`` — the v1
  ``mcv`` capability, verified empirically here too: a 2-column group on Census
  ``CLIMATE`` moves an estimate from ~49k to ~989k (ground truth ~990k), i.e.
  it repairs selection-cardinality q-error.
- Column groups share a single ``GATHER`` scan of the table, so maintenance
  cost is a *fixed per-table* term (FIXED_ONLY) driven by the sampling knob
  ``estimate_percent`` (the "per_scan" capacity model).  There is no
  meaningful *per-statistic* marginal scan cost.
- Cardinality is read from ``EXPLAIN PLAN`` + ``plan_table`` (the rewritten
  ``SELECT *`` yields its filtered row count at the root node).
- Catalog: ``USER_STAT_EXTENSIONS`` (registered column groups) and
  ``USER_TAB_COL_STATISTICS`` (per-hidden-column histogram detail, used as a
  monotonic size proxy).
- Oracle cannot NULL-mask a single statistic without disturbing neighbours ->
  ``has_protocol_m() == False``; it uses Protocol-A.

Capability mapping (validated): ``mcv`` = column-group histogram (primary);
``ndistinct`` = column group without histogram (distinct count only);
``dependency`` unsupported in this first cut.

Extension identity is the *column set* (a group is system-suffixed like
``SYS_STU...`` and ``DROP_EXTENDED_STATS`` drops by the column expression).
Identifiers are unquoted and upper-cased (Oracle folds unquoted names to upper;
v2 benchmark SQL is lower/mixed case).
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from typing import Optional

from .base import Backend, Estimate, IsolationCtx, MaintStructure, StatObject, StructuralProps
from ..backend.capabilities import Capability, Capacity
from ..config import DBConfig
from ..core.queries import BenchQuery

# Leading ``SELECT COUNT(*)`` (case-insensitive) — same rewrite as the PG backend.
_SELECT_COUNT_RE = re.compile(r"(?is)^\s*SELECT\s+COUNT\(\*\)\s+")

# PG-column -> Oracle-column renames required by an Oracle Table whose load had to
# avoid a bare Oracle reserved word. The v2 benchmark SQL is written against the
# PostgreSQL schema (e.g. `badges.Date`); when the table was loaded into Oracle a
# reserved-word column was renamed (benchmark load renamed `badges.Date` -> CDATE).
# These are applied to the *transpiled* SQL so every reference to the PG column on
# that table is rewritten to the real Oracle column. NOTE (2026-09-05): only stats_CEB
# `badges` hits this; the map is table-keyed and intentionally small. Oracle compares
# unquoted/uppercase, so we only rewrite the exact reserved-word identifier.
# mapping keyed by lower table name -> {pg_col: oracle_col}
_RESERVED_COL_RENAME = {
    "badges": {"date": "cdate"},
}


def _rewrite_reserved_columns(table: str, sql: str) -> str:
    """Rewrite a transpiled Oracle query's references to a table's PG reserved-word
    columns to their actual Oracle column names. ``sql`` is the Oracle-dialect SQL
    (table alias already stripped of ``AS`` by sqlglot, e.g. ``FROM badges b``)."""
    ren = _RESERVED_COL_RENAME.get(table.lower())
    if not ren:
        return sql
    out = sql
    for pg_col, or_col in ren.items():
        # rewrite `<col>` used where the qualified source column is the reserved word;
        # benchmarks reference it via the table's alias, but be permissive (qualified
        # alias or bare word bounded by non-identifier chars).
        out = re.sub(
            rf"\b({pg_col})\b(?![A-Za-z0-9_])", or_col, out,
            flags=re.IGNORECASE,
        )
    return out

_CAPABILITIES = [
    # dependency: not implemented in the first cut.
    Capability("dependency", "none", "n/a", supported=False),
    Capability("ndistinct", "column_group", "estimate_percent", True, False),
    # primary: Oracle column-group statistics (multi-column histogram) repair
    # selection cardinality, mirroring PG mcv.
    Capability("mcv", "column_group", "estimate_percent", True, True),
]


# Abstract level index -> {"s_rows": S-target, "buckets": n}. 2026-09-05 S-grid:
# lambda tiers are defined by SAMPLE ROWS S (dataset-bound), not by a DBMS %
# knob. Global S_rows grid = [30000, 300000]; Oracle realizes S via
#   estimate_percent = 100 * min(S, N) / N
# (per table N), which mirrors PostgreSQL's S = min(300*target, N) so the two
# engines realize the SAME per-table S points:
#   N < 30000            -> both tiers full N        (1 distinct point)
#   30000 <= N < 300000  -> L0=30000 partial / L1=full N    ({30000, full})
#   N >= 300000          -> L0=30000 / L1=300000            ({30000, 300000})
# Column groups share one GATHER scan; the sampling knob (estimate_percent) is
# the per-scan capacity. The former fixed-% ladder (1/10/100) is dropped.
DEFAULT_CAPACITY_LADDER = {
    0: {"s_rows": 30000, "buckets": 254},
    1: {"s_rows": 300000, "buckets": 254},
}

# Gather-time calibration for a column-group GATHER (one shared scan), degree 1,
# measured 2026-09-02 on Census CLIMATE (~2.46M rows). The GATHER samples
# S_rows rows (per the sampling knob); measured one-refresh seconds:
#   L0 -> S=30000  rows -> 0.54s
#   L1 -> S=300000 rows -> 2.10s
# Fit an *affine-in-sampled-rows* model cost = base + rate*S. Solving the two
# points gives
#   rate = (2.10-0.54)/(300000-30000) ~ 5.778e-6 s/row,
#   base = 0.54 - rate*30000          ~ 0.3667 s.
# Scaling with rows actually read is S-grid-consistent: a GATHER samples
# min(S, N) rows and costs the same on any table with N >= S (CLIMATE and an
# 11.6M-row DMV both read 30k/300k rows -> ~0.54s/~2.10s), only shrinking on
# tables small enough that the level caps at a full scan (N < S). This replaces
# an earlier model that multiplied by N/2.46M, which wrongly billed large tables
# by their total size even though they sample no more rows.
_GATHER_BASE_S = 0.366667
_GATHER_RATE_S_PER_ROW = 5.7778e-6
# Marginal per-statistic component: negligible for column groups sharing a scan
# (FIXED_ONLY). Kept tiny so the per-stat model still reports a nonzero cost.
_VAR_PER_STAT_S = 0.002


class OracleBackend(Backend):
    """Oracle backend using DBMS_STATS column groups + Protocol-A.

    All SQL / DBMS_STATS knowledge lives here, never in ``core/``.
    """

    def __init__(self, cfg: Optional[DBConfig] = None,
                 capacity_ladder: Optional[dict[int, dict]] = None):
        self._cfg = cfg or DBConfig.from_env()
        self._ladder = dict(capacity_ladder or DEFAULT_CAPACITY_LADDER)
        self._conn = None
        self._owner: Optional[str] = None

    # -- connection --------------------------------------------------------

    def _dsn(self) -> str:
        c = self._cfg
        service = (c.service or c.dbname) or "FREEPDB1"
        return f"{c.host}:{c.port}/{service}"

    def _connect(self):
        import oracledb
        if self._cfg.mode == "thick":
            oracledb.init_oracle_client()
        self._conn = oracledb.connect(
            user=self._cfg.user, password=self._cfg.password, dsn=self._dsn())
        self._conn.autocommit = True
        cur = self._conn.cursor()
        cur.execute("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual")
        row = cur.fetchone()
        self._owner = row[0] if row else self._cfg.user
        return self._conn

    @property
    def conn(self):
        if self._conn is None:
            return self._connect()
        return self._conn

    def _cur(self):
        return self.conn.cursor()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _q_table(table: str) -> str:
        """Normalise a v2 table key (e.g. ``.climate``) to Oracle upper form."""
        return ((table or "").lstrip(".")).strip().upper()

    @staticmethod
    def _q_cols(columns) -> tuple[str, ...]:
        """Upper-case a column tuple (Oracle unquoted fold)."""
        return tuple((c or "").strip().upper() for c in columns)

    def _ext_expr(self, columns) -> str:
        """Catalog 'extension' string identifying a column group, e.g.
        ``("IAVAIL","ICLASS")`` (starts with '(' -> accepted by DROP)."""
        cols = self._q_cols(columns)
        return "(" + ",".join('"%s"' % c for c in cols) + ")"

    def _native(self, capacity: Capacity) -> tuple[float, int]:
        """Convenience: realized ``(estimate_percent, buckets)`` for an abstract
        capacity *without a table* — falls back to treating the level as a full
        scan (100%). Prefer :meth:`_percent_for` when the table is known."""
        return self._percent_for(None, capacity.level), self._buckets(capacity.level)

    def _s_rows_target(self, level: int) -> float:
        """The S_rows (sample-rows) target of λ-tier ``level`` from the S-grid."""
        if level not in self._ladder:
            raise KeyError(f"capacity level {level!r} not in ladder "
                           f"{list(self._ladder)}")
        params = self._ladder[level]
        if "s_rows" in params:
            return float(params["s_rows"])
        # legacy ladder stored estimate_percent (fixed %); convert at N=100 rows
        # keeps old callers working, but S-grid ladders store s_rows.
        return float(params["estimate_percent"])

    def _buckets(self, level: int) -> int:
        params = self._ladder.get(level, {})
        return int(params.get("buckets", 254))

    def _percent_for(self, table: Optional[str], level: int) -> float:
        """Realized Oracle ``estimate_percent`` for the S-grid tier ``level`` on
        ``table``: ``100 * min(S, N) / N`` (mirrors PG ``S=min(300*target,N)``).

        - N <= S   -> full scan (100%), the {full} / case-2-L1 points;
        - N >  S   -> partial ``%`` that samples the requested S rows.
        Unknown N/table falls back to full scan (100%), which is the safe
        engine-faithful depth (AUTO_SAMPLE_SIZE = full for these sizes).
        """
        S = self._s_rows_target(level)
        n = self._num_rows(table) if table else 0.0
        if n is None or n <= 0:
            return 100.0
        return min(100.0, 100.0 * min(S, n) / n)

    # -- metadata ----------------------------------------------------------

    def name(self) -> str:
        return "oracle"

    def supported_capabilities(self) -> list[Capability]:
        return list(_CAPABILITIES)

    def structural_props(self) -> StructuralProps:
        # Sparse-one-stat / disjointness are *expected* to hold on Oracle too
        # (data-driven, validated for PG); defaults kept permissive so the
        # sparse-linear class is reachable, pending M4 end-to-end validation.
        # Column groups share one GATHER scan -> FIXED_ONLY maintenance;
        # capacity is per-scan (estimate_percent).
        return StructuralProps(
            sparse_one_stat=True,
            disjoint_supported=True,
            maint_structure=MaintStructure.FIXED_ONLY,
            capacity_model="per_scan",
            supports_objectives=("mean",),
        )

    def has_protocol_m(self) -> bool:
        return False

    # -- capacity ----------------------------------------------------------

    def set_capacity(self, capacity: Capacity) -> None:
        # Oracle has no global 'how much' knob; estimate_percent is passed to
        # build_stats. No-op is fine here.
        return

    # -- lifecycle / DDL ---------------------------------------------------

    def create_stat(self, obj: StatObject) -> None:
        """Oracle column groups materialise during a GATHER; DDL-only creation
        is unavailable, so this is a no-op — creation happens in build_stats."""
        return

    def drop_stat(self, obj: StatObject) -> None:
        """Drop the column group by its column expression (idempotent)."""
        expression = self._ext_expr(obj.columns)
        self.drop_expression(self._q_table(obj.table), expression)

    def drop_expression(self, tname: str, expression: str) -> None:
        """Drop a column group on ``tname`` by its catalog extension string."""
        with self._cur() as cur:
            try:
                cur.execute(
                    "BEGIN DBMS_STATS.DROP_EXTENDED_STATS("
                    f"ownname=>'{self._owner}', tabname=>'{tname}', "
                    "extension=>:e); END;", {"e": expression})
            except Exception:
                return  # not present is fine

    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        """Gather each distinct base table once, at ``capacity``'s sampling.

        One ``GATHER_TABLE_STATS`` per table builds *all* requested column
        groups in a single scan (FIXED_ONLY / per-scan semantics). The groups
        are created here (Oracle has no separate create step).
        """
        if not objs:
            return
        by_table: dict[str, list[StatObject]] = {}
        for o in objs:
            by_table.setdefault(o.table, []).append(o)
        for table, objs_on in by_table.items():
            tname = self._q_table(table)
            # S-grid: the sampling depth is the S_rows target realizing min(S,N)
            # rows on *this* table, so estimate_percent is per-table.
            ep = self._percent_for(table, capacity.level)
            buckets = self._buckets(capacity.level)
            # Keep the engine's natural per-column statistics (SIZE AUTO builds
            # histograms for skewed columns) and add a histogram only on the
            # requested column groups. This matches how a real deployment would
            # analyse: measuring a column group on top of healthy single-column
            # stats is directly comparable to PostgreSQL's ANALYZE + extended
            # statistic (fair cross-backend baseline/candidate semantics).
            mo_parts = ["FOR ALL COLUMNS SIZE AUTO"]
            for o in objs_on:
                cap = o.capability
                cap_name = cap.name if cap is not None else "mcv"
                group = "(" + ",".join(self._q_cols(o.columns)) + ")"
                size = int(buckets) if cap_name == "mcv" else 1
                mo_parts.append(f"FOR COLUMNS {group} SIZE {size}")
            method_opt = " ".join(mo_parts)
            with self._cur() as cur:
                cur.execute(
                    "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                    f"ownname=>'{self._owner}', tabname=>'{tname}', "
                    "method_opt=>:m, estimate_percent=>:ep, degree=>1); END;",
                    {"m": method_opt, "ep": ep})

    def restore_natural_stats(self, table: str, estimate_percent: float = 100.0,
                              degree: int = 1) -> None:
        """Restore the engine's *natural* per-column statistics baseline.

        ``build_stats`` gathers with ``FOR ALL COLUMNS SIZE 1`` (no single-column
        histograms) to keep the focused measure cheap. That *destroys* the
        per-column histograms a real deployment would have, so measuring
        "baseline (no extended stats)" right after such gathers reports an
        artificially weak Oracle estimate. A fair cross-backend baseline should
        compare each engine with its normal per-column statistics; this method
        re-gathers with ``SIZE AUTO`` (builds histograms for skewed columns),
        mirroring PostgreSQL's default ``ANALYZE`` single-column baseline.
        """
        tname = self._q_table(table)
        with self._cur() as cur:
            cur.execute(
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                f"ownname=>'{self._owner}', tabname=>'{tname}', "
                "method_opt=>'FOR ALL COLUMNS SIZE AUTO', "
                "estimate_percent=>:ep, degree=>:d); END;",
                {"ep": estimate_percent, "d": degree})

    # -- λ-first (sampling-first, §7bis) realization ----------------------

    def lambda_sampling_rows(self, table: str, level: int) -> Optional[float]:
        """S at λ-tier ``level`` = ``estimate_percent/100 · N`` (Oracle's scan knob
        already directly sets the sample; no single-column target involved)."""
        return self.sample_rows_per_level(table, level)

    def lambda_sampling_percent(self, table: str, level: int) -> Optional[float]:
        """Oracle's native ``estimate_percent`` that realizes λ-tier ``level`` on
        ``table`` under the S-grid = ``100*min(S,N)/N`` (per-table, not a fixed
        %; mirrors PG's S realization)."""
        return float(self._percent_for(table, level))

    # Oracle's representation grid: a SINGLE engine-faithful operating point,
    # NOT a PG-style multi-point ``attstattarget`` menu. Unlike PostgreSQL,
    # Oracle exposes no per-object capacity knob comparable to
    # ``statistics_target``: histogram resolution is decided by the engine
    # itself (``SIZE AUTO`` / converged-to-natural buckets), and ``SIZE`` only
    # upper-bounds that. We verified (2026-09-05, Census L0) that requesting
    # SIZE 64 vs SIZE 254 collapses to ~the same realized few-dozen buckets,
    # i.e. there is no meaningful user-controlled resolution axis to grid
    # over. So the grid is a single point ``254`` = "let the engine choose
    # freely" (Oracle's classic column-group bucket headroom; >=~10000 is an
    # illegal method_opt literal ORA-20000). The generic measurement/optimizer
    # ``candidates x params`` layer is unchanged; this just makes Oracle
    # effectively ``|params| = 1`` per candidate.
    _PARAM_TIERS: tuple[int, ...] = (254,)
    #: max legal ``SIZE`` literal (Oracle rejects >= 10000 in method_opt).
    _MAX_PARAM: int = 1000

    def representation_param_tiers(self, table: str | None = None) -> tuple[int, ...]:
        return tuple(self._PARAM_TIERS)

    def single_col_target_for_level(self, table: str, level: int) -> Optional[int]:
        """Oracle realizes λ via ``estimate_percent`` (scan knob), NOT a single-column
        target — there is no per-column statistics_target knob on Oracle."""
        return None

    def max_param_at_level(self, table: str, level: int) -> Optional[float]:
        """No engine-imposed lattice cap on the representation param at a λ-tier.

        On PG, ``SIZE/param <= S/300`` is a genuine identity because the *same*
        knob (``statistics_target``) is both the MCV-list cap and the driver of
        sampling (``S = target*300``), so a statistic cannot represent more than
        its own target. Oracle has no such coupling: ``SIZE`` (histogram buckets)
        and ``estimate_percent`` (the λ sampling depth) are two *independent*
        arguments of the same ``GATHER_TABLE_STATS`` call, so a column group can
        legally carry ``SIZE 254`` even when the λ scan samples 1% of the table.

        Returning ``None`` (the base-class contract for "cap not engine-imposed")
        means the only bound on the offered representation params is
        :meth:`representation_param_tiers` itself; the generic per-λ driver no
        longer prunes ``SIZE=254`` at L0 as it would under the inherited PG
        ``S/300`` rule. (λ still affects *fidelity* of a sparse histogram, but
        that is a quality axis addressed separately, not a hard level->param cap.)
        """
        return None

    def enter_lambda_state(self, table: str, level: int) -> None:
        """Realize λ-tier ``level``: one single-column-only GATHER at that λ's
        realized estimate_percent for ``table`` (SIZE AUTO — natural single-col
        histograms), no column group. After this, ``estimate`` = the no-ext
        per-λ baseline ``e^0(S)``."""
        ep = self._percent_for(table, level)
        self.restore_natural_stats(table, estimate_percent=ep)

    def build_stat_param(self, obj: StatObject, param: int) -> None:
        """Build one column group at an explicit ``param`` buckets, at the current
        λ's realized estimate_percent for the object's table (S-grid scan depth).
        Decouples the object's bucket count from the level ladder."""
        ep = self._percent_for(obj.table, obj.capacity.level)  # this λ's scan %
        group = "(" + ",".join(self._q_cols(obj.columns)) + ")"
        mo = (f"FOR ALL COLUMNS SIZE AUTO FOR COLUMNS {group} SIZE {int(param)}")
        tname = self._q_table(obj.table)
        with self._cur() as cur:
            cur.execute(
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                f"ownname=>'{self._owner}', tabname=>'{tname}', "
                "method_opt=>:m, estimate_percent=>:ep, degree=>1); END;",
                {"m": mo, "ep": ep})

    # -- estimation --------------------------------------------------------

    def estimate(self, query: BenchQuery) -> Estimate:
        sql = _rewrite_count_to_select(query.sql)
        sql = _to_oracle_dialect(sql)
        est = self._top_cardinality(sql)
        return Estimate(estimate=est, raw=None, actual=query.ground_truth)

    def _top_cardinality(self, sql: str) -> int:
        sid = uuid.uuid4().hex[:16]
        with self._cur() as cur:
            # STATEMENT_ID must be a *string literal* (binds -> ORA-01780), so it
            # is formatted in; it is internally generated (never user input).
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID='{sid}' FOR {sql}")
            cur.execute(
                "SELECT cardinality FROM plan_table "
                "WHERE statement_id=:s AND cardinality IS NOT NULL "
                "ORDER BY id ASC", {"s": sid})
            row = cur.fetchone()
            est = int(row[0]) if row else 0
            try:
                cur.execute(
                    "DELETE FROM plan_table WHERE statement_id=:s", {"s": sid})
            except Exception:
                pass
        return est

    # -- catalog -----------------------------------------------------------

    def stat_size_bytes(self, obj: StatObject) -> int:
        """Monotonic proxy for the histogram's storage footprint.

        Oracle does not expose per-group dictionary bytes the way PG exposes
        ``pg_statistic_ext_data`` payloads. We use the hidden extension
        column's ``USER_TAB_COL_STATISTICS`` histogram detail (num_buckets x
        avg element width) as a monotonic catalog-derived proxy, which the
        storage-budget ILP can consume. Returns 0 when not yet built.

        (The extension column is a CLOB and cannot be compared in SQL, so the
        matching column group is resolved in Python from a fetched snapshot.)
        """
        tname = self._q_table(obj.table)
        want = self._q_cols(obj.columns)
        hidden = None
        with self._cur() as cur:
            cur.execute(
                "SELECT extension_name, extension FROM user_stat_extensions "
                "WHERE table_name=:t", {"t": tname})
            for name, expression in cur.fetchall():
                if tuple(_parse_extension_expression(_text(expression))) == want:
                    hidden = _text(name)
                    break
        if hidden is None:
            return 0
        with self._cur() as cur:
            cur.execute(
                "SELECT num_buckets, avg_col_len FROM user_tab_col_statistics "
                "WHERE table_name=:t AND column_name=:c",
                {"t": tname, "c": hidden})
            c = cur.fetchone()
        if c is None:
            return 0
        buckets = int(c[0] or 0)
        avg_len = float(c[1] or 0)
        return max(0, int(buckets * (avg_len + 4.0)))

    def list_stats(self, table: str) -> list[StatObject]:
        """Return the column groups currently on ``table`` (for verify)."""
        tname = self._q_table(table)
        with self._cur() as cur:
            cur.execute(
                "SELECT extension_name, extension FROM user_stat_extensions "
                "WHERE table_name=:t ORDER BY extension_name", {"t": tname})
            rows = cur.fetchall()
        mcv = next((c for c in _CAPABILITIES if c.name == "mcv"), _CAPABILITIES[-1])
        out: list[StatObject] = []
        for ext_name, expression in rows:
            ext_name = _text(ext_name)
            cols = tuple(_parse_extension_expression(expression))
            out.append(StatObject(table=table, columns=cols, capability=mcv,
                                  capacity=Capacity(0),
                                  name=ext_name or _text(expression)))
        return out

    # -- isolation (Protocol-A, column-group aware) -------------------------

    def isolate(self, keep: set[StatObject], table: str) -> IsolationCtx:
        """Protocol-A isolation for column groups.

        Oracle extensions are identified by column *set*, and a column group
        cannot be excluded individually (no Protocol-M). To study one group's
        effect we drop every group on ``table`` whose column set is not in
        ``keep``, measure, then restore the dropped groups — matching by column
        set (robust to naming differences between passes).
        """
        tname = self._q_table(table)

        def _colset(s: StatObject) -> tuple[str, ...]:
            return self._q_cols(s.columns)

        @contextmanager
        def _ctx():
            keep_sets = {_colset(s) for s in keep if s.columns}
            mcv = next((c for c in _CAPABILITIES if c.name == "mcv"),
                       _CAPABILITIES[-1])
            # 1) snapshot the live groups, then drop every one not in ``keep``
            #    so only the keep groups remain effective for measurement.
            with self._cur() as cur:
                cur.execute(
                    "SELECT extension FROM user_stat_extensions "
                    "WHERE table_name=:t", {"t": tname})
                live = [_text(e) for (e,) in cur.fetchall()]
            originally_present: set[tuple[str, ...]] = set()
            dropped: list[StatObject] = []   # groups removed (restore on exit)
            for expression in live:
                cols = tuple(_parse_extension_expression(expression))
                originally_present.add(cols)
                if cols not in keep_sets:
                    self.drop_expression(tname, expression)
                    dropped.append(StatObject(table=tname,
                                              columns=cols, capability=mcv,
                                              capacity=Capacity(0)))
            # 2) add any keep group not already live so it is active to measure.
            added_sets: set[tuple[str, ...]] = set()
            for k in keep:
                ks = _colset(k)
                if k.columns and ks not in originally_present:
                    self.build_stats([k], k.capacity)
                    added_sets.add(ks)
            try:
                yield
            finally:
                # remove the groups *we* added for isolation (they were not the
                # caller's pre-existing state)
                for k in keep:
                    if k.columns and _colset(k) in added_sets:
                        self.drop_stat(k)
                # restore the originally-present groups we dropped
                for st in dropped:
                    if not self._present(st):
                        self.build_stats([st], st.capacity)

        return _ctx()

    def _present(self, obj: StatObject) -> bool:
        tname = self._q_table(obj.table)
        want = self._q_cols(obj.columns)
        with self._cur() as cur:
            cur.execute(
                "SELECT extension FROM user_stat_extensions "
                "WHERE table_name=:t", {"t": tname})
            for (expression,) in cur.fetchall():
                if tuple(_parse_extension_expression(_text(expression))) == want:
                    return True
        return False

    # -- maintenance cost ---------------------------------------------------

    def table_maintain_tiers(self, table: str) -> tuple[float, ...]:
        """Fixed one-refresh GATHER cost per S-grid tier on ``table`` (seconds).

        A GATHER samples ``S_realized = min(S_rows_target(level), N)`` rows on
        ``table`` (S-grid semantics), so the cost is affine in the rows actually
        read: ``base + rate * S_realized``. Table-dependence enters *only*
        through S_realized — NOT through total N — so large tables that sample
        the same S rows cost the same (CLIMATE & an 11.6M-row DMV both read
        30k/300k rows -> ~0.54s / ~2.10s). Monotone increasing in level, since
        S_realized is non-decreasing in level.
        """
        if not self._ladder:
            # defensive: no ladder -> report the two CLIMATE calibration points
            return (_GATHER_BASE_S + _GATHER_RATE_S_PER_ROW * 30000.0,
                    _GATHER_BASE_S + _GATHER_RATE_S_PER_ROW * 300000.0)
        n = self._num_rows(table)
        out = []
        for lvl in sorted(self._ladder):
            s_target = self._s_rows_target(lvl)
            s_realized = min(s_target, n) if n and n > 0 else s_target
            out.append(_GATHER_BASE_S + _GATHER_RATE_S_PER_ROW * s_realized)
        return tuple(out)

    def stat_maintain_var(self, obj: StatObject) -> float:
        """Marginal per-statistic refresh cost (payload only), seconds."""
        return _VAR_PER_STAT_S

    def sample_rows_per_level(self, table: str, level: int) -> Optional[float]:
        """Expected SAMPLE rows at an S-grid capacity tier on ``table``.

        = ``min(S_target, N)``: the S_rows target (30000 / 300000) saturated at
        the table's row count N — mirrors PostgreSQL's ``S=min(300*target,N)``,
        so both engines realize the same per-table S points. (Oracle then *tries*
        to sample this many rows; block-granularity may make the recorded sample
        overshoot on small tables, which is reported via SAMPLE_SIZE.)
        """
        if level not in self._ladder:
            return None
        n = self._num_rows(table)
        if n is None or n <= 0:
            return None
        S = self._s_rows_target(level)
        return float(min(S, n))

    def num_rows(self, table: str) -> Optional[float]:
        n = self._num_rows(table)
        return None if n <= 0 else float(n)

    def _num_rows(self, table: str) -> float:
        tname = self._q_table(table)
        try:
            with self._cur() as cur:
                cur.execute(
                    "SELECT num_rows FROM user_tables WHERE table_name=:t",
                    {"t": tname})
                row = cur.fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0


def _text(value) -> str:
    """Coerce an oracledb CLOB (LOB) value back to plain text for Python use."""
    if value is None:
        return ""
    # LOB objects expose .read(); str/bytes pass through.
    read = getattr(value, "read", None)
    if callable(read):
        try:
            out = value.read()
            return out.decode() if isinstance(out, bytes) else out
        except Exception:
            return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _parse_extension_expression(expression: str) -> list[str]:
    """Decode an Oracle extension expression like ``("IAVAIL","ICLASS")`` into
    an ordered column list (quote/paren stripped, upper-cased)."""
    expression = _text(expression)
    if not expression:
        return []
    inner = expression.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    parts = [tok.strip().strip('"').strip() for tok in re.split(r",", inner)]
    return [p for p in parts if p]


def _rewrite_count_to_select(sql: str) -> str:
    """Rewrite ``SELECT COUNT(*) ...`` -> ``SELECT * ...`` so the plan's root
    cardinality equals the filtered input cardinality.

    Oracle requires an explicit select list (unlike PostgreSQL, which tolerates
    an empty one), so we emit ``SELECT *`` here.
    """
    sql = sql.strip().rstrip(";")
    rewritten = _SELECT_COUNT_RE.sub("SELECT * ", sql, count=1)
    if rewritten == sql:
        raise ValueError(f"Could not rewrite COUNT(*) query: {sql!r}")
    return rewritten


def _to_oracle_dialect(sql: str) -> str:
    """Transpile a PostgreSQL-flavored benchmark query to Oracle syntax.

    The v2 benchmark query files are written in PostgreSQL dialect (they use
    ``AS`` table aliases and ``::type`` casts). Oracle rejects those forms, so
    before EXPLAIN-ing we normalise the SQL to the Oracle dialect via sqlglot.
    Falls back to the input unchanged if transpilation is not applicable.
    """
    try:
        import sqlglot
        out = sqlglot.transpile(sql, read="postgres", write="oracle")
        if out and out[0]:
            res = out[0]
            # Apply reserved-word column renames for tables whose Oracle load renamed a
            # column away from an Oracle reserved word (e.g. badges.Date -> CDATE).
            fm = re.search(r"\bfrom\s+(\w+)\b", sql, re.I)
            if fm:
                res = _rewrite_reserved_columns(fm.group(1), res)
            return res
    except Exception:
        pass
    return sql
