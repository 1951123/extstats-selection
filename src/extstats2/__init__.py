"""extstats2 — budgeted, capacity-aware, backend-agnostic selection of
extended statistics.

v2 generalises the PostgreSQL-specific v1 toolkit so the same core algorithm
(candidate generation -> measurement -> budgeted MILP allocation -> verify) runs
against any backend implementing :class:`extstats2.backend.base.Backend`
(PostgreSQL first, Oracle next).

See ``docs/architecture.md`` for the design.
"""

__version__ = "0.1.0"
