"""Backend abstraction — the seam between the core algorithm and a database.

This is the *only* surface that ``core/`` is allowed to depend on. A backend
implementation (``backend/postgres.py``, ``backend/oracle.py``, ...) fills in
all the details the core cannot know: DDL syntax, how statistics are built,
how the optimizer's cardinality estimate is read out, and the per-object on-disk
size / catalog access.

Design goals
------------
1. *Minimal*: the interface is just large enough for the three core phases
   (candidate measurement, budget costing, verification) to run.
2. *Stable*: ``core/`` is written against these types, so changing the interface
   means changing core.  Keep it small and backend-agnostic.
3. *Two protocols*: Protocol-A (create -> build -> measure -> drop -> rebuild)
   is the *universal* baseline every backend supports.  Protocol-M (catalog-mask)
   is an optional acceleration; a backend declares support via
     :meth:`Backend.supports_catalog_mask` and overrides :meth:`Backend.isolate`.

   Protocol-A cost accounting (one measured (candidate, level) — see
   ``core/measure.py``): ``build`` is the ONLY sampling scan — PG issues one
   ANALYZE (``build_stats``), Oracle one GATHER_TABLE_STATS that both creates and
   materialises its column groups. ``create`` is DDL-only (PG) / a no-op (Oracle);
   ``measure`` = ``estimate`` (EXPLAIN, no scan) + ``stat_size_bytes`` (catalog
   read); there is NO second ANALYZE/GATHER to re-read the statistic.
   "drop -> rebuild" is the isolation layer restoring the table's *other* pre-
   existing statistics (or a fair natural single-column baseline via
   ``restore_natural_stats``) after the measurement — it is environment restore,
   not a second pass for the candidate being measured, and only triggers when
   such surrounding state actually needs putting back.

These types replace the PostgreSQL-specific plumbing in v1
(``measure.py``, ``measure_mask.py``, ``estimate.py``, ``stats.py``, the
``pg_statistic_ext_data`` catalog reads): the SQL / catalog knowledge now lives
inside each backend, not in the shared core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .capabilities import Capability, Capacity


# ---------------------------------------------------------------------------
# Shared value types (backend-agnostic)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatObject:
    """A concrete statistical object to create / measure / drop.

    This abstracts away "a statistic on (table, columns) at some capacity":

    - PostgreSQL: a ``CREATE STATISTICS`` object named ``name``; capacity is a
      ``statistics_target``.
    - Oracle: a column group / extended statistic (possibly a hidden column);
      capacity is an ``estimate_percent`` / bucket count.

    ``name`` is a backend-generated, writable identifier used in both DDL and
    catalog queries. ``capability`` and ``capacity`` are core concepts the
    backend maps to native parameters.
    """

    table: str                       # qualified table name (backend-specific form)
    columns: tuple[str, ...]         # ordered column set (canonical order)
    capability: Capability           # which capability this object implements
    capacity: Capacity               # abstract capacity level
    name: str = ""                   # backend-generated identifier

    @property
    def key(self) -> str:
        """Deterministic dedup key (backend-agnostic) for grouping/physical
        identity across queries (a shared physical statistic is paid once)."""
        return f"{self.table}|{','.join(self.columns)}|{self.capability.name}|{self.capacity.level}"


@dataclass(frozen=True)
class Estimate:
    """A cardinality estimate for one query (abstracts EXPLAIN / DBMS_XPLAN)."""

    estimate: int                    # optimizer-estimated output cardinality
    raw: Any = None                  # raw plan (JSON for PG, XPLAN text for Oracle, ...)
    actual: Optional[int] = None     # ground truth, when known

    @property
    def qerror(self) -> Optional[float]:
        """q-error vs ``actual``; ``None`` when actual is unknown (or <= 0)."""
        if self.actual is None or self.actual <= 0:
            return None
        return qerror(self.estimate, self.actual)


class IsolationCtx(Protocol):
    """A context manager that isolates one statistic's effect for measurement.

    The core uses ``with backend.isolate(keep, table): <measure>`` and never
    needs to know whether the backend is doing Protocol-A (drop/rebuild) or
    Protocol-M (catalog mask).  The backend guarantees the database is left in
    the *pre-isolation* state when the with-block exits (even on exception).
    """

    def __enter__(self) -> "IsolationCtx": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...


# ---------------------------------------------------------------------------
# Structural-properties contract (see docs/architecture.md §1.9)
# ---------------------------------------------------------------------------

# Maintenance-cost structure of the backend's statistics refresh.
class MaintStructure:
    """Enum-like constants for :attr:`StructuralProps.maint_structure`.

    - ``FIXED_VAR``: refresh cost = fixed sampling base + additive per-statistic
      update term (PostgreSQL's ANALYZE).
    - ``FIXED_ONLY``: refresh cost is dominated by a fixed sampling term, and
      per-statistic update cost is negligible (e.g. Oracle column groups sharing
      one scan).
    """
    FIXED_VAR = "fixed+var"
    FIXED_ONLY = "fixed_only"


@dataclass(frozen=True)
class StructuralProps:
    """A backend's declaration of the structural properties of its estimation /
    cost model.  The core uses this to select the optimizer *class* and to
    instantiate its constraints / objective (soft selection — see §1.9).

    Decisive dimensions (choose the optimizer class):
      sparse_one_stat, disjoint_supported
    Instance-parameter dimensions (fill the chosen class):
      maint_structure, capacity_model, protocol_m (already on Backend)
    """

    # -- decisive dimensions ----------------------------------------------
    # True when this backend can support "one statistic per query captures the
    # dominant correlation", letting the objective be exactly linear (the
    # sparse-linear MILP class).  When False, the core falls back to the general
    # multiplicative MILP class.
    sparse_one_stat: bool = True
    # True when column-disjoint selection of statistics can be enforced so the
    # joint effect on a query is independent (no planner interference), making
    # the estimation model trustworthy.  Decides the pruning constraints applied.
    disjoint_supported: bool = True

    # -- instance-parameter dimensions -------------------------------------
    # Maintenance cost structure (MaintStructure.*).  Determines how the
    # maintenance budget constraint is instanced (per-statistic vs table-fixed).
    maint_structure: str = MaintStructure.FIXED_VAR
    # Capacity model: "per_stat" (e.g. PG statistics_target per object) or
    # "per_scan" (e.g. Oracle estimate_percent per GATHER). Determines how the
    # capacity ladder drives storage/maintenance costs.
    capacity_model: str = "per_stat"
    # Objective aggregations for which this backend can provide a *trustworthy*
    # estimate.  Subset of {"mean","geomean","worst","p90"}.
    supports_objectives: tuple[str, ...] = ("mean",)


# ---------------------------------------------------------------------------
# The backend interface
# ---------------------------------------------------------------------------

class Backend(ABC):
    """Abstract interface every database backend implements."""

    # -- metadata ----------------------------------------------------------

    @abstractmethod
    def name(self) -> str:
        """Short backend identifier, e.g. ``"postgres"`` / ``"oracle"``."""

    @abstractmethod
    def supported_capabilities(self) -> list[Capability]:
        """Capabilities this backend can build, each mapped to its native kind.

        Unsupported canonical capabilities are included with ``supported=False``
        so the core can still list them but must not create them.
        """

    def structural_props(self) -> StructuralProps:
        """Declare the backend's structural-properties contract (§1.9).

        The core uses this for *soft selection* of the optimizer class (see
        :func:`extstats2.core.optimize.select_optimizer_class`): the decisive
        dimensions pick between the sparse-linear MILP and the general
        multiplicative MILP, and the instance params fill the chosen class.
        Backends that do not override this declare a permissive default
        (sparse + disjoint + fixed+var + mean), which is what PostgreSQL offers.
        """
        return StructuralProps()

    # -- lifecycle / DDL ---------------------------------------------------

    @abstractmethod
    def create_stat(self, obj: StatObject) -> None:
        """Create the statistic object (no build yet)."""

    @abstractmethod
    def drop_stat(self, obj: StatObject) -> None:
        """Drop the statistic object."""

    @abstractmethod
    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        """Build (ANALYZE / GATHER) the given objects at ``capacity``.

        In PG this also rebuilds the single-column stats; the backend owns that
        detail. Called with the session capacity already set by the caller where
        relevant (see :meth:`set_capacity`).
        """

    # -- capacity ----------------------------------------------------------

    @abstractmethod
    def set_capacity(self, capacity: Capacity) -> None:
        """Set the session/global 'how much' knob to ``capacity``.

        PG: ``SET default_statistics_target = <level>`` (or per-object
        ``ALTER STATISTICS ... SET STATISTICS``). Oracle: nothing global — the
        estimate_percent is passed to ``build_stats`` instead. Backends that
        cannot express this as a session knob should document / no-op.
        """

    # -- estimation --------------------------------------------------------

    @abstractmethod
    def estimate(self, query: "BenchQuery") -> Estimate:
        """Estimate the cardinality of ``query`` and return it as :class:`Estimate`.

        ``query.sql`` is a ``SELECT COUNT(*) ... WHERE ...`` benchmark query; the
        backend rewrites it to ``SELECT * ...`` so the top-level plan rows equal
        the filtered input cardinality (same trick as v1 ``estimate.py``), then
        reads the estimate from its native plan output.
        """

    # -- catalog -----------------------------------------------------------

    @abstractmethod
    def stat_size_bytes(self, obj: StatObject) -> int:
        """On-disk size in bytes of the statistic, from the native catalog."""

    @abstractmethod
    def list_stats(self, table: str) -> list[StatObject]:
        """Return the statistics currently present on ``table`` (for verify)."""

    # -- maintenance cost --------------------------------------------------

    def table_maintain_tiers(self, table: str) -> tuple[float, ...]:
        """Fixed ANALYZE/GATHER cost ladder for one deployed refresh of ``table``.

        Returns a tuple indexed by *target tier* (the abstract capacity level):
        ``base[k]`` = one-refresh fixed cost when the highest target level
        selected on ``table`` is `k`.  This is the *per-table* fixed cost shared
        across all statistics on the table (the sampling scan is paid once per
        ANALYZE, at the max target — targrows semantics).

        Default returns all zeros so backends that don't track maintenance cost
        degrade gracefully (the ILP then ignores the maintenance budget).
        Backends should also implement :meth:`stat_maintain_var`.
        """
        return (0.0,)

    def stat_maintain_var(self, obj: StatObject) -> float:
        """Marginal per-statistic refresh cost of ``obj`` (payload update term).

        This is the *variable* part: recomputing this statistic's payload on
        the shared sample.  It is added per created statistic in the ILP, while
        the fixed scan cost is charged once per activated table via
        :meth:`table_maintain_tiers`.

        Default returns ``0.0``.
        """
        return 0.0

    # -- sampling / fidelity contract (v1 "λ" expected-capture) -----------

    def sample_rows_per_level(self, table: str, level: int) -> Optional[float]:
        """Expected number of table rows one ANALYZE/GATHER samples at ``level``.

        This is the denominator for v1's ``λ`` ("expected capture") statistic:
        for a query that filters down to ``truth`` rows on a table of ``N`` rows,
        the driving combination is expected to appear in the statistic's sample
        ``lambda_expected = (truth / N) * sample_rows`` times.  When
        ``lambda_expected`` is far below 1, a single ANALYZE *may not even see*
        the combination, so the measured q-error is high-variance / unreliable
        (v1 Sec.8 "fidelity" / query.184); when it is well above 1 the
        measurement is faithful.  This is a data/`level` property the core can
        reason about without knowing the engine's sampling formula.

        Returns ``None`` when the backend cannot estimate it for ``table``.
        """
        return None

    def num_rows(self, table: str) -> Optional[float]:
        """Estimated row count of ``table`` (None if unknown)."""
        return None

    # -- lambda-state realization (sampling-first model, §7bis) -----------

    def lambda_sampling_rows(self, table: str, level: int) -> Optional[float]:
        """Rows the shared ANALYZE/GATHER samples at λ-tier ``level`` (= S_level).

        Alias for :meth:`sample_rows_per_level` (the sample-tier ``S``). Default
        delegates; backends may override if the λ coordinate differs.
        """
        return self.sample_rows_per_level(table, level)

    def lambda_sampling_percent(self, table: str, level: int) -> Optional[float]:
        """The engine's native percent-of-scan that realizes λ at ``level``.

        ``None`` on engines (PG) that realize λ via a single-column *target*
        rather than a percent; Oracle returns its ``estimate_percent`` (1/10/100).
        Recorded in the corpus ``_meta.json`` (results/measure) so the same
        abstract λ level maps to a backend-specific sampling knob for
        cross-backend comparison.
        """
        return None

    def representation_param_tiers(self, table: str | None = None) -> tuple[int, ...]:
        """The representation-param sampling points this *backend* offers for its
        extended-stat objects, in its OWN units (NOT a cross-backend shared list).

        This makes concrete the "param is not cross-backend / not necessarily a
        single scalar" decision (§7bis 2nd 定稿 + architecture.md): PG expresses
        representation as a scalar ``attstattarget`` and can take large values
        (25..10000 with real differentiation), whereas Oracle's ``SIZE`` is a
        hard per-object upper cap with a "natural bucket" plateau — sub-natural
        SIZE values sit on a quality cliff and >~254 adds nothing for column
        groups (and >=10000 is an illegal literal). Backends override with the
        points that produce meaningfully distinct (qerr, size) operating points.
        """
        return (25, 50, 100, 1000, 10000)

    def max_param_at_level(self, table: str, level: int) -> Optional[float]:
        """Lattice cap on a representation param at λ-tier ``level``: ``S/300``.

        On PG this equals the single-column target (``S_level/300``). Backends
        return ``None`` if the cap is not engine-imposed (Oracle) — the caller
        then applies it as an optional guard.
        """
        rows = self.lambda_sampling_rows(table, level)
        return None if rows is None else rows / 300.0

    def single_col_target_for_level(self, table: str, level: int) -> Optional[int]:
        """PG's native handle realizing λ at ``level``: set every single column
        to this attribute target. Oracle returns ``None`` (it uses
        ``estimate_percent`` instead)."""
        cap = self.max_param_at_level(table, level)
        return None if cap is None else int(cap)

    def enter_lambda_state(self, table: str, level: int) -> None:
        """Realize λ-tier ``level`` on ``table``: all single columns at
        ``S_level/300`` (PG) / a single-col gather at depth (Oracle), and **no
        extended statistic present**. After this, ``estimate`` gives the no-ext
        per-λ baseline ``e^0(S_level)``; a candidate object added at
        ``param ≤ S_level/300`` shares this established deep scan (free-rider).
        Backends must override to actually realize the depth; the default only
        sets the abstract capacity and is not physically meaningful.
        """
        self.set_capacity(Capacity(level))

    def build_stat_param(self, obj: StatObject, param: Any) -> None:
        """Build a single extended statistic ``obj`` at an explicit representation
        ``param``, in the current (λ) state whose shared scan depth is set by the
        single columns. This decouples the object's param from any capacity-level
        mapping. Backends override; default falls back to a normal build."""
        self.build_stats([obj], obj.capacity)

    def build_stat_params_batch(self, objs_params: list[tuple[StatObject, int]]) -> None:
        """Protocol-M batch: build several objects at their OWN params, de-
        amortizing the shared (λ-state) scan into ONE ANALYZE instead of N.

        PG overrides (SET each object's target, then ANALYZE once). The default
        (no inline-mask backend) falls back to per-object ``build_stat_param`` —
        correct but slower (Protocol-A), used only when the backend cannot batch.
        """
        for obj, param in objs_params:
            self.build_stat_param(obj, param)

    def supports_catalog_mask(self) -> bool:
        """Whether this backend can inline-mask statistic payloads (Protocol-M).

        PostgreSQL does (NULL-mask ``pg_statistic_ext_data``); Oracle cannot, so
        it returns False and measurement falls back to Protocol-A semantics."""
        return False

    def catalog_driver(self) -> Optional["CatalogDriver"]:
        """Return this backend's catalog driver for Protocol-M masking, or None."""
        return None

    # -- isolation ---------------------------------------------------------

    @abstractmethod
    def isolate(self, keep: set[StatObject], table: str) -> IsolationCtx:
        """Return an isolation context that makes *only* ``keep`` active on
        ``table`` while measuring, then restores everything.

        Default universal implementation is Protocol-A (drop the rest, rebuild
        after).  PostgreSQL overrides this with Protocol-M (NULL-mask in
        ``pg_statistic_ext_data``).
        """


# ---------------------------------------------------------------------------
# Helpers used by any backend / the q-error logic (kept here, DB-agnostic)
# ---------------------------------------------------------------------------

def qerror(estimate: int | float, actual: int | float) -> float:
    """Factor q-error ``max(e,a)/min(e,a)`` (>= 1). Zero-sensitive handling
    matches v1: degenerate zero cases floor to a small factor."""
    if estimate <= 0 or actual <= 0:
        if actual == 0 and estimate == 0:
            return 1.0
        lo = min(actual, estimate)
        hi = max(actual, estimate)
        if lo <= 0:
            lo = 1.0
        return hi / lo
    return max(estimate, actual) / min(estimate, actual)
