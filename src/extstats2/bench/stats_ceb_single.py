"""stats_CEB single-table (sub-plan) query loader (ported from v1
``parsers/stats_ceb_single.py``).

Each line in ``stats_CEB_single_table.sql`` looks like::

    SELECT COUNT(*) FROM badges as b;||0||79851

i.e. ``<sql>||<subplan_id>||<ground_truth>``. The SQL is a SINGLE-table
selection-predicate query (no joins). Because extended statistics cannot be
used for join estimation, these pure single-table selection queries are the
prime target of the method ("Census-like" single-table benchmark on the same
schema as the join workload).
"""

from __future__ import annotations

from pathlib import Path

from ..core.queries import BenchQuery


def load_stats_ceb_single(queries_dir: Path) -> list[BenchQuery]:
    """Load single-table stats_CEB queries from ``stats_CEB_single_table.sql``.

    Each line is ``<sql>||<subplan_id>||<ground_truth>``. The trailing integer is
    the true cardinality; a unique ``qid`` is built from a stable prefix.
    """
    path = queries_dir / "stats_CEB_single_table.sql"
    if not path.exists():
        raise FileNotFoundError(
            f"stats_CEB single-table query file not found: {path}"
        )

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if "||" not in line:
                raise ValueError(
                    f"stats_CEB single-table line {line_no}: missing '||': {line!r}"
                )
            parts = line.split("||")
            sql = parts[0].strip()
            truth = int(parts[-1].strip())     # ground truth = last field
            qid = f"st.{line_no}"
            queries.append(
                BenchQuery(
                    bench="stats_ceb_single",
                    qid=qid,
                    sql=sql,
                    ground_truth=truth,
                )
            )
    return queries
