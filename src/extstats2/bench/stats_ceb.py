"""stats_CEB query loader (ported from v1 ``parsers/stats_ceb.py``).

Format (one query per line in ``stats_CEB.sql``)::

    79851||SELECT COUNT(*) FROM badges as b, users as u WHERE ...

Each line is ``<ground_truth>||<sql>``. The leading integer is the **true
cardinality** of the query (verified against the loaded ``stats`` database), so
it is used both as the ``qid`` and stored in ``ground_truth``.
"""

from __future__ import annotations

from pathlib import Path

from ..core.queries import BenchQuery


def load_stats_ceb(queries_dir: Path) -> list[BenchQuery]:
    """Load stats_CEB queries from ``queries_dir`` (must contain ``stats_CEB.sql``)."""
    path = queries_dir / "stats_CEB.sql"
    if not path.exists():
        raise FileNotFoundError(f"stats_CEB query file not found: {path}")

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if "||" not in line:
                raise ValueError(
                    f"stats_CEB query line {line_no}: missing '||' separator: {line!r}"
                )
            prefix, _, sql = line.partition("||")
            prefix = prefix.strip()
            queries.append(
                BenchQuery(
                    bench="stats_ceb",
                    qid=prefix,
                    sql=sql.strip(),
                    ground_truth=int(prefix),
                )
            )
    return queries
