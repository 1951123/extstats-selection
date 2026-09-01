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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="extstats2", description=__doc__)
    p.add_argument("--backend", choices=["postgres", "oracle"], default="postgres")
    p.add_argument("--bench", default="census",
                   choices=["census", "job", "stats_ceb", "stats_ceb_single"])
    p.add_argument("--budget-bytes", type=int, default=0, help="storage budget (0=unlimited)")
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
        capacities=tuple(args.capacities),
        protocol=args.protocol,
    )
    if args.command == "check":
        backend = config.get_backend(cfg.backend)
        print(f"backend      : {backend.name()}")
        print(f"capabilities : {[str(c) for c in backend.supported_capabilities()]}")
        print(f"protocol     : {backend.protocol(cfg.protocol)} (requested={cfg.protocol})")
        print(f"bench        : {cfg.bench}")
        print(f"capacities   : {cfg.capacities}")
        print(f"budget_bytes : {cfg.budget_bytes}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
