"""extstats2.core — database-agnostic algorithm core.

These modules depend only on :mod:`extstats2.backend.base` abstract types and
never import a concrete backend.  The core is intentionally v1-compatible where
v1 was already generic (``optimize.py`` is an unchanged port).
"""

from .candidates import CandidateSet, generate_candidates, generate_candidates_per_query
from .measure import measure_query
from .optimize import ILPResult, Option, PhysicalStat, build_problem, solve_ilp
from .predicates import predicate_columns
from .queries import BenchQuery

__all__ = [
    "CandidateSet",
    "generate_candidates",
    "generate_candidates_per_query",
    "measure_query",
    "ILPResult",
    "Option",
    "PhysicalStat",
    "build_problem",
    "solve_ilp",
    "predicate_columns",
    "BenchQuery",
]
