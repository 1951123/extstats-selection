"""Per-λ (sampling-first) phase-1 result storage.

Stores the premeasure output of the converged model (§7bis): each *query* produces
a ``by_lambda`` dict whose outer key is a sample-tier level index. Every λ slot
holds BOTH the per-λ no-ext baseline ``e^0(S_λ)`` (single columns at ``S_λ/300``,
no extended stat) AND the candidate readings ``(colset, param) → q-error`` measured
in that same λ-state — so the optimizer reading this file gets the same-``S``
fair pairing ``Δ_{λ,(C,p)} = baseline.qerror − cand.qerror``.

Storage is **one JSON file per query** (decoupled production, incremental
re-runs, parallel-safe), plus a single root ``_meta.json`` carrying the shared
lambda-tier definitions (backend-agnostic level → S_rows/single_target).

Output layout under a results root ``outdir``::

    outdir/
      per_lambda/
        <workload>/             # one dir per workload
          <backend>/            # one dir per DBMS engine (PG/Oracle ...)
            _meta.json          # workload/backend + lambda tier + param grid
            <qid>.json          # one query's by_lambda block (+ actual)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Schema shapes (documented; measurement code targets these).
# ---------------------------------------------------------------------------

# A single lambda-tier descriptor (backend-agnostic on `level`; native params in
# fields so we record exactly what was physically set).
@dataclass
class LambdaTier:
    level: int                 # abstract level index (outer key in by_lambda)
    S_rows: Optional[int]      # sample rows this tier ANALYZEs (cap at N)
    single_target: Optional[int]   # PG: S/300 (all single cols) ; None if not PG
    estimate_percent: Optional[float]  # Oracle: scan % ; None if not Oracle

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "S_rows": self.S_rows,
            "single_target": self.single_target,
            "estimate_percent": self.estimate_percent,
        }


@dataclass
class Meta:
    bench: str
    backend: str
    # λ axis: the sample tiers sampled (level -> S_rows / single_target / ep).
    tiers: list[LambdaTier] = field(default_factory=list)
    # ext representation-parameter axis (INDEPENDENT of λ; offered per λ only
    # where p <= S_rows/300). Recorded so consumers know the full premeasure grid.
    param_tiers: tuple[int, ...] = ()
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bench": self.bench,
            "backend": self.backend,
            "tiers": [t.to_dict() for t in self.tiers],
            "param_tiers": list(self.param_tiers),
            **self.extra,
        }


# Each query file:  {"qid","actual","by_lambda": { "<level>": <lambda-slot> }}
# A lambda-slot:    {"S_rows","single_target","baseline": {estimate,qerror},
#                    "candidates":[ {"cols","param","estimate","qerror", ...fidelity} ]}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def result_dir(outdir: Path, workload: str, backend: str) -> Path:
    """Directory for one (workload, backend)'s per-λ results.

    Layout: ``outdir/per_lambda/<workload>/<backend>/`` when ``outdir`` is the
    results root (e.g. ``results``), or ``outdir/<workload>/<backend>/`` when
    ``outdir`` already points at ``per_lambda``. Two layers keep
    (a) different workloads and (b) different DBMS engines (which may hold the
    same column names but different native params/kinds) from ever colliding.
    """
    base = Path(outdir)
    if base.name == "per_lambda":
        return base / workload / backend
    return base / "per_lambda" / workload / backend


def write_meta(outdir: Path, meta: Meta) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / "_meta.json"
    p.write_text(json.dumps(meta.to_dict(), indent=2))
    return p


def write_query_measure(outdir: Path, block: dict) -> Path:
    """Write one query's per-λ block. ``block`` = {"qid","actual","by_lambda":...}."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    qid = block["qid"]
    p = outdir / f"{qid}.json"
    p.write_text(json.dumps(block, indent=2))
    return p


def read_meta(outdir: Path) -> Optional[Meta]:
    p = Path(outdir) / "_meta.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    tiers = [LambdaTier(**t) for t in d.get("tiers", [])]
    param_tiers = tuple(d.get("param_tiers", []))
    extra = {k: v for k, v in d.items()
             if k not in ("bench", "backend", "tiers", "param_tiers")}
    return Meta(bench=d.get("bench", ""), backend=d.get("backend", ""),
                tiers=tiers, param_tiers=param_tiers, extra=extra)


def read_query_measure(outdir: Path, qid: str) -> Optional[dict]:
    p = Path(outdir) / f"{qid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def list_qids(outdir: Path) -> list[str]:
    outdir = Path(outdir)
    if not outdir.exists():
        return []
    return sorted(
        p.stem for p in outdir.glob("*.json") if p.stem != "_meta"
    )


def load_workload(outdir: Path) -> dict:
    """Merge all per-query files into the ``results``-style structure the
    optimizer consumes: {"results": [ <per-query block>, ... ]}."""
    outdir = Path(outdir)
    results = []
    for qid in list_qids(outdir):
        b = read_query_measure(outdir, qid)
        if b is not None:
            results.append(b)
    return {"results": results, "meta": read_meta(outdir).to_dict()
            if read_meta(outdir) else {}}
