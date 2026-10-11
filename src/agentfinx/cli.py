"""Unified ``agentfinx`` command line.

The heavy logic lives in the top-level entry modules (they stay importable as
first-class ``py-modules`` and keep their own ``main`` entry points). This
dispatcher gives the installed ``agentfinx`` command a single front door that
routes a subcommand to the matching module, instead of the previous hollow
``from test import main`` facade.

    agentfinx ask     ...   -> finx_pipeline.main            (build dataset / answer a query)
    agentfinx batch   ...   -> dataset_batch_runner.main
    agentfinx predict ...   -> dataset_batch_result.main
    agentfinx score   ...   -> ragas_eval_runner.main
    agentfinx recall  ...   -> eval_retrieval_recall.main
    agentfinx analyze ...   -> analyze_batch_metrics.main
    agentfinx serve   ...   -> webapp.cli.serve_main   (local web API + frontend)
    agentfinx worker  ...   -> webapp.cli.worker_main  (--queue chat|import)
    agentfinx invites ...   -> webapp.cli.invites_main (sign-up invites)
    agentfinx db init       -> webapp.cli.db_main      (account database, new installs)
    agentfinx storage ...   -> dataset_catalog.storage_migration.main (status, migrate)
    agentfinx datasets gc   -> dataset_catalog.gc.main (superseded Qdrant builds)
"""

from __future__ import annotations

import sys
from typing import Callable

# Subcommand -> (module[:function], accepts an argv list).  argv-aware mains
# get the remaining args directly; the older no-argv mains read ``sys.argv``,
# so we hand them a rebuilt ``sys.argv`` instead.
_COMMANDS: dict[str, tuple[str, bool]] = {
    "ask": ("finx_pipeline", False),
    "batch": ("dataset_batch_runner", False),
    "predict": ("dataset_batch_result", True),
    "score": ("ragas_eval_runner", True),
    "recall": ("eval_retrieval_recall", True),
    "analyze": ("analyze_batch_metrics", True),
    "reports": ("acquisition.cli", True),
    "serve": ("webapp.cli:serve_main", True),
    "worker": ("webapp.cli:worker_main", True),
    "invites": ("webapp.cli:invites_main", True),
    "db": ("webapp.cli:db_main", True),
    "storage": ("dataset_catalog.storage_migration", True),
    "datasets": ("dataset_catalog.gc", True),
}


def _load_main(target: str) -> Callable[..., object]:
    from importlib import import_module

    module_name, _, attribute = target.partition(":")
    module = import_module(module_name)
    return getattr(module, attribute or "main")


def _usage() -> str:
    commands = ", ".join(sorted(_COMMANDS))
    return f"usage: agentfinx <command> [args...]\n  commands: {commands}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_usage())
        return 0 if args else 2

    command = args[0]
    rest = args[1:]
    if command not in _COMMANDS:
        print(f"agentfinx: unknown command '{command}'\n{_usage()}", file=sys.stderr)
        return 2

    module_name, accepts_argv = _COMMANDS[command]
    entry = _load_main(module_name)

    if accepts_argv:
        return int(entry(rest) or 0)

    # No-argv main: rebuild sys.argv so its own argparse sees the passed args.
    saved = sys.argv
    sys.argv = [f"agentfinx {command}", *rest]
    try:
        return int(entry() or 0)
    finally:
        sys.argv = saved


if __name__ == "__main__":
    raise SystemExit(main())
