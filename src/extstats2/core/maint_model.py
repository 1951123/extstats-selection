"""Linear maintenance-cost model and its measured parameters (2026-09-06).

Model
-----
Total (one maintenance refresh of an activated set of tables ``T``)::

    maint = Σ_{t ∈ T} [ fixed(t, ℓ_t)  +  c_var(t, ℓ_t) · n_t ]

* ``ℓ_t`` — refresh level of ``t`` this maintenance period (= its max selected λ).
* ``fixed(t, ℓ)`` — REAL measured one-refresh scan seconds of the bare table at
  that level's sampled ``S`` (the shared ANALYZE / GATHER). This is a whole-scan
  cost: it depends on the table and its sampling depth, hence is stored per
  ``(table, level)``.
* ``c_var(t, ℓ)`` — measured marginal per-extended-statistic cost on ``t`` at
  that level. Every extstat on the same ``(table, level)`` is treated as carrying
  the same marginal (assumption: uniform per-stat refresh cost).

Why an artifact, not source constants
-------------------------------------
``fixed`` and ``c_var`` are fitted OFFLINE against the live engine by a dedicated
estimator **separate from the per-query measure run** (a per-stat marginal is not
naively timer-measurable under a shared scan — the residual is tiny and noisy —
so ``c_var`` should come from an aggregate whole-scan difference). They are stored
next to the corpus as ``results/measure/<workload>/<backend>/_maint.json``. The
optimizer & reporting layers read them back; a *fallback* (the backend's
closed-form estimate) is supplied by the caller only for a table/level that has
not been measured.

Schema of ``_maint.json`` (table keys are the corpus dotted form, ``.climate``)::

    {
      "backend": "postgres",
      "fixed_seconds": { ".climate": {"0": 0.54, "1": 2.10}, ... },
      "c_var":         { ".climate": {"0": …,  "1": …},     ... }
    }

This module is deliberately DB-free: it owns the ``_maint.json`` schema, its IO,
and the pure linear-cost math, so it is unit-testable without an engine. The live
measurement that fills the params runs in a separate DB-timed runner (see
:func:`fit_maint_params` and the module's estimator note below).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .measure_io import result_dir

# Corpus-artifact filename living next to _meta.json inside a corpus dir.
MAINT_FILE = "_maint.json"
# JSON keys.
KEY_FIXED = "fixed_seconds"
KEY_CVAR = "c_var"


@dataclass(frozen=True)
class MaintParams:
    """Measured maintenance parameters for one (workload, backend) corpus.

    ``fixed_seconds[t][lvl]`` / ``c_var[t][lvl]``: seconds keyed by table
    (corpus dotted key) then λ level (kept as ``str`` for trivial JSON
    round-tripping; helpers coerce to ``int``).
    """
    backend: str = ""
    fixed_seconds: dict[str, dict[str, float]] = field(default_factory=dict)
    c_var: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"backend": self.backend,
                KEY_FIXED: self.fixed_seconds,
                KEY_CVAR: self.c_var}

    @staticmethod
    def from_dict(d: dict) -> "MaintParams":
        # Table keys are canonicalised to *lowercase* so lookups are
        # case-insensitive: the corpus may key camelCase tables (`.postHistory`)
        # while callers / config use lowercase (`.posthistory`). Both resolve.
        return MaintParams(
            backend=d.get("backend", ""),
            fixed_seconds={str(t).lower(): {str(l): float(v) for l, v in tab.items()}
                           for t, tab in d.get(KEY_FIXED, {}).items()},
            c_var={str(t).lower(): {str(l): float(v) for l, v in tab.items()}
                   for t, tab in d.get(KEY_CVAR, {}).items()},
        )

    # -- typed accessors --------------------------------------------------
    def fixed(self, table: str, level: int) -> Optional[float]:
        """Measured one-refresh fixed seconds for ``table`` at ``level``, or
        ``None`` when this (table, level) has not been measured. Table matching is
        case-insensitive (keys canonicalised to lowercase on load)."""
        d = self.fixed_seconds.get(str(table).lower())
        return d.get(str(level)) if d else None

    def var(self, table: str, level: int) -> Optional[float]:
        """Measured per-extstat marginal ``c_var`` seconds, or ``None``."""
        d = self.c_var.get(str(table).lower())
        return d.get(str(level)) if d else None

    # -- linear model -----------------------------------------------------
    def maint_for_table(self, table: str, level: int, n_stats: int) -> float:
        """``fixed(t,ℓ) + c_var(t,ℓ)·n_stats`` — linear maintenance cost of one
        refresh of ``table`` at ``level`` carrying ``n_stats`` extended stats.

        Raises ``KeyError`` if that (table, level) is unmeasured.
        """
        f = self.fixed(table, level)
        c = self.var(table, level)
        if f is None or c is None:
            raise KeyError(f"no measured maint params for "
                           f"(table={table!r}, level={level!r})")
        return f + c * n_stats

    def maint_for_table_or(self, table: str, level: int, n_stats: int,
                           fallback: Callable[[str, int, int], float]) -> float:
        """Like :meth:`maint_for_table` but where a (table, level) is unmeasured,
        delegates to ``fallback(table, level, n_stats)`` (caller's closed-form
        model), so a partially-fitted corpus still yields a number."""
        if self.fixed(table, level) is None or self.var(table, level) is None:
            return fallback(table, level, n_stats)
        return self.maint_for_table(table, level, n_stats)

    def maint_total(self, plan: dict[str, tuple[int, int]], *,
                    fallback: Optional[Callable[[str, int, int], float]] = None
                    ) -> float:
        """Total maintenance over a ``{table: (level, n_stats)}`` deployment plan
        (the tables activated this refresh). Sums the linear per-table cost;
        without ``fallback``, raises on the first unmeasured (table, level)."""
        tot = 0.0
        for table, (level, n_stats) in plan.items():
            if fallback is None:
                tot += self.maint_for_table(table, level, n_stats)
            else:
                tot += self.maint_for_table_or(table, level, n_stats, fallback)
        return tot


# ---------------------------------------------------------------------------
# I/O — corpus sibling _maint.json
# ---------------------------------------------------------------------------

def maint_path(outdir: Path, workload: str, backend: str) -> Path:
    """Path to the maintenance artifact under a results root.

    Mirrors :func:`result_dir`: ``outdir`` is normally the results root (→
    ``outdir/measure/<workload>/<backend>/_maint.json``). Pass a dir whose own
    name is ``measure`` only when ``outdir`` is already the corpus layer of a
    results root (the same rule ``result_dir`` applies).
    """
    return result_dir(Path(outdir), workload, backend) / MAINT_FILE


def read_maint(outdir: Path, workload: str, backend: str) -> Optional[MaintParams]:
    """Load fitted maintenance params for a (workload, backend) corpus. Returns
    ``None`` when no ``_maint.json`` has been produced yet."""
    p = maint_path(Path(outdir), workload, backend)
    if not p.exists():
        return None
    return MaintParams.from_dict(json.loads(p.read_text()))


def write_maint(outdir: Path, workload: str, backend: str,
                params: MaintParams) -> Path:
    """Persist ``params`` as ``_maint.json`` in the corpus dir."""
    p = maint_path(Path(outdir), workload, backend)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(params.to_dict(), indent=2))
    return p


# ---------------------------------------------------------------------------
# Estimator (offline, live-DB) — where the numbers come from
#
# ``fixed``/``c_var`` are NOT produced in this pure module; they are measured by a
# dedicated, DB-timed runner (separate from the per-query measure run), which for
# each corpus owner-table at each realised λ level:
#   fixed(t,ℓ) : time the BARE one-refresh scan in the λ-state — i.e.
#                `enter_sampling_state(table, level)` (PG: 1× ANALYZE of natural
#                single-col stats at S/300; Oracle: 1× GATHER at ep=100·S/N);
#   c_var(t,ℓ) : from an AGGREGATE whole-scan delta — time one refresh carrying
#                k representative column-group stats and take (refresh(k)−fixed)/k.
#                Group counts k give a larger, more stable signal than a per-stat
#                timer (which under a shared scan is a tiny, unreliable residual).
# (k=1 is the limit; the runner picks the probe count.) It drops the probe groups
# afterwards, leaving the table bare, and persists via MaintParams/write_maint.
#
# Keeping this runner OUT of this module keeps its logic DB-free and unit-testable;
# the runner imports MaintParams/estimate helpers here and drives the backend.
# ---------------------------------------------------------------------------

__all__ = [
    "MaintParams", "MAINT_FILE", "KEY_FIXED", "KEY_CVAR",
    "maint_path", "read_maint", "write_maint",
]
