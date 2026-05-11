"""CLI dispatcher for hub-docs-mining. Subcommands: triage | download | classify | aggregate."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hub-docs-mining",
        description="Mine the Seattle Emergency Hubs Drive audit for library candidates and corpus descriptions.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_triage = sub.add_parser("triage", help="Stage A — heuristic filter + LLM triage of CSV rows.")
    p_triage.add_argument("--limit", type=int, default=None, help="Process only the first N pending rows in each tier.")

    p_dl = sub.add_parser("download", help="Stage B — fetch library candidates from Drive.")
    p_dl.add_argument("--limit", type=int, default=None, help="Download at most N files in this invocation.")

    p_cls = sub.add_parser("classify", help="Stage C — classify downloaded files with LM Studio.")
    p_cls.add_argument("--limit", type=int, default=None, help="Classify at most N files in this invocation.")

    sub.add_parser("aggregate", help="Stage D — emit proposed manifest + provenance + descriptions.")

    args = parser.parse_args(argv)

    if args.cmd == "triage":
        from .triage import run
        run(limit=args.limit)
    elif args.cmd == "download":
        from .download import run
        run(limit=args.limit)
    elif args.cmd == "classify":
        from .classify import run
        run(limit=args.limit)
    elif args.cmd == "aggregate":
        from .aggregate import run
        run()
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
