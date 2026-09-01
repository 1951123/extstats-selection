"""Catalog model — abstracts the per-backend system catalogs that expose
statistics metadata.

v1 read PostgreSQL's ``pg_statistic_ext`` / ``pg_statistic_ext_data`` directly
(including the ``pg_mcv_list`` payload for Protocol-M masking). v2 abstracts the
*catalog* behind a small interface so:

- the core / Protocol-M driver never hard-codes ``pg_statistic_ext_data``,
  ``stxdmcv``, ``pg_mcv_list``, etc.;
- each backend provides a :class:`CatalogDriver` that answers the same questions
  ("what statistics exist", "how big is this one", "read/write this payload")
  against its own catalogs (PG: ``pg_statistic_ext_data``; Oracle:
  ``USER_TAB_COL_STATISTICS`` / ``USER_STAT_EXTENSIONS``).

Only PostgreSQL implements catalog-masking today; the masking primitives are
declared here so the Protocol-M driver in ``plan/`` stays backend-oblivious and
the Oracle backend simply does not provide them (falls back to Protocol-A).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .base import StatObject


class CatalogDriver(Protocol):
    """Per-backend access to statistics metadata."""

    def lookup_oid(self, obj: StatObject) -> int | str:
        """Return a stable catalog identifier (OID / extension name) for ``obj``."""
        ...

    def size_bytes(self, obj: StatObject) -> int:
        """On-disk size of ``obj`` in bytes, from the native catalog."""
        ...

    def list_on_table(self, table: str) -> list[StatObject]:
        """All statistics currently present on ``table`` (symbolic StatObjects)."""
        ...

    # -- payload masking (optional; PG only) ------------------------------
    # Backends that cannot inline-mask statistics leave these raising
    # NotImplementedError; the plan layer then falls back to Protocol-A.

    def backup_payloads(self, objs: list[StatObject]) -> "PayloadBackup":
        """Back up the current payload of ``objs`` so they can be masked & restored."""
        ...


class PayloadBackup(Protocol):
    """Opaque handle to a backed-up set of catalog payloads."""

    def mask_all_but(self, keep: set[StatObject]) -> None:
        """NULL/disable every backed-up payload except those in ``keep``."""
        ...

    def restore(self) -> None:
        """Restore all backed-up payloads (idempotent / safe on error)."""
        ...

    def close(self) -> None:
        """Release any temp resources (e.g. drop backup tables)."""
        ...
