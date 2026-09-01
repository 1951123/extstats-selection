"""Cross-backend query representation (port of v1 ``parsers/base.py``).

The benchmark query object is already database-agnostic in v1, so it carries
over unchanged. Parsers convert each benchmark's textual format into these; the
core algorithm and the backend estimation layer both consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class BenchQuery:
    """One parsed query from a benchmark."""

    bench: str                       # "census" | "job" | "stats_ceb" | ...
    qid: str                         # short id, e.g. "1a", "query.1"
    sql: str                         # SQL text (without the ground-truth suffix)
    ground_truth: Optional[int] = None


class QueryParser(Protocol):
    """Protocol implemented by each benchmark's loader."""

    def __call__(self, queries_dir: Path) -> list[BenchQuery]:
        ...


def iter_sql_files(queries_dir: Path) -> list[Path]:
    """Return sorted ``*.sql`` paths directly under ``queries_dir``."""
    return sorted(queries_dir.glob("*.sql"))
