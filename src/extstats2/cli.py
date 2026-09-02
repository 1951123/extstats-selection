"""CLI entry point for the v2 toolkit (skeleton).

Pipeline matches v1: ``generate -> measure -> optimize -> verify``, but every
step is backend-agnostic via :func:`extstats2.config.get_backend`.
The CLI wiring is intentionally thin; the staging is filled in as backends (M2/M3)
land. For now it validates configuration and backend selection.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .core.optimize import select_optimizer_class


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="extstats2", description=__doc__)
    p.add_argument("--backend", choices=["postgres", "oracle"], default="postgres")
    p.add_argument("--bench", default="census",
                   choices=["census", "stats_ceb", "stats_ceb_single"],
                   help="benchmark (JOB dropped: join-heavy, extended stats cannot fix join error)")
    p.add_argument("--budget-bytes", type=int, default=0, help="storage budget (0=unlimited)")
    p.add_argument("--maint-budget", type=float, default=None,
                   help="maintenance budget for the ILP (None/0=unconstrained)")
    p.add_argument("--objective", default="mean",
                   choices=["mean", "geomean", "worst", "p90"],
                   help="objective aggregation (must be in backend supports_objectives)")
    p.add_argument("--capacities", type=int, nargs="+", default=[0, 1, 2],
                   help="abstract capacity level indices to probe")
    p.add_argument("--protocol", choices=["a", "m", None], default=None)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate config + backend selection")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = config.Config(
        backend=args.backend,
        bench=args.bench,
        budget_bytes=args.budget_bytes,
        maint_budget=args.maint_budget,
        objective=args.objective,
        capacities=tuple(args.capacities),
        protocol=args.protocol,
    )
    if args.command == "check":
        backend = config.get_backend(cfg.backend)
        props = backend.structural_props()
        opt_class = select_optimizer_class(props, cfg.objective)
        print(f"backend      : {backend.name()}")
        print(f"capabilities : {[str(c) for c in backend.supported_capabilities()]}")
        print(f"protocol     : {backend.protocol(cfg.protocol)} (requested={cfg.protocol})")
        print(f"bench        : {cfg.bench}")
        print(f"capacities   : {cfg.capacities}")
        print(f"budget_bytes : {cfg.budget_bytes}")
        print(f"maint_budget : {cfg.maint_budget}")
        print(f"objective    : {cfg.objective}")
        print(f"optimizer    : {opt_class}")
        print(f"contract     : sparse={props.sparse_one_stat} "
              f"disjoint={props.disjoint_supported} "
              f"maint={props.maint_structure} cap={props.capacity_model} "
              f"objectives={props.supports_objectives}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
