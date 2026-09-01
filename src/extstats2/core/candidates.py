"""Candidate column-combination generation (port of v1 ``candidates.py``).

Produces *candidate* statistic definitions from per-query, per-base-table
predicate columns. This module lives at the *column-set* abstraction level and
is backend-agnostic: it does not mention PostgreSQL kinds or Oracle column
groups. A candidate is simply ``(table, columns)``; which capabilities apply is
decided later by the backend's ``supported_capabilities()``.

Algorithm (unchanged from v1)
-----------------------------
For each query, for each base table with ``>= 2`` predicate columns, consider all
column combinations of size in ``arities`` (default 2..3).

Granularity
-----------
- :func:`generate_candidates_per_query` groups by query (dedup within a query).
- :func:`generate_candidates` collapses identical ``(table, columns)`` across the
  whole workload so each physical statistic is created once.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Iterator

from .predicates import predicate_columns
from .queries import BenchQuery


@dataclass(frozen=True)
class CandidateSet:
    """A combination of columns on a single base table."""

    table: str                       # qualified table name (backend-specific form)
    columns: tuple[str, ...]         # canonical (sorted) column set

    @property
    def table_unqualified(self) -> str:
        return self.table.rpartition(".")[2]

    def __repr__(self) -> str:
        return f"<CandidateSet {self.table}({', '.join(self.columns)})>"


def iter_candidates_for_query(
    query: BenchQuery,
    arities: Iterable[int] = (2, 3),
) -> Iterator[CandidateSet]:
    """Yield the candidate combinations for a single query (dedup within it).

    Yields each combination once in canonical column order (the backend treats
    column order as insignificant for its extended statistics, matching v1).
    """
    pred: dict[str, set[str]] = predicate_columns(query)
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for table, cols in pred.items():
        sorted_cols = sorted(cols)
        if len(sorted_cols) < 2:
            continue
        for arity in sorted(set(arities)):
            if arity < 2 or arity > len(sorted_cols):
                continue
            for combo in combinations(sorted_cols, arity):
                key = (table, combo)
                if key not in seen:
                    seen.add(key)
                    yield CandidateSet(table=table, columns=combo)


def generate_candidates_per_query(
    queries: list[BenchQuery],
    arities: Iterable[int] = (2, 3),
) -> dict[str, list[CandidateSet]]:
    """Group candidates by query id (v1 granularity: per-query gains)."""
    out: dict[str, list[CandidateSet]] = {}
    for q in queries:
        out[q.qid] = list(iter_candidates_for_query(q, arities))
    return out


def generate_candidates(
    queries: list[BenchQuery],
    arities: Iterable[int] = (2, 3),
) -> list[CandidateSet]:
    """Collapse identical (table, columns) combos across the whole workload."""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    out: list[CandidateSet] = []
    for q in queries:
        for cand in iter_candidates_for_query(q, arities):
            key = (cand.table, cand.columns)
            if key not in seen:
                seen.add(key)
                out.append(cand)
    return out


def candidates_by_table(
    candidates: Iterable[CandidateSet],
) -> dict[str, list[CandidateSet]]:
    """Group a flat candidate list by base table (for batched build/measure)."""
    by_table: dict[str, list[CandidateSet]] = defaultdict(list)
    for cand in candidates:
        by_table[cand.table].append(cand)
    return dict(by_table)
