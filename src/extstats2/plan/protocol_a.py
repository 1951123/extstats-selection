"""Protocol-A driver — the universal, backend-oblivious isolation protocol.

Every backend can support Protocol-A: build the statistic, measure, drop it,
and rebuild the table's other statistics to restore prior state. The core
measurement scheduler in ``core/measure.py`` drives this through
``backend.isolate()``, whose default universal implementation is
``protocol_a_isolate`` in ``backend/base.py``.

This module documents the driver contract and provides a thin wrapper; it is
kept separate from ``core`` so the *protocol choice* stays an explicit concern
(see :mod:`extstats2.plan`) rather than being baked into the scheduler.
"""

from __future__ import annotations

from ..backend.base import Backend, protocol_a_isolate


def protocol_a_isolate_ctx(backend: Backend, keep: set, table: str):
    """Return a Protocol-A isolation context for ``backend``.

    This is just a re-export of the reference implementation in ``base.py`` so
    plan-level code can refer to it unambiguously.
    """
    return protocol_a_isolate(backend, keep, table)
