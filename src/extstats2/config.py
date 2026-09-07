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
    program, so ``core/`` never imports a backend directly.
    """
    if name == "postgres":
        from .backend.postgres import PostgresBackend
        return PostgresBackend(**kwargs)
    if name == "oracle":
        from .backend.oracle import OracleBackend
        return OracleBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r}; expected 'postgres' or 'oracle'")


# ---------------------------------------------------------------------------
# S-grid sampling levels (CANONICAL truth for the sample-first design)
# ---------------------------------------------------------------------------
# The current / canonical S-grid / sample-first model keys sampling by the
# REQUESTED sample rows S_L per level L (the old v1 "capacity level -> native
# knob" 3-level ladder was removed — single-version policy).
#   * Two levels today:  L0 -> 30k rows, L1 -> 300k rows (project scope;
#     extend by adding further entries to SAMPLING_LEVELS if needed).
#   * Per (owner) table the actually sampled rows are
#         S_realized(t, L) = min(S_L, N_t)
#     (small/mid tables saturate at N_t; large tables get the two distinct S).
#   * Each backend maps the SAME realized-S to its native parameter:
#         PG      : statistics_target = S_L / 300   (=> single_col target 100/1000)
#         Oracle  : estimate_percent   = 100 * S_realized(t,L) / N_t
#     (precisely what core.measure_sampling / the backend S-realization helpers
#     drive).
#
# The active sampling-level indices used by core.measure_sampling are the SAME
# DEFAULT_SAMPLING_LEVELS=(0,1); they are kept there (not referenced into a
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
    capacities: tuple[int, ...] = (0, 1)   # S-grid sampling level indices (L0/L1);
                                    # maps to requested rows via SAMPLING_LEVELS
    db: DBConfig = field(default_factory=DBConfig.from_env)


def env_or(name: str, default: str) -> str:
    return os.environ.get(name, default)
