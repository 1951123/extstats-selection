"""Oracle backend (placeholder).

**Milestone M3.** Oracle has no PostgreSQL-style extended-statistics objects;
the native mechanism is *extended/column-group statistics* created through
``DBMS_STATS``:

- ``DBMS_STATS.GATHER_TABLE_STATS(..., method_opt => 'FOR COLUMNS (a,b,c) SIZE <n>')``
  builds a multi-column histogram / column group;
- ``DBMS_STATS.CREATE_EXTENDED_STATS`` creates a named extension (a hidden
  column like ``SYS_STU...``);
- cardinality is read from ``EXPLAIN PLAN`` + ``DBMS_XPLAN.DISPLAY`` (``Cardinality``);
- catalog: ``USER_STAT_EXTENSIONS`` / ``USER_TAB_COL_STATISTICS``.
- No catalog-mask acceleration -> ``has_protocol_m() == False``, using Protocol-A.

Capability mapping (first cut): ``mcv`` (column-group histogram) and ``ndistinct``
(column group) supported; ``dependency`` marked unsupported.

The current skeleton makes ``config.get_backend("oracle")`` importable; the real
implementation is M3.
"""

from __future__ import annotations

from ..backend.base import Backend, Estimate, IsolationCtx, StatObject
from ..backend.capabilities import Capability, Capacity
from ..core.queries import BenchQuery

_CAPABILITIES = [
    # dependency: not implemented in the first cut.
    Capability("dependency", "none", "n/a", supported=False),
    Capability("ndistinct", "column_group", "estimate_percent", True),
    Capability("mcv", "column_group", "estimate_percent", True),
]


class OracleBackend(Backend):
    """Oracle backend using DBMS_STATS column groups + Protocol-A. TODO(M3)."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def name(self) -> str:
        return "oracle"

    def supported_capabilities(self) -> list[Capability]:
        return list(_CAPABILITIES)

    def has_protocol_m(self) -> bool:
        return False

    def create_stat(self, obj: StatObject) -> None:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def drop_stat(self, obj: StatObject) -> None:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def build_stats(self, objs: list[StatObject], capacity: Capacity) -> None:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def set_capacity(self, capacity: Capacity) -> None:
        # Oracle has no global 'how much' knob; estimate_percent is passed in
        # build_stats. No-op is acceptable here.
        return

    def estimate(self, query: BenchQuery) -> Estimate:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def stat_size_bytes(self, obj: StatObject) -> int:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def list_stats(self, table: str) -> list[StatObject]:
        raise NotImplementedError("oracle backend is a placeholder (M3)")

    def isolate(self, keep: set[StatObject], table: str) -> IsolationCtx:
        # Universal Protocol-A (see base.protocol_a_isolate).
        from ..backend.base import protocol_a_isolate
        return protocol_a_isolate(self, keep, table)
