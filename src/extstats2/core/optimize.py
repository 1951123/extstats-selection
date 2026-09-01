"""Phase-2 MILP: budgeted selection of (combo, capacity) options minimising
average q-error.  **Direct, unchanged port of v1 ``optimize.py``** — this model
is already fully backend-agnostic (it reasons only about column sets, per-level
q-errors, and byte costs), so it carries straight into v2 unchanged.

Model (multi-select, multiplicative approximation)
--------------------------------------------------
A query may select ANY subset of its candidate statistics (powerset semantics).
The joint effect is approximated multiplicatively in log space:

    log e_i(T_i) ≈ log e_i^0 + sum_{s in T_i} log(e_is / e_i^0)

To keep the approximation valid we forbid selecting column-overlapping statistics
within a single query, so the terms are independent.

Variables (all binary):
  - y_s : create physical statistic s (table, columns, capacity)
  - x_is: query i selects statistic s

Objective (minimise mean q-error):
    const + sum_{i,s} w_is * x_is,   w_is = log(e_is/e_i^0) <= 0

Constraints:
  1) storage budget  : sum_s c_s * y_s <= C
  2) select created  : x_is <= y_s
  3) overlap-free    : within each query, column-overlapping stats can't both
                       be chosen (keeps the multiplicative approximation valid)
  4) level exclusivity: at most one capacity level per (table, columns)
  5) (optional) global disjointness: no two created stats share a column

Because y_s is shared across queries (2) but paid once in the budget (1),
multiple queries reusing a statistic pay its storage only once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


@dataclass(frozen=True)
class Option:
    """One (candidate, capacity) selection available to a query."""

    stat_index: int
    qerror: float
    level: int
    query: str = ""
    cand: str = ""

    def log_improvement(self, qbase: float) -> float:
        """w = log(e_is / e_i^0), <= 0 when the stat helps."""
        return float(np.log(max(self.qerror, 1e-12) / max(qbase, 1e-12)))


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


def solve_ilp(
    phys_stats: list[PhysicalStat],
    queries_options: list[list[Option]],
    qerror_base: list[float],
    budget_bytes: int,
    maint_budget: Optional[float] = None,
    per_query_cap: Optional[int] = None,
    global_disjoint: bool = False,
) -> ILPResult:
    """Solve the multi-select shared-resource ILP with scipy.optimize.milp.

    ``maint_budget`` (optional): a hard budget on the total *maintenance cost*
    of the selected statistics, ``sum_s maint_cost(s) * y_s <= maint_budget``
    (additive approximation — see §1.7 [O1] / docs). When ``None`` (or ``<= 0``),
    no maintenance constraint is added and ``maint_cost`` is ignored, so existing
    call sites behave exactly as before.
    """
    n_stats = len(phys_stats)
    n_opt = sum(len(opts) for opts in queries_options)
    n_var = n_stats + n_opt
    m = len(qerror_base)

    c = np.zeros(n_var)
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        for o in opts:
            c[n_stats + gi] = o.log_improvement(qbase)
            gi += 1
    integrality = np.ones(n_var)

    combo_groups: dict[tuple, list[int]] = {}
    for s_idx, ps in enumerate(phys_stats):
        combo_groups.setdefault((ps.table, ps.columns), []).append(s_idx)
    n_combo = len(combo_groups)
    colset = [set(ps.columns) for ps in phys_stats]

    if per_query_cap is not None:
        n_extra = m + n_combo
    else:
        n_overlap = 0
        for opts in queries_options:
            n_overlap += len(list(_overlap_pairs(opts, phys_stats)))
        n_extra = n_overlap + n_combo
    if global_disjoint:
        n_disjoint = sum(
            1
            for a in range(n_stats)
            for b in range(a + 1, n_stats)
            if colset[a] & colset[b]
        )
        n_extra += n_disjoint
    # optional maintenance budget adds one constraint row
    has_maint = maint_budget is not None and maint_budget > 0
    if has_maint:
        n_extra += 1
    n_con = 1 + n_opt + n_extra

    A = lil_matrix((n_con, n_var))
    ub = np.full(n_con, np.inf)
    nrow = 0

    # 1) storage budget
    for s_idx, ps in enumerate(phys_stats):
        A[nrow, s_idx] = ps.cost
    ub[nrow] = budget_bytes
    nrow += 1

    # 1b) maintenance budget (optional, additive): sum_s maint_s * y_s <= M
    if has_maint:
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

    # 3a) per-query cap
    if per_query_cap is not None:
        gi = 0
        for opts in queries_options:
            for _ in opts:
                A[nrow, n_stats + gi] = 1.0
                gi += 1
            ub[nrow] = float(per_query_cap)
            nrow += 1
    # 3b) overlap-free within query
    else:
        gi = 0
        for opts in queries_options:
            for a, b in _overlap_pairs(opts, phys_stats):
                A[nrow, n_stats + gi + a] = 1.0
                A[nrow, n_stats + gi + b] = 1.0
                ub[nrow] = 1.0
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

    constraints = LinearConstraint(A.tocsr(), lb=np.full(n_con, -np.inf), ub=ub)
    bounds = Bounds(lb=np.zeros(n_var), ub=np.ones(n_var))

    res = milp(c=c, integrality=integrality, bounds=bounds, constraints=constraints)
    if res.x is None:
        raise RuntimeError(f"ILP failed: {res.message}")

    x = res.x
    selected = [phys_stats[s_idx] for s_idx in range(n_stats) if x[s_idx] > 0.5]
    total_bytes = int(sum(ps.cost for ps in selected))
    total_maint = float(sum(ps.maint_cost for ps in selected))

    qerr_per_query: list[float] = []
    chosen: list[list[str]] = []
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        log_t = np.log(max(qbase, 1e-12))
        sel_keys: list[str] = []
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
