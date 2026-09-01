"""extstats2.plan — measurement protocols.

Protocols sit between the core scheduler and the backend:

- ``protocol_a.py``: the universal Protocol-A (drop/rebuild isolation), available
  to every backend.
- ``protocol_m.py``: the catalog-mask Protocol-M driver (PostgreSQL only), written
  against :class:`extstats2.backend.catalog.CatalogDriver` so it stays
  backend-oblivious.

The core scheduler picks a protocol via ``backend.protocol(requested)``.
"""
