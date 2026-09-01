"""Smoke tests for the scaffolded v2 core + abstractions.

These exercise the parts that are already implemented and backend-agnostic:
the ported MILP (`core/optimize.py`), candidate generation, predicate extraction,
and the backend abstraction contracts.  They require no live database.
"""

from __future__ import annotations

import numpy as np
import pytest

from extstats2.config import get_backend
from extstats2.core.optimize import (
    Option,
    PhysicalStat,
    build_problem,
    solve_ilp,
)
from extstats2.core.queries import BenchQuery
from extstats2.core.candidates import generate_candidates
from extstats2.core.predicates import predicate_columns


# ---------------------------------------------------------------------------
# core/optimize (ported MILP) — database-free
# ---------------------------------------------------------------------------

def test_milp_single_query_picks_best_nonoverlapping():
    # Two physical stats: A on cols (a,b) cost 100 qerr2; B on (b,c) cost 150 qerr 8.
    # They overlap on b, so under the overlap-free constraint the query picks at
    # most one. stat0 gives the better improvement, so the query *selects* stat0.
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=100),
        PhysicalStat(table="t", columns=("b", "c"), level=1, cost=150),
    ]
    base = 10.0
    opts = [
        Option(stat_index=0, qerror=2.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=8.0, level=1, query="q1", cand="t(b,c)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000)
    # Query selects stat0 (chosen), achieving q-error 2.0.
    assert res.chosen == [["t|a,b|L1"]]
    assert res.mean_qerror == pytest.approx(2.0, abs=1e-6)
    assert res.mean_qerror < base  # improved over baseline
    # The model may *create* un-selected stats within budget (v1 behaviour:
    # y has no cost term), so we assert on chosen/mean, not on selected_stats.
    assert res.total_bytes <= 1000


def test_milp_budget_capped():
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=300),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=300),
    ]
    base = 10.0
    # both improve but cost 300+300=600 > budget 400, so budget binds to one
    opts = [
        Option(stat_index=0, qerror=2.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=2.0, level=1, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=400)
    assert len(res.selected_stats) == 1
    assert res.total_bytes <= 400


def test_milp_maint_budget_binds():
    # Two disjoint stats, both big improvements, both fit the storage budget,
    # but only one fits the *maintenance* budget when maint_costs differ.
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=2, cost=200, maint_cost=100.0),
        PhysicalStat(table="t", columns=("c", "d"), level=2, cost=200, maint_cost=100.0),
    ]
    base = 10.0
    # non-overlapping -> both selectable; storage 400 <= 1000 so only maint binds
    opts = [
        Option(stat_index=0, qerror=2.0, level=2, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=2.0, level=2, query="q1", cand="t(c,d)"),
    ]
    # maint budget 120 < sum(100+100)=200 -> at most one selected
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000, maint_budget=120.0)
    assert len(res.selected_stats) == 1
    assert res.total_maint == pytest.approx(100.0, abs=1e-6)
    assert res.total_maint <= 120.0
    # and q-error reflects that single selection (2.0)
    assert res.mean_qerror == pytest.approx(2.0, abs=1e-6)


def test_milp_maint_budget_noop_when_none():
    # Without maint_budget, maint_cost is ignored -> both selected (behavior
    # identical to pre-maintenance ILP).
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=2, cost=200, maint_cost=100.0),
        PhysicalStat(table="t", columns=("c", "d"), level=2, cost=200, maint_cost=100.0),
    ]
    base = 10.0
    opts = [
        Option(stat_index=0, qerror=2.0, level=2, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=2.0, level=2, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000)  # no maint_budget
    assert len(res.selected_stats) == 2
    assert res.total_maint == pytest.approx(200.0, abs=1e-6)


def test_build_problem_reads_maint_cost():
    phase1 = {
        "results": [
            {
                "qid": "q1",
                "qerror_base": 10.0,
                "candidates": {
                    "t(a,b)": {
                        "table": "t",
                        "columns": ["a", "b"],
                        "levels": {
                            "1": {"qerror": 2.0, "size_bytes": 100,
                                  "maint_cost": 12.5},
                            "2": {"qerror": 1.5, "size_bytes": 200,
                                  "maint_cost": 25.0},
                        },
                    },
                },
            }
        ]
    }
    phys, _opts, _bases = build_problem(phase1)
    by_level = {p.level: p for p in phys}
    assert by_level[1].maint_cost == pytest.approx(12.5)
    assert by_level[2].maint_cost == pytest.approx(25.0)
    # legacy dicts without maint_cost default to 0.0
    legacy = {"results": [{
        "qid": "q1", "qerror_base": 10.0,
        "candidates": {"t(a,b)": {"table": "t", "columns": ["a", "b"],
                                  "levels": {"1": {"qerror": 2.0, "size_bytes": 100}}}},
    }]}
    p, _o, _b = build_problem(legacy)
    assert p[0].maint_cost == 0.0


def test_milp_shared_resource_paid_once():
    # Same physical stat usable by two queries; only one copy paid.
    phys = [PhysicalStat(table="t", columns=("a", "b"), level=1, cost=200)]
    base_a, base_b = 10.0, 8.0
    opts_a = [Option(stat_index=0, qerror=2.0, level=1, query="qa", cand="t(a,b)")]
    opts_b = [Option(stat_index=0, qerror=1.5, level=1, query="qb", cand="t(a,b)")]
    res = solve_ilp(phys, [opts_a, opts_b], [base_a, base_b], budget_bytes=200)
    # One shared stat created (y=1), paid once, selected by both queries.
    assert len(res.selected_stats) == 1
    assert res.total_bytes == 200
    assert len(res.chosen[0]) == 1
    assert len(res.chosen[1]) == 1


def test_build_problem_shape():
    phase1 = {
        "results": [
            {
                "qid": "q1",
                "qerror_base": 10.0,
                "candidates": {
                    "t(a,b)": {
                        "table": "t",
                        "columns": ["a", "b"],
                        "levels": {
                            "1": {"qerror": 2.0, "size_bytes": 100},
                            "2": {"qerror": 1.5, "size_bytes": 200},
                        },
                    },
                },
            }
        ]
    }
    phys, opts, bases = build_problem(phase1)
    assert len(phys) == 2  # two (combo, level) physical stats
    assert len(opts) == 1
    assert bases == [10.0]


# ---------------------------------------------------------------------------
# core/candidates + predicates — database-free
# ---------------------------------------------------------------------------

def test_predicate_columns_single_table():
    q = BenchQuery(
        bench="census",
        qid="q1",
        sql="SELECT COUNT(*) FROM climate WHERE dIncome1 = 10 AND dAgep = 5",
        ground_truth=100,
    )
    cols = predicate_columns(q)
    # single table -> unqualified columns attributed to it (case-insensitive
    # match on the column names).
    values = {"".join(v).lower() for v in cols.values()}
    assert any("dincome1" in v and "dagep" in v for v in values)


def test_candidates_generation():
    q = BenchQuery(
        bench="census",
        qid="q1",
        sql=("SELECT COUNT(*) FROM climate "
             "WHERE dIncome1 = 10 AND dAgep = 5 AND dEducation = 3"),
        ground_truth=100,
    )
    cands = generate_candidates([q], arities=(2, 3))
    # A 3-column set yields C(3,2)=3 pairs + C(3,3)=1 triple, all deduped.
    assert len(cands) == 4


# ---------------------------------------------------------------------------
# backend abstraction + config factory
# ---------------------------------------------------------------------------

def test_backend_factory_contracts():
    be = get_backend("postgres")
    assert be.name() == "postgres"
    caps = {c.name: c for c in be.supported_capabilities()}
    assert "mcv" in caps
    assert be.has_protocol_m() is True
    assert be.protocol(None) == "m"
    assert be.protocol("m") == "m"
    assert be.protocol("a") == "a"


def test_oracle_protocol_falls_back_to_a():
    be = get_backend("oracle")
    assert be.has_protocol_m() is False
    assert be.protocol(None) == "a"
    assert be.protocol("m") == "a"  # requested m falls back to a


def test_capacity_contract():
    from extstats2.config import capacity_ladder
    assert set(capacity_ladder("postgres")) == {0, 1, 2}
    assert capacity_ladder("postgres")[1]["statistics_target"] == 1000
    assert capacity_ladder("oracle")[2]["estimate_percent"] == 100
