"""extstats2.core — database-agnostic algorithm core.

These modules depend only on :mod:`extstats2.backend.base` abstract types and
never import a concrete backend.  The core is intentionally v1-compatible where
v1 was already generic (``optimize.py`` is an unchanged port).
"""

from .candidates import CandidateSet, generate_candidates, generate_candidates_per_query
from .measure import measure_query
from .optimize import (
    OBJECTIVE_GEOMEAN,
    OBJECTIVE_MEAN,
    OBJECTIVE_WORST,
    ILPResult,
    MaintProfile,
    OptimizerClass,
    Option,
    PhysicalStat,
    build_problem,
    select_optimizer_class,
    solve_ilp,
)
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
    "MaintProfile",
    "OptimizerClass",
    "OBJECTIVE_MEAN",
    "OBJECTIVE_GEOMEAN",
    "OBJECTIVE_WORST",
    "build_problem",
    "solve_ilp",
    "select_optimizer_class",
    "predicate_columns",
    "BenchQuery",
]
