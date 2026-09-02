"""Measurement scheduler (backend-oblivious).

v1 had two hard-coded, PostgreSQL-tied measurement engines (``measure.py`` for
Protocol-A, ``measure_mask.py`` for Protocol-M) that both reached into PG catalogs
and DDL directly. v2 collapses measurement into a single scheduler in ``core``
that drives whatever :class:`~extstats2.backend.base.Backend` it is given:

    for each query:
        base = backend.estimate(query)              # no extended statistics
        for each candidate (table, columns):
            for each capability supported by backend:
                for each capacity level:
                    # Protocol-A or Protocol-M, chosen by backend
                    with backend.isolate({that one stat}, table):
                        backend.build_stats([stat], capacity)
                        est = backend.estimate(query)
                        size = backend.stat_size_bytes(stat)

The *protocol* ("a" = clean isolation, "m" = catalog-mask acceleration) is a
backend decision: ``backend.protocol(None)`` resolves it. Protocol-M lives in
``plan/protocol_m.py`` and is only invoked for backends that support it.

Output shape mirrors v1's ``QueryMeasurement`` so the ILP stage consumes it
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..backend.base import Backend, StatObject
from ..backend.capabilities import Capacity
from .candidates import CandidateSet
from .queries import BenchQuery


@dataclass
class CandidateMeasurement:
    """Per-(candidate, level[, repeat]) measurements for one query."""

    table: str
    columns: tuple[str, ...]
    capability: str
    levels: dict[int, dict] = field(default_factory=dict)
    # levels[level] = {"estimate": int, "qerror": float, "size_bytes": int,
    #                  "maint_cost": float, "qerror_repeats": list[float],
    #                  "capacity": Capacity}


@dataclass
class QueryMeasurement:
    """Measurements for one query (v1-compatible shape)."""

    qid: str
    bench: str
    qerror_base: float
    estimate_base: int
    actual: Optional[int]
    candidates: dict[str, CandidateMeasurement] = field(default_factory=dict)


def measure_query(
    backend: Backend,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    capabilities: Optional[list[str]] = None,
    capacity_levels: tuple[int, ...] = (0, 1, 2),
    capacity_labels: Optional[dict[int, str]] = None,
    protocol: Optional[str] = None,
    repeats: int = 1,
) -> QueryMeasurement:
    """Measure ``query`` under each (candidate, capability, capacity level).

    ``candidates`` are column combinations; the backend decides the effective
    protocol. ``capabilities`` restricts which capabilities to probe (default:
    every capability the backend supports). ``capacity_levels`` are abstract
    level indices; the backend maps each to a native parameter.

    ``repeats>1`` re-runs the build+measure cycle per (candidate, level) to
    capture estimate variability (as in v1).
    """
    prot = backend.protocol(protocol)

    # Baseline: no extended statistics at all.
    base = backend.estimate(query)
    mes = QueryMeasurement(
        qid=query.qid,
        bench=query.bench,
        qerror_base=base.qerror if base.qerror is not None else float("nan"),
        estimate_base=base.estimate,
        actual=query.ground_truth,
    )

    supported = {c.name: c for c in backend.supported_capabilities() if c.supported}
    wanted = capabilities or list(supported.keys())
    wanted = [name for name in wanted if name in supported]

    def _capacity(level: int) -> Capacity:
        label = (capacity_labels or {}).get(level, str(level))
        return Capacity(level, label)

    for cand in candidates:
        for cap_name in wanted:
            cap = supported[cap_name]
            cm = CandidateMeasurement(
                table=cand.table,
                columns=cand.columns,
                capability=cap_name,
            )
            for level in capacity_levels:
                stat = StatObject(
                    table=cand.table,
                    columns=cand.columns,
                    capability=cap,
                    capacity=_capacity(level),
                    name=backend_stat_name(backend, cand, cap_name, level),
                )
                qerrs: list[float] = []
                sizes: list[int] = []
                estimates: list[int] = []
                for _ in range(repeats):
                    with backend.isolate({stat}, cand.table):
                        # ensure the candidate statistic exists before building
                        # (isolate keeps `stat` active; create is idempotent).
                        backend.create_stat(stat)
                        backend.build_stats([stat], _capacity(level))
                        est = backend.estimate(query)
                        sizes.append(backend.stat_size_bytes(stat))
                        estimates.append(est.estimate)
                        qerrs.append(est.qerror if est.qerror is not None else float("nan"))
                # ``maint_cost`` here carries the per-statistic *variable* refresh
                # term only.  The *fixed* table-level ANALYZE cost (shared per
                # activated table, targrows=max(target)) is not charged here — it
                # is supplied to the ILP as a MaintProfile (table_base_tiers) via
                # backend.table_maintain_tiers, so a table with several selected
                # statistics pays its fixed scan only once.
                cm.levels[int(level)] = {
                    "estimate": estimates[0],
                    "qerror": _mean(qerrs),
                    "qerror_repeats": qerrs,
                    "size_bytes": _mean_int(sizes),
                    "maint_cost": backend.stat_maintain_var(stat),
                    "level": level,
                }
            mes.candidates[f"{cand.table_unqualified}({','.join(cand.columns)})"] = cm

    return mes


def backend_stat_name(backend: Backend, cand: CandidateSet, cap_name: str,
                      level: int) -> str:
    """Default statistic object name; backends may override via their own naming
    after calling this.  Kept deterministic and lowercase (PG folds identifiers;
    Oracle extension names differ and will override)."""
    tbl = cand.table_unqualified.lower()
    cols = "_".join(c.lower() for c in cand.columns)
    tag = {"dependency": "d", "ndistinct": "n", "mcv": "m"}.get(cap_name, "x")
    return f"ext_{tag}_{tbl}_{cols}_l{level}"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _mean_int(xs: list[int]) -> int:
    return int(round(sum(xs) / len(xs))) if xs else 0
