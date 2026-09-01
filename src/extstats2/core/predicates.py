"""Predicate-column extraction (port of v1 ``predicates.py``, dialect-neutral).

Extracts, per base table, the columns referenced by *selection* predicates in a
query's WHERE clause using sqlglot. Extended statistics apply to base-table
column combinations for selection predicates only — not join predicates — which
is a property of the statistics model shared by PostgreSQL (and approximated by
Oracle column-group statistics), so the extraction is backend-agnostic.

The only change from v1: the sqlglot dialect is no longer hard-coded to
``"postgres"``.  Callers may pass an optional ``dialect`` (the core default is
None -> sqlglot's generic dialect), and a backend may inform the core of its
dialect so predicates parse correctly for that engine's SQL flavour.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp

from .queries import BenchQuery

# Selection operators (a single column compared against a constant / literal).
_SELECTION_OPS = {
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.In,
    exp.Like, exp.ILike, exp.Between, exp.Is, exp.Contains,
}


@dataclass
class TableRef:
    """A resolved relation in the FROM clause."""

    schema: Optional[str]
    table: str
    alias: Optional[str]
    key: str = field(init=False)

    def __post_init__(self) -> None:
        self.key = f"{self.schema or ''}.{self.table}"


def _resolve_column(column: exp.Column, aliases: dict[str, TableRef]) -> Optional[TableRef]:
    # ``column.table`` may be None (generic dialect) OR the empty string
    # (when a column has no table prefix) — both mean "not qualified".
    if not column.table:
        return None
    ref = aliases.get(column.table) or aliases.get(f"({column.table})")
    return ref


def table_aliases(query: BenchQuery, dialect: Optional[str] = None) -> dict[str, TableRef]:
    """Parse the FROM clause and return alias -> TableRef mapping."""
    ast = sqlglot.parse_one(query.sql, read=dialect) if dialect else sqlglot.parse_one(query.sql)
    aliases: dict[str, TableRef] = {}
    for table in ast.find_all(exp.Table):
        alias = table.alias or table.name
        ref = TableRef(schema=table.db or table.catalog, table=table.name, alias=alias)
        aliases[alias] = ref
        aliases.setdefault(table.name, ref)
    return aliases


def predicate_columns(
    query: BenchQuery,
    dialect: Optional[str] = None,
) -> dict[str, set[str]]:
    """Return ``{qualified_table: set(column)}`` of base-table columns constrained
    by selection predicates in the query's WHERE clause."""
    ast = sqlglot.parse_one(query.sql, read=dialect) if dialect else sqlglot.parse_one(query.sql)
    aliases = table_aliases(query, dialect)
    where = ast.find(exp.Where)
    result: dict[str, set[str]] = defaultdict(set)
    if where is None:
        return dict(result)

    unique_tables = {ref.key for ref in aliases.values()}
    single_table = unique_tables.pop() if len(unique_tables) == 1 else None

    for node in where.walk():
        if not isinstance(node, tuple(_SELECTION_OPS)):
            continue
        cols = [c for c in node.find_all(exp.Column)]
        keys = {
            (_resolve_column(c, aliases).key if _resolve_column(c, aliases) else None)
            for c in cols
        }
        keys.discard(None)

        if len(keys) == 1:
            target = keys.pop()
        elif len(keys) == 0 and single_table is not None:
            target = single_table
        else:
            continue

        for c in cols:
            if not c.table and single_table is not None:
                result[target].add(c.name)
            elif c.table is not None and _resolve_column(c, aliases) is not None:
                result[target].add(c.name)

    return dict(result)
