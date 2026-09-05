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

_CAPABILITIES = [
    # dependency: not implemented in the first cut.
    Capability("dependency", "none", "n/a", supported=False),
    Capability("ndistinct", "column_group", "estimate_percent", True, False),
    # primary: Oracle column-group statistics (multi-column histogram) repair
    # selection cardinality, mirroring PG mcv.
    Capability("mcv", "column_group", "estimate_percent", True, True),
]


# Abstract level index -> (estimate_percent, buckets). Column groups share one
# GATHER scan; the sampling knob (estimate_percent) is the per-scan capacity.
DEFAULT_CAPACITY_LADDER = {
    0: {"estimate_percent": 1, "buckets": 254},
    1: {"estimate_percent": 10, "buckets": 254},
    2: {"estimate_percent": 100, "buckets": 254},
}

# FIXED per-table gather cost (one shared scan), seconds, indexed by tier.
# Calibrated 2026-09-02 on Census CLIMATE (~2.46M rows), degree 1:
#   estimate_percent 1% -> 0.54s ; 10% -> 2.10s ; 100% -> 21.7s
_FIXED_TIER_S = (0.54, 2.10, 21.7)
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
        """Map an abstract capacity level -> (estimate_percent, buckets)."""
        lvl = capacity.level
        if lvl not in self._ladder:
            raise KeyError(
                f"capacity level {lvl!r} not in ladder {list(self._ladder)}")
        params = self._ladder[lvl]
        return float(params["estimate_percent"]), int(params.get("buckets", 254))

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
        ep, buckets = self._native(capacity)
        by_table: dict[str, list[StatObject]] = {}
        for o in objs:
            by_table.setdefault(o.table, []).append(o)
        for table, objs_on in by_table.items():
            tname = self._q_table(table)
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
        """Oracle's native ``estimate_percent`` that realizes λ at ``level``
        (1/10/100 = the ladder's sampling knob)."""
        ep, _ = self._native(Capacity(level))
        return float(ep)

    # Oracle's representation grid: engine-faithful operating points, NOT the
    # PG scalar ``attstattarget`` list. Verified (2026-09-03, q.184 driver pair):
    # ``SIZE`` is a HARD per-object upper cap; sub-natural buckets sit on a qerr
    # cliff and >~254 adds nothing for column groups (>=~10000 is an illegal
    # method_opt literal, ORA-20000). A tiny grid {64, 254} spans "a real
    # low-resolution threshold" and "let the engine choose freely"; 254 is the
    # classic column-group bucket headroom so most objects plateau there.
    _PARAM_TIERS: tuple[int, ...] = (64, 254)
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
        ``estimate_percent`` (SIZE AUTO — natural single-col histograms), no column
        group. After this, ``estimate`` = the no-ext per-λ baseline ``e^0(S)``."""
        ep, _ = self._native(Capacity(level))
        self.restore_natural_stats(table, estimate_percent=ep)

    def build_stat_param(self, obj: StatObject, param: int) -> None:
        """Build one column group at an explicit ``param`` buckets, at the current
        λ's estimate_percent (the λ-state scan depth). Decouples the object's
        bucket count from the level ladder, matching the λ-first model."""
        ep, _ = self._native(obj.capacity)          # this λ's scan %
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
        """Fixed one-refresh GATHER cost per tier (shared scan), seconds."""
        scale = self._relrows_scale(table)
        n = max(self._ladder) + 1 if self._ladder else max(len(_FIXED_TIER_S), 1)
        out = []
        for lvl in range(n):
            if lvl not in self._ladder:
                out.append(out[-1] if out else 0.0)
                continue
            idx = min(lvl, len(_FIXED_TIER_S) - 1)
            out.append(_FIXED_TIER_S[idx] * scale)
        return tuple(out)

    def stat_maintain_var(self, obj: StatObject) -> float:
        """Marginal per-statistic refresh cost (payload only), seconds."""
        return _VAR_PER_STAT_S

    def sample_rows_per_level(self, table: str, level: int) -> Optional[float]:
        """Expected GATHER sample rows at a capacity tier.

        Oracle samples ``estimate_percent/100`` of the table's rows per scan. The
        whole table shares one scan (per_scan capacity model), so this is the
        sampling the column-group statistic sees at ``level``.
        """
        if level not in self._ladder:
            return None
        params = self._ladder[level]
        ep = float(params["estimate_percent"]) / 100.0
        n = self._num_rows(table)
        if n <= 0:
            return None
        return float(ep * n)

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

    def _relrows_scale(self, table: str) -> float:
        """Scale factor vs the calibration table (CLIMATE ~2.46M rows)."""
        tname = self._q_table(table)
        try:
            with self._cur() as cur:
                cur.execute(
                    "SELECT num_rows FROM user_tables WHERE table_name=:t",
                    {"t": tname})
                row = cur.fetchone()
            n = float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 1.0
        if n <= 0:
            return 1.0
        # floor so tiny tables don't collapse to ~0
        return max(0.1, n / 2_460_000.0)


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
            return out[0]
    except Exception:
        pass
    return sql
