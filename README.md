# extstats2 — backend-agnostic budgeted selection of extended statistics

v2 of the extended-statistics-selection research toolkit. It generalises v1
(`extended-stats-optim`, PostgreSQL-only) so the same core algorithm runs
against **any database backend** that implements a small interface. First-class
backends: **PostgreSQL 16** and **Oracle**.

## Why a v2?

v1's algorithm core (candidate generation → measurement → budgeted ILP
allocation → verify) is valuable, but its PostgreSQL bindings were pervasive:
it generated PG `CREATE STATISTICS` DDL, read cardinalities from
`EXPLAIN (FORMAT JSON)`, and even manipulated `pg_statistic_ext_data` /
`pg_mcv_list` directly for its catalog-mask protocol. Migrating v1's ideas to
another engine meant rewriting the engine, not just adding a driver.

v2 draws a **backend abstraction seam** between the *database* (DDL, catalogs,
planner output, sampling semantics) and the *algorithm* (which statistics to
build and at what capacity). See [`docs/architecture.md`](docs/architecture.md)
for the full design.

## Layout

```
├── src/extstats2/
│   ├── core/            # database-agnostic algorithm (optimize = unchanged v1 port)
│   ├── backend/         # backend abstraction + postgres / oracle implementations
│   │   ├── base.py      #   Backend / Capability / StatObject / Estimate / isolate
│   │   ├── capabilities.py
│   │   ├── catalog.py   #   CatalogDriver (abstracts pg_statistic_ext_data / USER_* )
│   │   ├── postgres.py  #   PG16 (M2)
│   │   └── oracle.py    #   Oracle column groups (M3)
│   ├── plan/            # measurement protocols (Protocol-A universal, Protocol-M PG)
│   ├── config.py        # backend factory, capacity ladders
│   └── cli.py           # generate -> measure -> optimize -> verify
├── docs/architecture.md # design document
└── tests/
```

## Key abstractions

- **Capability** — a cross-backend notion of *what correlation a statistic
  captures* (`dependency` / `ndistinct` / `mcv`), each backend maps to its own
  implementation (PG `dependencies/ndistinct/mcv`; Oracle column-group stats).
- **Capacity** — an *abstract level index* the core passes through; the backend
  maps it to a native knob (PG `statistics_target`; Oracle `estimate_percent`).
- **Backend.isolate()** — abstracts *measurement isolation*: Protocol-A
  (drop/rebuild, universal) or Protocol-M (catalog-mask, PG-only acceleration).
- **CatalogDriver** — abstracts system-catalog access so no core code touches
  `pg_statistic_ext_data` / `USER_TAB_COL_STATISTICS`.

## Status / milestones

- [x] M1 scaffolding: abstractions (`base`/`capabilities`/`catalog`), core port
      (`optimize`, `candidates`, `predicates`, `measure`), config, CLI, design doc.
- [x] M2 PostgreSQL backend: estimate, DDL (create/drop/build), size, list,
      Protocol-A isolate (exception-safe), backend-owned capacity ladder,
      fixed+var `maintain_cost`. Protocol-M catalog-mask is a later enhancement.
- [x] M3 Oracle backend: column groups + Protocol-A (column-set isolate), mcv
      capability validated to repair selection cardinality on Census CLIMATE;
      FIXED_ONLY/per_scan maintenance model calibrated from measured GATHER
      times.
- [ ] M4 cross-backend validation (same core on PG & Oracle).

## Quick start

```bash
pip install -e .[dev]
python -m extstats2.cli check --backend postgres   # config/backend/sampling sanity only
```

The PostgreSQL backend requires a running PG 16 instance and a loaded benchmark
(`benchmarks/init_*.sh`), mirroring v1's conventions.

> **Note (canonical path).** The S-grid / sample-first research pipeline is driven
> from `scratch/` — e.g. `measure_sgrid.py` (per-sampling-level corpus into
> `results/measure/…`), `fit_maint_postgres.py` / `fit_maint_oracle.py`
> (`_maint.json`), and `measure_milp_curve.py` (budget×quality curves) — and the
> canonical library entry points are re-exported from `extstats2.core`
> (`measure_query_sampling*`, `measure_workload_sampling`, `optimize_sgrid`
> / `load_sgrid_problem`, `solve_ilp`). The model keys measurement by the REQUESTED
> sampling level `S`; the realized sample is `min(S, N_t)` and `λ = min(S, N_t)/N_t`
> is a derived reporting metric, not a search axis. Legacy v1 / capacity-era code
> (`core.measure`, `eval/cross_*`, `plan/`) has been removed.
