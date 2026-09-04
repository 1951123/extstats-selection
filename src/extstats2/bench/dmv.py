"""DMV single-table query loader for the BayesCard / AreCELearnedYet DMV bench.

The BayesCard ``Benchmark/DMV/query.sql`` file is a custom DSL (not raw SQL):
  -  ``SELECT COUNT(*) FROM DMV WHERE Col IN [A, B, C] AND Col2 == N ... ||<truth>``
  -  values are UNQUOTED (``NY``, ``2DSD``, ``H/TR``); lists use ``[...]``;
     some lists have a trailing comma; equality uses ``==``.
This module:

  1. :func:`dsl_to_sql` — translate one DSL line (or its WHERE) into valid
     PostgreSQL, quoting every scalar literal (the DMV table folds unquoted
     lower-case; the CSV columns were loaded lower-case in :mod:`dmv`).
  2. :func:`load_dmv` — read the canonical translated file
     ``benchmarks/DMV/queries/dmv.sql`` (one line ``<sql>||<truth>``, mirroring
     the stats_CEB format with the truth re-computed on OUR DMV snapshot) and
     yield ``BenchQuery``. Ground truth is authoritative because the BayesCard
     ``||`` values were computed on a different author snapshot.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.queries import BenchQuery

# --- DSL -> SQL -----------------------------------------------------------

_EQ_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?=[A-Za-z0-9_]+)", )
# careful matching handled below


def _quote(token: str) -> str:
    tok = token.strip()
    # strip any stray surrounding quote the source may carry
    tok = tok.strip("\"'")
    return "'" + tok.replace("'", "''") + "'"


def translate_where(where_body: str) -> str:
    """Translate a DMV-DSL WHERE body into PG SQL conditions (values quoted).

    Handles ``Col IN [a, b, ]`` (optional trailing comma) and ``Col == v``.
    """
    out = []  # conditions appended with AND

    i = 0
    n = len(where_body)
    while i < n:
        # skip spaces / standalone AND/JOIN etc (we rebuild ANDs between terms)
        if where_body[i].isspace():
            i += 1
            continue
        # 'AND' separator -> move past
        if where_body.startswith(("AND", "and"), i):
            i += 3
            continue

        # parse an IN-list:  Col IN [ ... ]
        m_in = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*IN\s*\[\s*([^\]\[]*)\]\s*", where_body[i:], re.I)
        # parse an equality:  Col == v  (v = run of non-space, non-AND, up to next AND)
        m_eq = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*==\s*([A-Za-z0-9_./\-]+)", where_body[i:])
        if m_eq:
            col, val = m_eq.group(1), m_eq.group(2)
            out.append(f"{col} = {_quote(val)}")
            i += m_eq.end()
            continue
        if m_in:
            col = m_in.group(1)
            raw = m_in.group(2)
            vals = [v for v in (_quote(x) for x in raw.split(",")) if v != "''"]
            out.append(f"{col} IN ({', '.join(vals)})")
            i += m_in.end()
            continue
        # unknown -> skip one char defensively
        i += 1

    return " AND ".join(out)


def dsl_line_to_sql(line: str):
    """Split a DSL line into ``(count_sql, raw_truth_or_None)``.

    ``count_sql`` replaces the WHERE clause with translated PG SQL; the leading
    ``SELECT COUNT(*) FROM DMV`` is preserved verbatim.
    """
    line = line.strip().rstrip(";")
    # separate trailing truth
    truth = None
    if "||" in line:
        sql_part, _, truth_s = line.rpartition("||")
        truth = int(truth_s.strip())
    else:
        sql_part = line
    # split at first WHERE
    m = re.search(r"\bWHERE\b(.*)$", sql_part, re.I)
    if not m:
        return f"{sql_part}", truth
    head = sql_part[: m.start()]
    body = m.group(1)
    where = translate_where(body)
    return f"{head} WHERE {where}", truth


# --- canonical file loader -----------------------------------------------

CANON = "dmv.sql"   # file name for the translated re-truth'd queries


def load_dmv(queries_dir: Path) -> list[BenchQuery]:
    """Read ``<queries_dir>/dmv.sql`` (``<sql>||<truth>`` per line) -> BenchQuery.

    Falls back to translating ``query.sql`` in place if ``dmv.sql`` is absent
    (useful for structure checks only; truth then comes from BayesCard and is
    NOT authoritative).
    """
    canon = queries_dir / CANON
    if canon.exists():

        def rows():
            with canon.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    sql, _, truth = line.rpartition("||")
                    yield (sql.strip(), int(truth.strip()))

        qs = []
        for idx, (sql, truth) in enumerate(rows(), start=1):
            qs.append(BenchQuery(bench="dmv", qid=f"dmv.{idx}",
                                 sql=sql, ground_truth=truth))
        return qs

    # fallback: translate BayesCard file on the fly
    src = queries_dir / "query.sql"
    if not src.exists():
        raise FileNotFoundError(f"DMV query file not found under {queries_dir}")
    qs = []
    for idx, line in enumerate(src.open("r", encoding="utf-8"), start=1):
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        sql, truth = dsl_line_to_sql(line)
        qs.append(BenchQuery(bench="dmv", qid=f"dmv.{idx}", sql=sql,
                             ground_truth=truth))
    return qs
