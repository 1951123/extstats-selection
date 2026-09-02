"""Benchmark loaders — turn benchmark query files into ``BenchQuery`` lists.

v2 supports Census and stats_CEB (including its single-table sub-plans). Each
loader reads a query text file under ``benchmarks/<name>/queries/`` and returns
a list of :class:`extstats2.core.queries.BenchQuery`, which is backend-agnostic.

This package is a port of v1's ``parsers/`` (minus JOB, which v2 drops).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import BENCHMARKS_DIR
from ..core.queries import BenchQuery

from .census import load_census
from .stats_ceb import load_stats_ceb
from .stats_ceb_single import load_stats_ceb_single

#: Canonical benchmark names -> (loader, subdirectory under ``benchmarks/`` that
#: holds the query files).  stats_ceb and stats_ceb_single share one directory.
_LOADERS: dict[str, tuple[Callable[[Path], list[BenchQuery]], str]] = {
    "census": (load_census, "Census"),
    "stats_ceb": (load_stats_ceb, "stats_CEB"),
    "stats_ceb_single": (load_stats_ceb_single, "stats_CEB"),
}


def supported_benches() -> tuple[str, ...]:
    """Return the canonical benchmark names (stable order)."""
    return tuple(_LOADERS)


def queries_dir_for(bench: str) -> Path:
    """Return the queries directory for ``bench`` under ``BENCHMARKS_DIR``."""
    if bench not in _LOADERS:
        raise ValueError(
            f"unknown benchmark {bench!r}; supported: {sorted(_LOADERS)}"
        )
    _, subdir = _LOADERS[bench]
    return BENCHMARKS_DIR / subdir / "queries"


def load_benchmark(bench: str) -> list[BenchQuery]:
    """Load a benchmark's queries from its ``queries/`` directory.

    Raises ``ValueError`` for unsupported benchmark names.
    """
    if bench not in _LOADERS:
        raise ValueError(
            f"unknown benchmark {bench!r}; supported: {sorted(_LOADERS)}"
        )
    loader, _ = _LOADERS[bench]
    return loader(queries_dir_for(bench))


__all__ = [
    "BenchQuery",
    "load_benchmark",
    "load_census",
    "load_stats_ceb",
    "load_stats_ceb_single",
    "supported_benches",
    "queries_dir_for",
]
