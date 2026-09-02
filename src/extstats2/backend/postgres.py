"""PostgreSQL 16 backend (placeholder).

**Milestone M2.** This file will port v1's PostgreSQL logic onto the backend
abstraction:

- Protocol-A measurement (was ``measure.py``),
- Protocol-M catalog-mask acceleration (was ``measure_mask.py``; implements
  ``CatalogDriver`` over ``pg_statistic_ext_data`` / ``pg_mcv_list``),
- cardinality estimation (was ``estimate.py``, via ``EXPLAIN (FORMAT JSON)``),
- DDL generation (was ``stats.py``), and per-object size via ``pg_statistic_ext_data``.

The current skeleton exists so :func:`extstats2.config.get_backend("postgres")`
is importable and the abstraction contracts are exercised; the real migration
fills in the SQL / catalog code here (never in ``core/``).
"""

from __future__ import annotations

from .base import Backend, Estimate, IsolationCtx, MaintStructure, StatObject, StructuralProps
from ..backend.capabilities import Capability, Capacity
from ..core.queries import BenchQuery

_CAPABILITIES = [
    Capability("dependency", "dependencies", "statistics_target", True),
    Capability("ndistinct", "ndistinct", "statistics_target", True),
    Capability("mcv", "mcv", "statistics_target", True),
]


class PostgresBackend(Backend):
    """PostgreSQL 16 backend. TODO(M2): implement against v1 migration notes."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def name(self) -> str:
        return "postgres"

    def supported_capabilities(self) -> list[Capability]:
        return list(_CAPABILITIES)

    def structural_props(self) -> StructuralProps:
        # PostgreSQL supports the sparse-linear class and column-disjoint pruning
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
        # PG supports catalog-mask acceleration (Protocol-M). The mask driver is
        # implemented in M2; the capability flag is already true so the protocol
        # selection logic in ``backend.protocol()`` behaves as designed.
        return True

    # -- lifecycle / DDL (M2: CREATE/DROP STATISTICS) ----------------------
    def create_stat(self, obj: StatObject) -> None:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def drop_stat(self, obj: StatObject) -> None:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def set_capacity(self, capacity: Capacity) -> None:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def estimate(self, query: BenchQuery) -> Estimate:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def stat_size_bytes(self, obj: StatObject) -> int:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def list_stats(self, table: str) -> list[StatObject]:
        raise NotImplementedError("postgres backend is a placeholder (M2)")

    def isolate(self, keep: set[StatObject], table: str) -> IsolationCtx:
        # M2: override to use Protocol-M (catalog mask) via CatalogDriver.
        raise NotImplementedError("postgres backend is a placeholder (M2)")
