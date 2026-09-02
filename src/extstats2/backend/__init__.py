"""extstats2.backend — pluggable database backends.

Each concrete backend implements :class:`extstats2.backend.base.Backend` (and,
for PostgreSQL's catalog-mask protocol, :class:`extstats2.backend.catalog.CatalogDriver`).

- ``postgres.py``: PostgreSQL 16 backend (Protocol-A + Protocol-M acceleration).
  Direct port of v1's ``measure.py`` / ``measure_mask.py`` / ``estimate.py`` /
  ``stats.py``, refactored onto the backend abstraction.
- ``oracle.py``: Oracle backend using ``DBMS_STATS`` column-group statistics and
  the universal Protocol-A (no catalog-mask).
"""

from .base import (
    Backend,
    Estimate,
    IsolationCtx,
    MaintStructure,
    StatObject,
    StructuralProps,
    qerror,
)
from .capabilities import (
    CANONICAL_CAPABILITIES,
    CAPACITY_NONE,
    Capability,
    Capacity,
)
from .catalog import CatalogDriver, PayloadBackup

__all__ = [
    "Backend",
    "Estimate",
    "IsolationCtx",
    "StatObject",
    "StructuralProps",
    "MaintStructure",
    "qerror",
    "Capability",
    "Capacity",
    "CAPACITY_NONE",
    "CANONICAL_CAPABILITIES",
    "CatalogDriver",
    "PayloadBackup",
]
