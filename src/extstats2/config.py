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
    objective: str = "mean"            # objective aggregation (mean|geomean|worst|p90)
    capacities: tuple[int, ...] = (0, 1, 2)   # abstract level indices to probe
    protocol: Optional[str] = None     # None -> backend decides (a/m)
    db: DBConfig = field(default_factory=DBConfig.from_env)

    def ladder(self) -> dict[int, dict]:
        return capacity_ladder(self.backend)


def env_or(name: str, default: str) -> str:
    return os.environ.get(name, default)
