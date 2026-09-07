"""extstats2.core — database-agnostic algorithm core.

These modules depend only on :mod:`extstats2.backend.base` abstract types and
never import a concrete backend.

CANONICAL (S-grid / lambda-first) entry points are re-exported here:
  - measurement   : measure_lambda (+ measure_lambda_io)  — per-λ S-grid corpus;
  - outer λ-stage : optimize_lambda (load_lambda_problem, build_inner_at_level,
                    inner_optimal_at_level, search_lambda);
  - generic inner : optimize (OptimizerClass / solve_ilp / build_problem ...);
  - basics        : candidates / predicates / queries.

The old v1 / capacity-era ``measure_query`` is NOT re-exported here (see the
NOTE below the imports); it stays reachable under ``extstats2.core.measure``
for the legacy evaluators / tests only.
"""

from .candidates import CandidateSet, generate_candidates, generate_candidates_per_query
from .measure_lambda import (DEFAULT_LAMBDA_LEVELS, measure_query_lambda,
                             measure_query_lambda_m, measure_workload_lambda)
from .measure_lambda_io import (LambdaTier, Meta, list_qids, load_workload,
                                read_meta, read_query_measure, result_dir,
                                write_meta, write_query_measure)
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
from .optimize_lambda import (build_inner_at_level, inner_optimal_at_level,
                              load_lambda_problem, search_lambda)
from .predicates import predicate_columns
from .queries import BenchQuery

# NOTE (legacy-cleaning batch-1): the old v1 / capacity-era measurement
# ``measure_query`` is intentionally NOT re-exported here — it lives under
# ``extstats2.core.measure`` (submodule) for the legacy evaluators / tests and is
# otherwise unreachable from the package surface.  ``core.measure_lambda`` is the
# canonical S-grid measurement; ``core.optimize_lambda`` is the canonical outer
# λ-selection driver.

__all__ = [
    # canonical S-grid measurement + corpus IO
    "DEFAULT_LAMBDA_LEVELS",
    "measure_query_lambda",
    "measure_query_lambda_m",
    "measure_workload_lambda",
    "LambdaTier",
    "Meta",
    "result_dir",
    "write_meta",
    "read_meta",
    "write_query_measure",
    "read_query_measure",
    "list_qids",
    "load_workload",
    # canonical outer λ-selection + inner generic MILP
    "load_lambda_problem",
    "build_inner_at_level",
    "inner_optimal_at_level",
    "search_lambda",
    "OptimizerClass",
    "Option",
    "PhysicalStat",
    "MaintProfile",
    "ILPResult",
    "build_problem",
    "solve_ilp",
    "select_optimizer_class",
    # candidates / predicates / queries
    "CandidateSet",
    "generate_candidates",
    "generate_candidates_per_query",
    "predicate_columns",
    "BenchQuery",
]
