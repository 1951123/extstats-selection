"""extstats2.core — database-agnostic algorithm core.

These modules depend only on :mod:`extstats2.backend.base` abstract types and
never import a concrete backend.

CANONICAL (S-grid / sample-first) entry points are re-exported here:
  - measurement   : measure_sampling (+ measure_io)  — per-level S-grid corpus;
  - outer S-grid  : optimize_sgrid (load_sgrid_problem, build_inner_at_level,
                    inner_optimal_at_level, search_sgrid) — choose level S, then
                    inner representation MILP at that level;
  - generic inner : optimize (OptimizerClass / solve_ilp / build_problem ...);
  - basics        : candidates / predicates / queries.

The model keys measurement by the REQUESTED sampling level ``S``; per table the
realized sample is ``min(S, N_t)`` and the sampling fraction
``lambda = min(S, N_t)/N_t`` is a *derived reporting metric*, not a search axis.

The legacy v1 / capacity-era ``measure.py`` has been removed (single-version
policy); measurements go through ``measure_sampling`` only.
"""

from .candidates import CandidateSet, generate_candidates, generate_candidates_per_query
from .measure_sampling import (DEFAULT_SAMPLING_LEVELS, measure_query_sampling,
                             measure_query_sampling_m, measure_workload_sampling)
from .measure_io import (SampleTier, Meta, list_qids, load_workload,
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
from .optimize_sgrid import (build_inner_at_level, inner_optimal_at_level,
                              load_sgrid_problem, search_sgrid)
from .predicates import predicate_columns
from .queries import BenchQuery

__all__ = [
    # canonical S-grid measurement + corpus IO
    "DEFAULT_SAMPLING_LEVELS",
    "measure_query_sampling",
    "measure_query_sampling_m",
    "measure_workload_sampling",
    "SampleTier",
    "Meta",
    "result_dir",
    "write_meta",
    "read_meta",
    "write_query_measure",
    "read_query_measure",
    "list_qids",
    "load_workload",
    # canonical outer S-grid selection + inner generic MILP
    "load_sgrid_problem",
    "build_inner_at_level",
    "inner_optimal_at_level",
    "search_sgrid",
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
