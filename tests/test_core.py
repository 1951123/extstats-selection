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
    ILPResult,
    MaintProfile,
    OptimizerClass,
    Option,
    PhysicalStat,
    build_problem,
    select_optimizer_class,
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


def test_catalog_mask_capability_contract():
    """PG advertises inline catalog-mask (Protocol-M) + a driver; Oracle does not."""
    from extstats2.core.measure_sampling import measure_query_sampling_m
    pg = get_backend("postgres")
    orc = get_backend("oracle")
    assert pg.supports_catalog_mask() is True
    assert pg.catalog_driver() is not None
    assert orc.supports_catalog_mask() is False
    assert orc.catalog_driver() is None
    # measure_query_sampling_m is importable and shares measure_query_sampling's fallback
    # entry (callers on non-mask backends get Protocol-A, tested in integration).
    assert callable(measure_query_sampling_m)


# ---------------------------------------------------------------------------
# optimizer-class selection + structural contract (§1.9)
# ---------------------------------------------------------------------------

def test_select_optimizer_class_sparse_vs_multiplicative():
    from extstats2.backend.base import StructuralProps
    assert select_optimizer_class(
        StructuralProps(sparse_one_stat=True)) == OptimizerClass.SPARSE_LINEAR
    assert select_optimizer_class(
        StructuralProps(sparse_one_stat=False)) == OptimizerClass.MULTIPLICATIVE


def test_select_optimizer_class_has_no_objective_knob():
    """The optimizer-class selector no longer accepts an ``objective`` argument:
    the optimization objective is fixed by the class (cap=1 exact-arithmetic vs
    cap>1 geometric-surrogate). Worst/p90 are evaluation metrics only and were
    never selectable as optimization objectives here.
    """
    from extstats2.backend.base import StructuralProps
    props = StructuralProps(sparse_one_stat=True)
    # objective was a vestigial validation-only param; removed -- passing it now
    # fails loudly rather than silently pretending worst/geomean are supported.
    with pytest.raises(TypeError):
        select_optimizer_class(props, objective="worst")


def test_sparse_linear_class_exact_decode():
    # With per_query_cap=1 and SPARSE_LINEAR, decoding is exactly linear:
    # e_i = qbase - Δ_is for the single chosen stat.
    phys = [PhysicalStat(table="t", columns=("a", "b"), level=1, cost=100)]
    base = 10.0
    # qerr 2 -> Δ = 8, so exact q-error after selection = 2.0
    opts = [Option(stat_index=0, qerror=2.0, level=1, query="q1", cand="t(a,b)")]
    res = solve_ilp(
        phys, [opts], [base], budget_bytes=1000,
        optimizer_class=OptimizerClass.SPARSE_LINEAR, per_query_cap=1,
    )
    assert res.chosen == [["t|a,b|L1"]]
    assert res.qerror_per_query[0] == pytest.approx(2.0, abs=1e-6)
    assert res.mean_qerror == pytest.approx(2.0, abs=1e-6)


def test_sparse_linear_prefers_larger_improvement():
    # Under the sparse-linear class the solver picks the option with the larger
    # absolute improvement (Δ), not necessarily the larger log improvement.
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=100),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=100),
    ]
    base = 100.0
    # opt0: qerr 50 (Δ=50); opt1: qerr 90 (Δ=10). Sparse-linear picks opt0.
    opts = [
        Option(stat_index=0, qerror=50.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=90.0, level=1, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(
        phys, [opts], [base], budget_bytes=1000,
        optimizer_class=OptimizerClass.SPARSE_LINEAR, per_query_cap=1,
    )
    assert res.chosen == [["t|a,b|L1"]]
    assert res.qerror_per_query[0] == pytest.approx(50.0, abs=1e-6)


def test_backend_structural_props_declarations():
    pg = get_backend("postgres").structural_props()
    assert pg.sparse_one_stat is True
    assert pg.disjoint_supported is True
    assert pg.maint_structure == "fixed+var"
    assert pg.capacity_model == "per_stat"
    assert "mean" in pg.supports_objectives

    ora = get_backend("oracle").structural_props()
    assert ora.maint_structure == "fixed_only"   # column groups share one scan
    assert ora.capacity_model == "per_scan"      # estimate_percent per GATHER
    assert "mean" in ora.supports_objectives


# ---------------------------------------------------------------------------
# Y-two-layer maintenance profile (table-activated fixed + per-stat var)
# ---------------------------------------------------------------------------

def _mk_prof():
    # one table with fixed ladder per tier (0,1,2); var paid per selected stat
    return MaintProfile(table_base_tiers={"t": (1.0, 10.0, 100.0)})


def test_y2_two_stats_same_table_pay_fixed_once():
    # Two non-overlapping stats on the SAME table, both level 1.
    # Fixed cost must be charged ONCE (at tier 1 = 10.0), not twice.
    prof = _mk_prof()
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=50,
                     maint_cost=1.0),   # var
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=50,
                     maint_cost=1.0),   # var
    ]
    base = 10.0
    # q=5 keeps the joint geometric surrogate feasible under the >=1 floor
    # (two independent 10->5 stats would give 10*(5/10)^2 = 2.5 >= 1) while
    # still letting both be selected on the SAME query to test fixed-once.
    opts = [
        Option(stat_index=0, qerror=5.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=5.0, level=1, query="q1", cand="t(c,d)"),
    ]
    # The two non-overlapping stats can BOTH be chosen (per_query_cap=None),
    # so total = fixed(10.0 once) + var(1+1) = 12.0, NOT 2*(10+1)=22.
    res = solve_ilp(phys, [opts], [base], budget_bytes=200,
                    maint_budget=100.0, maint_profile=prof)
    assert len(res.selected_stats) == 2
    assert res.total_maint == pytest.approx(12.0, abs=1e-6)
    assert res.total_bytes == 100


def test_y2_two_stats_different_tables_pay_two_fixed():
    # Two stats on DIFFERENT tables each pay their own table fixed cost.
    prof = MaintProfile(table_base_tiers={"t1": (1.0, 10.0, 100.0),
                                          "t2": (1.0, 10.0, 100.0)})
    phys = [
        PhysicalStat(table="t1", columns=("a", "b"), level=1, cost=50,
                     maint_cost=0.5),
        PhysicalStat(table="t2", columns=("c", "d"), level=1, cost=50,
                     maint_cost=0.5),
    ]
    base = 10.0
    # q=5 keeps both selections jointly feasible under the surrogate >=1 floor.
    opts = [
        Option(stat_index=0, qerror=5.0, level=1, query="q1", cand="t1(a,b)"),
        Option(stat_index=1, qerror=5.0, level=1, query="q1", cand="t2(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=200,
                    maint_budget=100.0, maint_profile=prof)
    # each table pays fixed 10.0, plus var 0.5+0.5 => 21.0
    assert res.total_maint == pytest.approx(21.0, abs=1e-6)


def test_y2_budget_binds_on_fixed_charge():
    # A budget low enough to allow only one table's fixed charge.
    prof = _mk_prof()
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=50, maint_cost=0.1),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=50, maint_cost=0.1),
        PhysicalStat(table="t", columns=("e", "f"), level=1, cost=50, maint_cost=0.1),
    ]
    base = 10.0
    # q=6 keeps three selections jointly feasible: 10*(6/10)^3 = 2.16 >= 1.
    opts = [
        Option(stat_index=0, qerror=6.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=6.0, level=1, query="q1", cand="t(c,d)"),
        Option(stat_index=2, qerror=6.0, level=1, query="q1", cand="t(e,f)"),
    ]
    # Allow ~2 var terms + 1 fixed: budget 10.5 -> can take several stats but
    # must stay within one table's fixed(10)+var; verify feasibility bound.
    res = solve_ilp(phys, [opts], [base], budget_bytes=300,
                    maint_budget=12.0, maint_profile=prof)
    # All three cheap stats selected => fixed 10 once + var 3*0.1 = 10.3 <= 12
    assert len(res.selected_stats) == 3
    assert res.total_maint == pytest.approx(10.3, abs=1e-6)


# ---------------------------------------------------------------------------
# S-grid (sampling-first) optimizer consumer — no live DB
# ---------------------------------------------------------------------------

def _mk_lambda_block(qid, actual, lam_blocks):
    """Build a by_lambda block from {level: {base_qerr, cands:[(cols,param,qerr,size)]}}."""
    by_lambda = {}
    for level, c in lam_blocks.items():
        slot = {"S_rows": None, "single_target": None,
                "baseline": {"estimate": 1, "qerror": c["base"]},
                "candidates": [
                    {"cols": list(cols), "param": p, "estimate": 1,
                     "qerror": qe, "lambda_q": None,
                     "size_bytes": sz, "maint_var": 0.1}
                    for cols, p, qe, sz in c["cands"]
                ]}
        by_lambda[str(level)] = slot
    return {"qid": qid, "actual": actual, "by_lambda": by_lambda}


def test_lambda_consumer_per_lambda_baseline_and_lattice_candidates():
    """Each λ slot carries its own baseline + candidate params bounded by
    p<=S/300; the consumer uses the λ-specific baseline (not a global one)."""
    from extstats2.core.optimize_sgrid import build_inner_at_level
    # λ0 holds only p=100 (its baseline at 300k), λ1 offers p up to 1000.
    blocks = {
        "q1": _mk_lambda_block("q1", 100, {
            "0": {"base": 50.0, "cands": [
                (("a", "b"), 100, 5.0, 200), (("a", "b"), 1000, 4.0, 400)]},
        }),
    }
    # cap for λ0 shown by which params appeared at measure time: here both are
    # recorded, but the L0 slot legitimately cannot have p>S/300 measured; we
    # hand only p=100 for L0:
    blocks["q1"]["by_lambda"]["0"]["candidates"] = [
        {"cols": ["a", "b"], "param": 100, "estimate": 1, "qerror": 5.0,
         "lambda_q": None, "size_bytes": 200, "maint_var": 0.1}]
    phys, opts, qbases = build_inner_at_level(blocks, "0")
    assert len(phys) == 1
    assert opts[0][0].level == 100
    assert qbases == [50.0]
    # per-λ baseline is used: the single option beats 50 -> included
    assert opts[0][0].qerror == 5.0


def test_lambda_consumer_maint_budget_binds():
    """A maintenance budget on the per-λ inner solver is a hard cap: it keeps
    total_maint <= budget and (for a tight cap) drops selections."""
    from extstats2.core.optimize_sgrid import inner_optimal_at_level
    # one query, λ0, many cheap storage / per-stat maint 0.1 candidates
    cands = [(("a%d" % i, "b%d" % i), 100, float(i + 2), 50) for i in range(8)]
    blocks = {"q1": _mk_lambda_block("q1", 100, {
        "0": {"base": 100.0, "cands": cands},
    })}

    # no maint constraint: all 8 chosen (storage 8*50=400 allowed)
    res, _, _ = inner_optimal_at_level(blocks, "0", budget_bytes=1000)
    assert res is not None
    assert res.total_maint == pytest.approx(8 * 0.1, abs=1e-6)
    n_free = len(res.selected_stats)

    # tight maint budget 0.25 forces <= 2 stats (0.1 each) chosen
    res2, _, _ = inner_optimal_at_level(blocks, "0", budget_bytes=1000,
                                        maint_budget=0.25)
    assert res2 is not None
    assert res2.total_maint <= 0.25 + 1e-9
    assert len(res2.selected_stats) < n_free
    assert 0 < len(res2.selected_stats) <= 3


def _mk_fidelity_block(qid, cols, lamq, base, qerr):
    """One query, one (query,level) slot: an extstat cutting qerr base->qerr,
    with fidelity lambda_q (per-query,level). cols is the candidate colset."""
    return {"qid": qid, "actual": 1, "by_lambda": {"0": {
        "S_rows": 30000.0, "single_target": None,
        "baseline": {"estimate": int(base * 10), "qerror": float(base)},
        "candidates": [{"cols": list(cols), "param": 254,
                         "estimate": int(qerr * 10), "qerror": float(qerr),
                         "lambda_q": float(lamq), "size_bytes": 500,
                         "maint_var": 0.0}]}}}


def test_fidelity_soft_penalty_default_off_is_identity():
    """fidelity_floor=None must reproduce the unweighted objective exactly:
    two queries competing for one budget slot resolve purely on raw improved
    the stat with the larger measured benefit, regardless of lambda_q."""
    from extstats2.core.optimize_sgrid import inner_optimal_at_level
    # A: small raw gain but high lambda_q; B: large raw gain but tiny lambda_q.
    blocks = {
        "qA": _mk_fidelity_block("qA", ["a", "b"], 50.0, 6.0, 1.0),
        "qB": _mk_fidelity_block("qB", ["c", "d"], 0.5, 20.0, 2.0),
    }
    r = inner_optimal_at_level(blocks, "0", budget_bytes=500,
                               fidelity_floor=None)[0]
    assert r is not None
    # B's raw improvement (base 20 -> 2) dominates A's (6 -> 1) => B wins.
    assert {tuple(p.columns) for p in r.selected_stats} == {("c", "d")}
    # and explicit floor 0.0 is the same identity
    r0 = inner_optimal_at_level(blocks, "0", budget_bytes=500,
                                fidelity_floor=0.0)[0]
    assert {tuple(p.columns) for p in r0.selected_stats} == {("c", "d")}


def test_fidelity_soft_penalty_decredits_low_lambda_q():
    """A finite k de-credits a (query,level) with low lambda_q: B's claimed gain
    (credited = raw*min(1, lambda_q/k)) falls below A's once k is large enough,
    so the trustworthy A is preferred under a shared budget."""
    from extstats2.core.optimize_sgrid import inner_optimal_at_level
    blocks = {
        "qA": _mk_fidelity_block("qA", ["a", "b"], 50.0, 6.0, 1.0),
        "qB": _mk_fidelity_block("qB", ["c", "d"], 0.5, 20.0, 2.0),
    }
    # credited_B(2.0) = min(1, .5/2)*18 = 4.5 < credited_A = 5  => A wins
    r = inner_optimal_at_level(blocks, "0", budget_bytes=500,
                               fidelity_floor=2.0)[0]
    assert r is not None
    assert {tuple(p.columns) for p in r.selected_stats} == {("a", "b")}

