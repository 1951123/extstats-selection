"""extstats2.core — database-agnostic algorithm core.

These modules depend only on :mod:`extstats2.backend.base` abstract types and
never import a concrete backend.  NOTE: `measure_query` re-exported here is the
LEGACY v1/capacity-era measurement (core/measure.py); the CURRENT canonical
S-grid / lambda-first measurement & outer selection live in
core/measure_lambda (+ measure_lambda_io) and core/optimize_lambda, which are
NOT package-level re-exports yet (see core/legacy-cleaning batch-1 design).
"""

from .candidates import CandidateSet, generate_candidates, generate_candidates_per_query
from .measure import measure_query
from .optimize import (
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
    "build_problem",
    "solve_ilp",
    "select_optimizer_class",
    "predicate_columns",
    "BenchQuery",
]
