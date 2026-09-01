"""Protocol-M driver — catalog-mask acceleration (PostgreSQL, abstracted).

v1's ``measure_mask.py`` implemented the catalog-mask protocol by directly
manipulating ``pg_statistic_ext_data`` / ``pg_mcv_list``. v2 lifts the *driver*
of that protocol into ``plan`` while keeping the actual catalog manipulation
behind the :class:`~extstats2.backend.catalog.CatalogDriver` abstraction, so:

- the driver logic (back up -> mask all but one -> measure -> restore) is
  written once and is backend-oblivious;
- a backend that can inline-mask its statistics (PG via ``pg_statistic_ext_data``)
  supplies a ``CatalogDriver`` implementing ``backup_payloads``; any backend that
  cannot (Oracle) leaves those methods unimplemented and the plan layer falls
  back to Protocol-A.

The driver itself does not know about ``stxdmcv``, ``pg_mcv_list``, ``stxoid``,
or ``ALTER STATISTICS`` — those live in ``backend/postgres.py``'s CatalogDriver.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

from ..backend.base import Backend, StatObject
from ..backend.catalog import CatalogDriver


def backup_payloads(driver: CatalogDriver, objs: list[StatObject]):
    """Back up ``objs``' catalog payloads via the driver."""
    return driver.backup_payloads(objs)


@contextmanager
def isolate_mask(
    backend: Backend,
    driver: CatalogDriver,
    keep: set[StatObject],
    table: str,
    all_on_table: Optional[Iterable[StatObject]] = None,
) -> Iterator[None]:
    """Measure context that activates only ``keep`` by masking all others.

    ``all_on_table`` may be supplied to avoid a catalog scan; when omitted it is
    fetched via ``driver.list_on_table(table)``.

    Everything is restored on exit (including on exception): we back up every
    present payload, NULL every one not in ``keep``, yield, then restore all.
    """
    present = set(all_on_table) if all_on_table is not None else set(driver.list_on_table(table))
    # Only those with a payload are maskable; keep and present must both exist.
    maskable = present
    backup = driver.backup_payloads(list(maskable))
    try:
        backup.mask_all_but(keep & maskable)
        yield
    finally:
        backup.restore()
        backup.close()
