"""Phase-2 MILP: budgeted selection of (combo, capacity) options minimising
q-error.  Direct port of v1 ``optimize.py`` — this model is backend-agnostic.

Two objective semantics, selected by the MILP class / per-query cap:
  * per_query_cap=1 + SPARSE_LINEAR  -> EXACT arithmetic mean (linear Δ objective);
  * per_query_cap=K>1 / None + MULTIPLICATIVE -> geometric-mean surrogate
    (log-space additive objective); see the Objective note below the model block.

Model (multi-select, multiplicative approximation for cap>1)
---------------------------------------------------------------
For cap>1 a query may select several *non-overlapping* statistics.  The joint
effect is approximated multiplicatively in log space:

    log e_i(T_i) ≈ log e_i^0 + sum_{s in T_i} log(e_is / e_i^0)

To keep the independence / multiplicative approximation valid we forbid selecting
column-overlapping statistics within a single query (Option A semantics — combine
only independent, non-overlapping stats), and constrain each per-query surrogate
product to stay >= 1.

Variables (all binary):
  - y_s : create physical statistic s (table, columns, capacity)
  - x_is: query i selects statistic s

Objective — two well-separated semantics (never conflated):
  * per_query_cap = 1  (SPARSE_LINEAR, exact):
        min 1/n sum_i e_i   ==  max sum_{i,s} (e_i^0 - e_is) * x_is
    Each query picks at most ONE stat, so e_i = e_i^0 - sum_s Δ_is x_is is
    *exactly* linear (Δ_is = e_i^0 - e_is >= 0); minimising the arithmetic mean
    is exactly maximising total linear improvement.  Exact formulation.
  * per_query_cap = K>1 / None  (MULTIPLICATIVE, geometric surrogate):
        min sum_i log \\hat e_i,   \\hat e_i = e_i^0 * prod_s (e_is/e_i^0)^{x_is}
    The joint effect is a multiplicative composition; minimising its (log-space,
    additive) surrogate is minimising the geometric mean of the surrogate q-error.
    A query may select up to K (or arbitrarily many when None) *non-overlapping*
    stats — Option A semantics: the multiplicative surrogate is an independence
    model whose validity premise is that combined stats do not share columns
    (see architecture.md §2/§3).  Each surrogate \\hat e_i is constrained >= 1
    (a linear row per query), so the solver never optimises an impossible
    below-1 product, keeping the solver objective identical to the final decode.

Constraints:
  1) storage budget  : sum_s c_s * y_s <= C
  2) select created  : x_is <= y_s
  3) overlap-free    : within each query, column-overlapping stats can't both
                       be chosen (multiplicative/independence model, Option A)
  4) surrogate floor : per query, log \\hat e_i >= 0 (i.e. \\hat e_i >= 1)
  5) level exclusivity: at most one capacity level per (table, columns)
  6) (optional) global disjointness: no two created stats share a column

Because y_s is shared across queries (2) but paid once in the budget (1),
multiple queries reusing a statistic pay its storage only once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


# Optimizer classes (see docs/architecture.md §1.9).  The class encodes the
# objective SEMANTICS: SPARSE_LINEAR == exact arithmetic mean (cap=1), while
# MULTIPLICATIVE == geometric-mean surrogate (cap>1/None).  There is no separate
# "objective" runtime switch; worst/p90/geomean are evaluation-only metrics.
class OptimizerClass:
    """Enum-like constants for the two MILP classes."""
    # Sparse-linear MILP: per-query at most one statistic, objective exactly
    # linear (uses Option.linear_improvement). Requires sparse_one_stat.
    SPARSE_LINEAR = "sparse_linear"
    # General multiplicative MILP: log-space additive approximation with
    # overlap-free pruning (uses Option.log_improvement). Fallback when sparsity
    # is not supported.
    MULTIPLICATIVE = "multiplicative"


# ---------------------------------------------------------------------------
# Optimization objective is NOT a runtime switch here: it is fully determined by
#   per_query_cap == 1 + SPARSE_LINEAR  -> exact arithmetic-mean objective;
#   per_query_cap > 1 / None + MULTIPLICATIVE -> geometric-mean surrogate.
# There is deliberately NO min-max / worst-case optimization objective.  ``p90``,
# ``worst`` and ``geo``/``geomean(pop)`` are EVALUATION METRICS computed AFTER a
# solve is returned (derived reporting), never optimization objectives.  Only the
# ``mean``/geometric pairwise names below label what the successful class actually
# solves; no caller can select a "worst-case solver" because none exists.
# ---------------------------------------------------------------------------

# Slack on the multiplicative surrogate-floor rows.  Set to 0 so a selection that
# *exactly* reaches the q-error floor (surrogate == 1, e.g. one stat fully fixing
# a query) is still feasible; the constraint sum_s w_is x_is >= -log(e_i^0) already
# forbids any set that would push the geometric product below 1 (the solver keeps
# the surrogate >=1 by construction, so its objective equals the final decode).
_FLOOR_SLACK = 0.0


@dataclass(frozen=True)
class Option:
    """One (candidate, capacity) selection available to a query."""

    stat_index: int
    qerror: float
    level: int
    query: str = ""
    cand: str = ""

    def log_improvement(self, qbase: float) -> float:
        """w = log(e_is / e_i^0), <= 0 when the stat helps (multiplicative class)."""
        return float(np.log(max(self.qerror, 1e-12) / max(qbase, 1e-12)))

    def linear_improvement(self, qbase: float) -> float:
        """Δ_is = e_i^0 - e_is >= 0 (sparse-linear class).

        Under the sparse (per-query-1) model the objective becomes exactly
        linear: e_i = e_i^0 - Σ_s Δ_is x_is, so minimising Σ_i e_i is equivalent
        to maximising Σ_{i,s} Δ_is x_is (i.e. minimising -Δ).
        """
        return float(max(qbase, 1e-12) - max(self.qerror, 1e-12))


@dataclass(frozen=True)
class PhysicalStat:
    """A unique physical statistic (table, columns, capacity) with a cost."""

    table: str
    columns: tuple[str, ...]
    level: int
    cost: int
    # Maintenance cost of one deployed refresh (additive approximation);
    # 0.0 when the backend does not track it.
    maint_cost: float = 0.0
    key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "key",
            f"{self.table}|{','.join(self.columns)}|L{self.level}",
        )


@dataclass
class ILPResult:
    """Solution of the phase-2 ILP."""

    mean_qerror: float
    qerror_per_query: list[float]
    baseline_per_query: list[float]
    selected_stats: list[PhysicalStat]
    total_bytes: int
    total_maint: float
    chosen: list[list[str]]
    status: int
    message: str


@dataclass(frozen=True)
class MaintProfile:
    """Deployed-refresh maintenance-cost profile (Y-two-layer model).

    ``table_base_tiers`` maps a table to its *fixed* one-refresh ANALYZE cost
    ladder, indexed by the highest target level selected on that table:
      ``base_tiers[table] = (base_0, base_1, base_2)``
    where ``base_k`` is the fixed cost when the max selected level is `k`
    (targrows = max target ⇒ one shared sampling scan per activated table).

    Per-statistic *variable* cost is carried on each ``PhysicalStat.maint_cost``
    (now meaning the marginal payload-update term only).

    The total deployed maintenance of a selected set ``S`` is
      ``sum_t base_tiers[t][max_{s∈S∩t} level_s] + sum_{s∈S} var_s``
    which the solver enforces as a linear staircase charge per activated table
    plus a linear per-statistic term.
    """

    # table (qualified) -> fixed cost ladder per reached max-level.
    table_base_tiers: dict[str, tuple[float, ...]] = field(default_factory=dict)

    def tables(self) -> list[str]:
        return list(self.table_base_tiers)

    def max_tier(self, table: str) -> int:
        """Highest index in the fixed ladder for ``table`` (0 if absent)."""
        return max(0, len(self.table_base_tiers.get(table, ())) - 1)


def build_problem(
    phase1: dict,
    *,
    skip_worse_than_baseline: bool = True,
    qerror_mode: str = "first",
) -> tuple[list[PhysicalStat], list[list[Option]], list[float]]:
    """Build (phys_stats, queries_options, qerror_base) from a phase-1 dict.

    ``phase1`` is the ``{"results": [...]}`` structure produced by
    ``core.measure`` (v1-compatible shape): each result has ``qid``,
    ``qerror_base`` and ``candidates`` with per-level ``qerror``/``size_bytes``.
    """
    results = phase1["results"]
    qerror_base: list[float] = []
    queries_options: list[list[Option]] = []

    stat_index: dict[str, int] = {}
    phys_stats: list[PhysicalStat] = []

    def _summarize(lv: dict) -> float:
        reps = lv.get("qerror_repeats")
        if reps and qerror_mode != "first":
            clean = [v for v in reps if v == v]
            if not clean:
                return float("nan")
            if qerror_mode == "mean":
                return float(np.mean(clean))
            if qerror_mode == "worst":
                return float(np.max(clean))
            if qerror_mode == "p90":
                return float(np.percentile(clean, 90))
        return float(lv["qerror"])

    for r in results:
        base = float(r["qerror_base"])
        qerror_base.append(base)
        opts: list[Option] = []
        for cand_key, cand in r.get("candidates", {}).items():
            cols = tuple(cand["columns"])
            table = cand["table"]
            for level_str, lv in cand.get("levels", {}).items():
                level = int(level_str)
                qerr = _summarize(lv)
                if skip_worse_than_baseline and qerr >= base:
                    continue
                stat_key = f"{table}|{','.join(cols)}|L{level}"
                if stat_key not in stat_index:
                    stat_index[stat_key] = len(phys_stats)
                    phys_stats.append(
                        PhysicalStat(
                            table=table,
                            columns=cols,
                            level=level,
                            cost=int(lv["size_bytes"]),
                            maint_cost=float(lv.get("maint_cost", 0.0)),
                        )
                    )
                opts.append(
                    Option(
                        stat_index=stat_index[stat_key],
                        qerror=qerr,
                        level=level,
                        query=str(r["qid"]),
                        cand=cand_key,
                    )
                )
        queries_options.append(opts)

    return phys_stats, queries_options, qerror_base


def _overlap_pairs(query_options: list[Option], phys_stats: list[PhysicalStat]):
    """Yield pairs of option indices (within one query) whose stats share a
    column, so at most one can be chosen (keeps multiplicative approx valid)."""
    for a in range(len(query_options)):
        cols_a = set(phys_stats[query_options[a].stat_index].columns)
        for b in range(a + 1, len(query_options)):
            cols_b = set(phys_stats[query_options[b].stat_index].columns)
            if cols_a & cols_b:
                yield a, b


def select_optimizer_class(props) -> str:
    """Select the optimizer *class* from a backend's structural contract (§1.9).

    The class is chosen by the *decisive* structural dimensions only:
      - sparse_one_stat -> SPARSE_LINEAR  (per-query cap=1, exact arithmetic mean)
      - else            -> MULTIPLICATIVE (per-query cap>1/None, geometric mean)

    There is no objective argument: the optimization objective is determined by
    the class (cap=1 exact-arithmetic vs cap>1 geometric-surrogate).  Worst/p90/
    geomean are evaluation metrics computed after solving, not selectable here.
    """
    return OptimizerClass.SPARSE_LINEAR if props.sparse_one_stat else OptimizerClass.MULTIPLICATIVE


def solve_ilp(
    phys_stats: list[PhysicalStat],
    queries_options: list[list[Option]],
    qerror_base: list[float],
    budget_bytes: int,
    maint_budget: Optional[float] = None,
    maint_profile: Optional[MaintProfile] = None,
    per_query_cap: Optional[int] = None,
    global_disjoint: bool = False,
    optimizer_class: str = OptimizerClass.MULTIPLICATIVE,
) -> ILPResult:
    """Solve the multi-select shared-resource ILP with scipy.optimize.milp.

    Maintenance-cost modelling (optional Y-two-layer model, §1.7 / docs):
      - If ``maint_profile`` is provided, the solver enforces a *table-activated*
        staircase cost: each activated table pays its fixed ANALYZE base once,
        at the tier of the highest target level selected on it, plus a linear
        per-statistic variable term (``PhysicalStat.maint_cost`` = var onward).
        ``maint_budget`` then caps total_maint under this model.
      - If ``maint_profile`` is None (default) and ``maint_budget`` is set, the
        simpler additive approximation applies: ``sum_s maint_cost(s)*y_s <= M``,
        matching prior behaviour and preserving backward compatibility.
      - If neither is set, no maintenance constraint is added.

    ``optimizer_class`` selects the MILP class (§1.9), which FULLY determines the
    optimization objective (there is no separate objective switch):
      - ``OptimizerClass.MULTIPLICATIVE`` (default; combine with per_query_cap
        None or K>1): GEOMETRIC-mean surrogate.  Objective = min sum_i log \\hat
        e_i with \\hat e_i = e_i^0 * prod_s (e_is/e_i^0)^{x_is}; each surrogate is
        constrained >= 1 (linear per-query floor row), so solver objective equals
        the final decode and never optimises a below-1 product.  Combine only
        non-overlapping stats (Option A, independence model).
      - ``OptimizerClass.SPARSE_LINEAR`` (use with per_query_cap=1): EXACT
        arithmetic-mean objective max sum_{i,s} (e_i^0 - e_is) x_is — one stat per
        query, so the arithmetic mean is exactly linear.  Not geometric.

    There is intentionally NO worst-case / min-max objective.  ``p90``, ``worst``
    and ``geo`` are EVALUATION METRICS a caller may compute from the returned
    ``qerror_per_query`` AFTER solving; they never alter the solve itself.
    """
    n_stats = len(phys_stats)
    n_opt = sum(len(opts) for opts in queries_options)

    def _improve(o: Option, qbase: float) -> float:
        if optimizer_class == OptimizerClass.SPARSE_LINEAR:
            # minimise -Δ_is (equivalently maximise total improvement ΣΔ_is x_is).
            return -o.linear_improvement(qbase)
        return o.log_improvement(qbase)

    # -- maintenance (Y-two-layer) indicator planning ----------------------
    # When `maint_profile` is given, we add, per candidate table and per
    # achieved level threshold L>=1, an indicator `w_{t,L}` = table t has a
    # selected statistic whose level >= L.  `w_{t,0}` marks table activation.
    # Staircase fixed cost is charged as base0*w_{t,0} + Σ_{L>=1} Δ_L*w_{t,L}.
    use_profile = maint_profile is not None and bool(maint_profile.table_base_tiers)
    if use_profile:
        # Monotone fixed ladder is REQUIRED: reaching a higher level must never
        # be cheaper than a lower one, otherwise `base[L]-base[L-1]` (the
        # incremental maintenance charge of raising a table to tier L) would be
        # negative and the staircase linearization would *reward* higher tiers.
        # Refuse to build the MILP on a non-monotone profile.
        for t, tiers in maint_profile.table_base_tiers.items():  # type: ignore[union-attr]
            prev = None
            for L, val in enumerate(tiers):
                if prev is not None and val < prev - 1e-12:
                    raise ValueError(
                        f"MaintProfile fixed ladder for table {t!r} is not "
                        f"monotonically non-decreasing: tier L{L-1}={prev!r} > "
                        f"L{L}={val!r}. Higher levels must cost >= lower levels."
                    )
                prev = val

    # Multiplicative(log-space) vs exact-(linear) objective. The former combines
    # several non-overlapping stats per query via a geometric surrogate; the
    # latter is the exact arithmetic case (cap=1, one stat per query).
    multiplicative = optimizer_class != OptimizerClass.SPARSE_LINEAR

    # per-table set of levels among candidate physical statistics
    table_levels: dict[str, set[int]] = {}
    if use_profile:
        for ps in phys_stats:
            if ps.table in maint_profile.table_base_tiers:  # type: ignore[union-attr]
                table_levels.setdefault(ps.table, set()).add(ps.level)
    # maintenance indicator columns appended after x-variables
    main_cols: dict[tuple[str, int], int] = {}   # (table, threshold L>=1) -> col
    main_row0: dict[str, int] = {}               # (table) activation col (L=0)
    n_main = 0
    if use_profile:
        for t, lvls in table_levels.items():
            # activation indicator for table t
            main_row0[t] = n_stats + n_opt + n_main
            n_main += 1
            max_l = max(lvls) if lvls else 0
            for L in range(1, max_l + 1):
                if L - 1 < len(maint_profile.table_base_tiers[t]):  # type: ignore[union-attr]
                    main_cols[(t, L)] = n_stats + n_opt + n_main
                    n_main += 1
    n_var = n_stats + n_opt + n_main
    m = len(qerror_base)

    c = np.zeros(n_var)
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        for o in opts:
            c[n_stats + gi] = _improve(o, qbase)
            gi += 1
    integrality = np.ones(n_var)

    combo_groups: dict[tuple, list[int]] = {}
    for s_idx, ps in enumerate(phys_stats):
        combo_groups.setdefault((ps.table, ps.columns), []).append(s_idx)
    n_combo = len(combo_groups)
    colset = [set(ps.columns) for ps in phys_stats]

    if multiplicative:
        # Option A semantics (see module docstring / architecture §2-§3): a query
        # may combine only *non-overlapping* stats.  Add overlap-free rows for the
        # multiplicative decode (independent of whether an explicit cap is given),
        # plus one per-query surrogate-floor row so every selected surrogate stays
        # >= 1 (never optimised to an impossible below-1 geometric product).
        need_overlap = True
        n_overlap = 0
        for opts in queries_options:
            n_overlap += len(list(_overlap_pairs(opts, phys_stats)))
    else:
        need_overlap = False
        n_overlap = 0
    # Explicit per-query cap (0/1/K) — when provided, emit a cap row per query.
    need_cap = per_query_cap is not None
    n_cap = m if need_cap else 0
    n_floor = m if multiplicative else 0   # one surrogate-floor row per query
    n_extra = n_cap + n_overlap + n_floor + n_combo
    if global_disjoint:
        n_disjoint = sum(
            1
            for a in range(n_stats)
            for b in range(a + 1, n_stats)
            if colset[a] & colset[b]
        )
        n_extra += n_disjoint
    # maintenance enrollment: profile (Y-two-layer) or additive fallback
    has_maint_budget = use_profile or (maint_budget is not None and maint_budget > 0)
    n_ind_trig = 0
    if has_maint_budget:
        if use_profile:
            # per-y trigger rows: activation (y<=w_t0) + each L<=level_s with base
            for ps in phys_stats:
                t = ps.table
                if t in main_row0:
                    n_ind_trig += 1
                    towers = maint_profile.table_base_tiers[t]
                    for L in range(1, ps.level + 1):
                        if L - 1 < len(towers) and (t, L) in main_cols:
                            n_ind_trig += 1
            # plus one total-budget row
            n_extra += n_ind_trig + 1
        else:
            n_extra += 1
    n_con = 1 + n_opt + n_extra

    A = lil_matrix((n_con, n_var))
    ub = np.full(n_con, np.inf)
    lb = np.full(n_con, -np.inf)   # default: no lower bound (rows are <= / =), except
                                   # the per-query surrogate-floor rows (>= below).
    nrow = 0

    # 1) storage budget
    for s_idx, ps in enumerate(phys_stats):
        A[nrow, s_idx] = ps.cost
    ub[nrow] = budget_bytes
    nrow += 1

    # 1b) maintenance constraint ------------------------------------------
    if has_maint_budget and use_profile:
        # -- indicator-trigger rows: y_s activates its table's thresholds --
        for s_idx, ps in enumerate(phys_stats):
            t = ps.table
            if t not in main_row0:
                continue
            col_y = s_idx
            # y_s <= w_{t,0}
            A[nrow, col_y] = 1.0
            A[nrow, main_row0[t]] = -1.0
            ub[nrow] = 0.0
            nrow += 1
            towers = maint_profile.table_base_tiers[t]
            for L in range(1, ps.level + 1):
                if L - 1 < len(towers) and (t, L) in main_cols:
                    A[nrow, col_y] = 1.0
                    A[nrow, main_cols[(t, L)]] = -1.0
                    ub[nrow] = 0.0
                    nrow += 1
        # -- total budget row: Σ_t(charge_t) + Σ_s var_s * y_s <= M --
        for t, base in maint_profile.table_base_tiers.items():
            if t not in main_row0:
                continue  # no candidate stats on this table in this problem
            # charge = base[0]*w0 + Σ_{L>=1} (base[L]-base[L-1]) * w_{t,L}
            b0 = base[0] if len(base) > 0 else 0.0
            A[nrow, main_row0[t]] = b0
            for L, col in main_cols.items():
                tL, th = L
                if tL == t and th - 1 < len(base):
                    A[nrow, col] += (base[th] - base[th - 1])
        for s_idx, ps in enumerate(phys_stats):
            A[nrow, s_idx] += ps.maint_cost  # var term
        ub[nrow] = float(maint_budget)
        nrow += 1
    elif has_maint_budget:
        # additive fallback: sum_s var_s * y_s <= M  (maint_cost == var)
        for s_idx, ps in enumerate(phys_stats):
            A[nrow, s_idx] = ps.maint_cost
        ub[nrow] = float(maint_budget)
        nrow += 1

    # 2) x_is - y_s <= 0
    gi = 0
    for opts in queries_options:
        for o in opts:
            A[nrow, n_stats + gi] = 1.0
            A[nrow, o.stat_index] = -1.0
            ub[nrow] = 0.0
            gi += 1
            nrow += 1

    # 3a) per-query cap (when an explicit cap 0/1/K is given)
    if need_cap:
        gi = 0
        for opts in queries_options:
            for _ in opts:
                A[nrow, n_stats + gi] = 1.0
                gi += 1
            ub[nrow] = float(per_query_cap)
            nrow += 1
    # 3b) overlap-free within query (Option A, multiplicative / independence):
    #     a query may never select two stats sharing a column, so at most one wins.
    if need_overlap:
        gi = 0
        for opts in queries_options:
            for a, b in _overlap_pairs(opts, phys_stats):
                A[nrow, n_stats + gi + a] = 1.0
                A[nrow, n_stats + gi + b] = 1.0
                ub[nrow] = 1.0
                nrow += 1
            gi += len(opts)
    # 3c) per-query surrogate floor (multiplicative only): keep every surrogate
    #     q-error >= 1, so the solver never optimises an impossible below-1
    #     geometric product.  In log space, w_is = log(e_is / e_i^0) (<=0) and
    #         log \\hat e_i = log e_i^0 + sum_{s} w_is x_is >= 0   <=>  \\hat e_i >= 1
    #         sum_s w_is x_is >= -log e_i^0   (a LINEAR row per query).
    #     Empty selection always satisfies it (e_i^0 >= 1), so it only forbids
    #     over-combining stats beyond the physical floor; the solver objective
    #     therefore stays exactly equal to the final decode (no post-hoc clamp).
    if multiplicative:
        gi = 0
        for b_i, opts in zip(qerror_base, queries_options):
            # require: sum_s w_is x_is >= -log(b_i)  (surrogate >= 1)
            lb[nrow] = -math.log(max(b_i, 1e-12)) + _FLOOR_SLACK
            for j, o in enumerate(opts):
                A[nrow, n_stats + gi + j] = o.log_improvement(b_i)
            nrow += 1
            gi += len(opts)

    # 4) column-combo level exclusivity
    for members in combo_groups.values():
        for s_idx in members:
            A[nrow, s_idx] = 1.0
        ub[nrow] = 1.0
        nrow += 1

    # 5) global disjointness (optional)
    if global_disjoint:
        for a in range(n_stats):
            for b in range(a + 1, n_stats):
                if colset[a] & colset[b]:
                    A[nrow, a] = 1.0
                    A[nrow, b] = 1.0
                    ub[nrow] = 1.0
                    nrow += 1

    constraints = LinearConstraint(A.tocsr(), lb=lb, ub=ub)
    bounds = Bounds(lb=np.zeros(n_var), ub=np.ones(n_var))

    res = milp(c=c, integrality=integrality, bounds=bounds, constraints=constraints)
    if res.x is None:
        raise RuntimeError(f"ILP failed: {res.message}")

    x = res.x
    selected = [phys_stats[s_idx] for s_idx in range(n_stats) if x[s_idx] > 0.5]
    total_bytes = int(sum(ps.cost for ps in selected))

    # Decode total maintenance. With the Y-two-layer profile, each activated
    # table pays its staircase fixed charge once (at the max reached level),
    # plus per-stat variable terms.
    if use_profile:
        total_maint = 0.0
        # per-stat variable (maint_cost = var onward)
        for s_idx, ps in enumerate(phys_stats):
            if x[s_idx] > 0.5:
                total_maint += ps.maint_cost
        # table fixed charges from the indicator solution
        for t, base in maint_profile.table_base_tiers.items():
            if t not in main_row0:
                continue
            if x[main_row0[t]] > 0.5:
                # charge at the highest reached threshold L
                reached = 0
                for (tt, L), col in main_cols.items():
                    if tt == t and L - 1 < len(base) and x[col] > 0.5:
                        reached = max(reached, L)
                total_maint += base[min(reached, len(base) - 1)]
        total_maint = float(total_maint)
    else:
        total_maint = float(sum(ps.maint_cost for ps in selected))

    qerr_per_query: list[float] = []
    chosen: list[list[str]] = []
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        sel_keys: list[str] = []
        if optimizer_class == OptimizerClass.SPARSE_LINEAR:
            # exactly-linear decode: e_i = e_i^0 - sum_s Δ_is x_is
            q = max(qbase, 1e-12)
            for j, o in enumerate(opts):
                if x[n_stats + gi + j] > 0.5:
                    q -= o.linear_improvement(qbase)
                    sel_keys.append(phys_stats[o.stat_index].key)
            qerr_per_query.append(float(max(q, 1.0)))
        else:
            # multiplicative decode in log space
            log_t = np.log(max(qbase, 1e-12))
            for j, o in enumerate(opts):
                if x[n_stats + gi + j] > 0.5:
                    log_t += o.log_improvement(qbase)
                    sel_keys.append(phys_stats[o.stat_index].key)
            qerr_per_query.append(float(np.exp(log_t)))
        chosen.append(sel_keys)
        gi += len(opts)

    mean_qerror = float(np.mean(qerr_per_query))
    return ILPResult(
        mean_qerror=mean_qerror,
        qerror_per_query=qerr_per_query,
        baseline_per_query=list(qerror_base),
        selected_stats=selected,
        total_bytes=total_bytes,
        total_maint=total_maint,
        chosen=chosen,
        status=int(res.status),
        message=str(res.message),
    )
