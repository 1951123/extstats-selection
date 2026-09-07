
# ---------------------------------------------------------------------------
# Milestone: MILP surrogate floor (>=1), Option-A overlap for cap>1, and
# maintenance-tier monotonicity — regression tests for the current research
# design (cap=1 => exact arithmetic mean; cap>1 => geometric-mean surrogate).
# ---------------------------------------------------------------------------

import pytest

from extstats2.core.optimize import (
    MaintProfile,
    OptimizerClass,
    Option,
    PhysicalStat,
    solve_ilp,
)

def _phys_overlap_triple():
    # Two overlapping stats on the same table (share col b): A:(a,b), B:(b,c).
    return [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=100),
        PhysicalStat(table="t", columns=("b", "c"), level=1, cost=100),
    ]


def test_multiplicative_surrogate_never_below_one_for_strong_multi():
    """Fix #1: with cap>1 (per_query_cap=None, MULTIPLICATIVE), combining several
    independently-strong stats can drive the raw geometric product below 1. The
    per-query surrogate floor must stop the solver selecting such an impossible
    set, so every decoded q-error stays >= 1 and equals the solver objective.

    base 45, two non-overlapping stats each qerr 3: raw product = 45*(3/45)^2 =
    0.2 < 1 -> the solver must NOT take both; every surrogate must be >= 1.
    """
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=10),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=10),
    ]
    base = 45.0
    opts = [
        Option(stat_index=0, qerror=3.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=3.0, level=1, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000)  # MULTIPLICATIVE
    # both non-overlapping and cheap, but jointly their surrogate would be 0.2
    # (<1): the floor forbids selecting both. At most one stat for the query.
    assert len(res.chosen[0]) <= 1
    # every decoded surrogate is >= 1 (never an impossible q-error)
    for q in res.qerror_per_query:
        assert q >= 1.0 - 1e-9
    assert res.mean_qerror >= 1.0 - 1e-9


def test_multiplicative_single_best_reachable_floor():
    """Fix #1 sanity: when a single stat legitimately reaches a high q-error the
    solver still selects it (floor does not over-restrict); decode == objective.

    base 45, one stat qerr 3 -> surrogate = 3 (>=1), selectable; decode 3.
    """
    phys = [PhysicalStat(table="t", columns=("a", "b"), level=1, cost=1)]
    base = 45.0
    opts = [Option(stat_index=0, qerror=3.0, level=1, query="q1", cand="t(a,b)")]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000)
    assert res.chosen == [["t|a,b|L1"]]
    assert res.qerror_per_query[0] == pytest.approx(3.0, abs=1e-6)


def test_multiplicative_single_stat_at_qerror_one_is_selectable():
    """The floor must not forbid a stat that *exactly* reaches the q-error floor
    (surrogate == 1). base 45, qerr 1 -> surrogate = 45*(1/45) = 1.0, feasible."""
    phys = [PhysicalStat(table="t", columns=("a", "b"), level=1, cost=1)]
    base = 45.0
    opts = [Option(stat_index=0, qerror=1.0, level=1, query="q1", cand="t(a,b)")]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000)
    assert res.chosen == [["t|a,b|L1"]]
    assert res.qerror_per_query[0] >= 1.0 - 1e-9


def test_overlap_forbidden_with_explicit_cap_greater_than_one():
    """Fix #2 (Option A chosen): the independence (multiplicative) model forbids a
    query selecting column-overlapping stats even when an explicit cap>1 (here
    K=2) would otherwise allow two selections. Only one overlapping stat wins.

    Intended semantics from architecture.md §2/§3: multiplicative surrogate is an
    independence model whose premise is that the combined stats do not share
    columns -> option A.
    """
    phys = _phys_overlap_triple()          # t(a,b), t(b,c) overlap on b
    base = 10.0
    opts = [
        Option(stat_index=0, qerror=2.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=3.0, level=1, query="q1", cand="t(b,c)"),
    ]
    # K=2 explicitly permitted, but Q1 must still not take both overlapping stats.
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000,
                    optimizer_class=OptimizerClass.MULTIPLICATIVE,
                    per_query_cap=2)
    assert len(res.chosen[0]) == 1


def test_double_selection_allowed_for_nonoverlap_under_cap2():
    """Contrast with Option A: two NON-overlapping stats may BOTH be selected when
    an explicit cap K=2 is given (they do not violate independence)."""
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=10),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=10),
    ]
    base = 50.0
    # q=40 keeps both in surrogate: 50*(40/50)^2 = 32 >=1
    opts = [
        Option(stat_index=0, qerror=40.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=40.0, level=1, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000,
                    optimizer_class=OptimizerClass.MULTIPLICATIVE,
                    per_query_cap=2)
    assert len(res.chosen[0]) == 2


def test_maintenance_non_monotone_ladder_errors():
    """Fix #4: a non-monotone fixed ladder (higher level cheaper than lower)
    would yield a negative incremental maintenance charge; refuse to build."""
    from extstats2.core.optimize import solve_ilp as _solve
    prof = MaintProfile(table_base_tiers={"t": (10.0, 5.0, 100.0)})  # L1 < L0
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=50,
                     maint_cost=1.0),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=50,
                     maint_cost=1.0),
    ]
    base = 10.0
    opts = [
        Option(stat_index=0, qerror=5.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=5.0, level=1, query="q1", cand="t(c,d)"),
    ]
    with pytest.raises(ValueError):
        _solve(phys, [opts], [base], budget_bytes=200,
               maint_budget=100.0, maint_profile=prof)


def test_maintenance_monotone_ladder_ok():
    """Fix #4 companion: a monotone ladder is accepted and the table's max reached
    level drives the single fixed charge."""
    prof = MaintProfile(table_base_tiers={"t": (1.0, 10.0, 100.0)})  # monotone
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=2, cost=50,
                     maint_cost=1.0),
    ]
    base = 10.0
    opts = [Option(stat_index=0, qerror=5.0, level=2, query="q1", cand="t(a,b)")]
    res = solve_ilp(phys, [opts], [base], budget_bytes=200,
                    maint_budget=200.0, maint_profile=prof)
    # fixed at tier 2 = 100.0 charged once, plus 1.0 var = 101.0
    assert res.total_maint == pytest.approx(101.0, abs=1e-6)


def test_sparse_linear_requires_per_query_cap_one():
    """P0-1 lock: OptimizerClass.SPARSE_LINEAR (exact arithmetic mean) is only
    mathematically valid with per_query_cap == 1 (one stat per query). Passing
    None (or any cap != 1) must raise, never silently build an invalid unbounded
    sparse formulation."""
    phys = [PhysicalStat(table="t", columns=("a", "b"), level=1, cost=100)]
    base = 45.0
    opts = [Option(stat_index=0, qerror=3.0, level=1, query="q1", cand="t(a,b)")]
    for bad_cap in (None, 2):
        with pytest.raises(ValueError):
            solve_ilp(phys, [opts], [base], budget_bytes=1000,
                      optimizer_class=OptimizerClass.SPARSE_LINEAR,
                      per_query_cap=bad_cap)
    # ...and cap=1 is accepted.
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR,
                    per_query_cap=1)
    assert res.chosen == [["t|a,b|L1"]]


def test_maintenance_profile_must_cover_candidate_levels():
    """Candidate-level coverage: if a candidate reaches a higher capacity level
    than the profile's ladder provides, the staircase would silently skip that
    threshold and under-charge maintenance. Must raise ValueError instead.

    Ladder (1.0, 10.0) covers levels {0,1}; a level-2 candidate is unrepresentable.
    """
    prof = MaintProfile(table_base_tiers={"t": (1.0, 10.0)})  # levels 0..1 only
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=2, cost=50,
                     maint_cost=1.0),
    ]
    base = 10.0
    opts = [Option(stat_index=0, qerror=5.0, level=2, query="q1", cand="t(a,b)")]
    with pytest.raises(ValueError):
        solve_ilp(phys, [opts], [base], budget_bytes=200,
                  maint_budget=200.0, maint_profile=prof)


# ---------------------------------------------------------------------------
# Model-level invariant tests (Reviewer round-4): mathematical properties the
# SOLVER must preserve, not bugs it had. These anchor the two-regime semantics.
# ---------------------------------------------------------------------------

def test_invariant_sparse_decode_eq_chosen_stat_error():
    """Test A — sparse (cap=1) exactness: whichever single stat a query selects,
    the decoded q-error must be exactly that stat's q-error (e_hat = e_s), and
    mean == decode (objective == reported metric), not a post-clamped value.

    base=100, e1=10, e2=20 (both overlap on nothing -> both are separate options;
    cap=1 picks the better, decoding arithmetic exactly)."""
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=10),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=10),
    ]
    base = 100.0
    opts = [
        Option(stat_index=0, qerror=10.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=20.0, level=1, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000,
                    optimizer_class=OptimizerClass.SPARSE_LINEAR, per_query_cap=1)
    # solver picks opt0 (e=10, Δ=90 > opt1's Δ=80) and decodes exactly to 10.
    assert res.chosen == [["t|a,b|L1"]]
    assert res.qerror_per_query == pytest.approx([10.0], abs=1e-9)
    assert res.mean_qerror == pytest.approx(10.0, abs=1e-9)


def test_invariant_multiplicative_decode_matches_analytic_product():
    """Test B — multiplicative (cap>1) exact-surrogate decode. With b=100 and two
    non-overlapping stats e1=20, e2=50, selecting both must decode to the analytic
    product 100*(20/100)*(50/100)=10, and that metric equals the solver objective
    (still >= 1, so no floor distortion)."""
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=10),
        PhysicalStat(table="t", columns=("c", "d"), level=1, cost=10),
    ]
    base = 100.0
    opts = [
        Option(stat_index=0, qerror=20.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=50.0, level=1, query="q1", cand="t(c,d)"),
    ]
    # budget lets both fit; floor 100*(.2*.5)=10 >= 1 so both are jointly feasible.
    res = solve_ilp(phys, [opts], [base], budget_bytes=1000,  # MULTIPLICATIVE
                    per_query_cap=2)
    assert len(res.chosen[0]) == 2
    analytic = 100.0 * (20.0 / 100.0) * (50.0 / 100.0)
    assert res.qerror_per_query[0] == pytest.approx(analytic, abs=1e-9)
    assert res.qerror_per_query[0] >= 1.0  # floor not hit for a legit >= 1 product


def test_invariant_maint_highest_level_binds_not_sum():
    """Test C — maintenance Y-two-layer shared-table semantics: two stats on ONE
    table, one at L1 and one at L2, pay the L2 fixed charge ONCE (B_t(2)+sum var),
    never B_t(1)+B_t(2). Ladder (1,10,100): L2 fixed = 100.

    Also covers: raising one stat L1->L2 charges 100, not 10+100."""
    prof = MaintProfile(table_base_tiers={"t": (1.0, 10.0, 100.0)})  # monotone
    phys = [
        PhysicalStat(table="t", columns=("a", "b"), level=1, cost=50,
                     maint_cost=1.0),   # var 1.0
        PhysicalStat(table="t", columns=("c", "d"), level=2, cost=50,
                     maint_cost=1.0),   # var 1.0
    ]
    base = 10.0
    opts = [
        Option(stat_index=0, qerror=5.0, level=1, query="q1", cand="t(a,b)"),
        Option(stat_index=1, qerror=5.0, level=2, query="q1", cand="t(c,d)"),
    ]
    res = solve_ilp(phys, [opts], [base], budget_bytes=200,
                    maint_budget=300.0, maint_profile=prof)
    assert len(res.selected_stats) == 2
    # fixed at highest reached level (L2 = 100) ONCE + var(1+1) => 102
    assert res.total_maint == pytest.approx(102.0, abs=1e-6)
    # not 10 (L1) + 100 (L2) + vars, i.e. never 112-ish.
    assert res.total_maint < 110.0

