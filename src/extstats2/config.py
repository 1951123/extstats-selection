"""Global configuration for the v2 toolkit.

Makes the *backend* (and thus the whole measurement pipeline) pluggable via a
factory.  Keeps core/ and plan/ free of any imported concrete backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
RESULTS_DIR = REPO_ROOT / "results"


# ---------------------------------------------------------------------------
# Backend factory (the single place that knows about concrete backends)
# ---------------------------------------------------------------------------

def get_backend(name: str, **kwargs):
    """Return a concrete backend instance by name.

    Importing the concrete backend module happens here, at the edge of the
    program, so ``core/`` and ``plan/`` never import a backend directly.
    """
    if name == "postgres":
        from .backend.postgres import PostgresBackend
        return PostgresBackend(**kwargs)
    if name == "oracle":
        from .backend.oracle import OracleBackend
        return OracleBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r}; expected 'postgres' or 'oracle'")


# ---------------------------------------------------------------------------
# Capacities
# ---------------------------------------------------------------------------

# LEGACY — old v1 "capacity level -> native knob" ladder with THREE levels
# (PG statistics_target 100/1000/10000; Oracle estimate_percent 1/10/100).
# The CURRENT canonical S-grid / lambda-first design has exactly TWO sampling
# levels, L0=30000 / L1=300000 requested rows, realized per (owner) table as
#   S_realized(t, L) = min(S_L, N_t),
# with each backend mapping that realized S to its native parameter itself
# (PG: statistics_target = S_L/300; Oracle: estimate_percent = 100*S/N_t).
# This legacy ladder is kept for the old capacity-era evaluators / scratch and
# migration; the S-grid source of truth is driven from measure_lambda's lambda
# levels (see docs/measure.md §1 / config DEFAULT_LAMBDA_LEVELS in
# core/measure_lambda).  Prefer those over this ladder for new code.
#
# Canonical capacity ladders, one per backend, mapping abstract level index ->
# native parameter(s).  Level indices are what the core / ILP sees.
CAPACITY_LADDERS: dict[str, dict[int, dict]] = {
    # PG: level -> statistics_target
    "postgres": {
        0: {"statistics_target": 100},
        1: {"statistics_target": 1000},
        2: {"statistics_target": 10000},
    },
    # Oracle: level -> estimate_percent (sampling) + buckets
    "oracle": {
        0: {"estimate_percent": 1, "buckets": 254},
        1: {"estimate_percent": 10, "buckets": 254},
        2: {"estimate_percent": 100, "buckets": 254},
    },
}


def capacity_ladder(backend: str) -> dict[int, dict]:
    return CAPACITY_LADDERS.get(backend, {})


# ---------------------------------------------------------------------------
# S-grid sampling levels (CANONICAL truth for the lambda-first design)
# ---------------------------------------------------------------------------
# The current / canonical S-grid / lambda-first model keys sampling by the
# REQUESTED sample rows S_L per level L, NOT by an abstract "capacity level"
# (the legacy CAPACITY_LADDERS above is kept only for old evaluators/scratch).
#   * Only TWO levels today:  L0 -> 30k rows, L1 -> 300k rows (project scope;
#     L2/full-scan was dropped for speed, restore by adding a level if needed).
#   * Per (owner) table the actually sampled rows are
#         S_realized(t, L) = min(S_L, N_t)
#     (small/mid tables saturate at N_t; large tables get the two distinct S).
#   * Each backend maps the SAME realized-S to its native parameter:
#         PG      : statistics_target = S_L / 300   (=> single_col target 100/1000)
#         Oracle  : estimate_percent   = 100 * S_realized(t,L) / N_t
#     (precisely what core.measure_lambda / the backend lambda_S helpers drive).
#
# The active lambda tier indices used by core.measure_lambda are the SAME
# DEFAULT_LAMBDA_LEVELS=(0,1); they are kept there (not referenced into a
# backend-importing module) to avoid a config<->core import cycle.
SAMPLING_LEVELS: dict[int, int] = {
    0: 30000,     # L0 : requested sample rows
    1: 300000,    # L1 : requested sample rows
}

def sampling_requested_rows(level: int) -> int:
    """Requested sample rows S_L for S-grid ``level`` (canonical, per design doc)."""
    if level not in SAMPLING_LEVELS:
        raise KeyError(
            f"sample level {level!r} not in the S-grid SAMPLING_LEVELS "
            f"{sorted(SAMPLING_LEVELS)} (L0=30000, L1=300000)."
        )
    return SAMPLING_LEVELS[level]

# Realized sample rows on a table with ``n_rows`` rows at S-grid ``level``.
def realized_sampling_rows(level: int, n_rows: int) -> int:
    return min(sampling_requested_rows(level), int(n_rows))

# PG statistics_target realizing S_L (== S_L / 300 -> 100 @ L0, 1000 @ L1).
def pg_target_for_sampling(level: int) -> int:
    return max(1, int(sampling_requested_rows(level) / 300))


# ---------------------------------------------------------------------------
# DB connection (backend-specific defaults, overridable via env)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DBConfig:
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    dbname: str = "postgres"
    # Oracle-specific extras (thin driver)
    service: Optional[str] = None       # service name / SID
    mode: str = "thin"                  # "thin" | "thick"

    @classmethod
    def from_env(cls) -> "DBConfig":
        return cls(
            host=os.environ.get("DBHOST", "localhost"),
            port=int(os.environ.get("DBPORT", "5432")),
            user=os.environ.get("DBUSER", "postgres"),
            password=os.environ.get("DBPASSWORD", ""),
            dbname=os.environ.get("DBNAME", "postgres"),
            service=os.environ.get("DBSERVICE"),
        )


# Default database per benchmark (matches init_*.sh conventions).
# v2 supports Census and stats_CEB (incl. its single-table sub-plans);
# JOB is dropped (join-heavy, extended statistics cannot fix join error).
DEFAULT_DB = {
    "census": "census",
    "stats_ceb": "stats",
    "stats_ceb_single": "stats",
}


@dataclass
class Config:
    backend: str = "postgres"          # backend name (see get_backend)
    bench: str = "census"
    budget_bytes: int = 0              # storage budget; 0 = unlimited
    maint_budget: Optional[float] = None  # maintenance budget; None/0 = unconstrained
    objective: str = "mean"            # reporting/eval metric label only (NOT an
                                       # optimizer objective; worst/p90/geomean are
                                       # derived metrics, not selectable by the MILP)
    capacities: tuple[int, ...] = (0, 1, 2)   # abstract level indices to probe
    protocol: Optional[str] = None     # None -> backend decides (a/m)
    db: DBConfig = field(default_factory=DBConfig.from_env)

    def ladder(self) -> dict[int, dict]:
        return capacity_ladder(self.backend)


def env_or(name: str, default: str) -> str:
    return os.environ.get(name, default)
