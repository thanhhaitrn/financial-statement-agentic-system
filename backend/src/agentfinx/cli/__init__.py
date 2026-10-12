"""Unified ``agentfinx`` command line.

Each command's ``main`` lives in its own module; this dispatcher is the one
installed entry point and routes a subcommand to it.

    agentfinx ask ...            -> workflow.pipeline      (build a dataset / answer a question)
    agentfinx serve ...          -> api.cli.serve_main     (the API behind the Next.js UI)
    agentfinx worker ...         -> api.cli.worker_main    (--queue chat|import)
    agentfinx invites ...        -> api.cli.invites_main   (sign-up invites)
    agentfinx db init            -> api.cli.db_main        (account database, new installs)
    agentfinx storage ...        -> datasets.storage_migration (status, migrate)
    agentfinx datasets gc        -> datasets.gc            (superseded Qdrant builds)
    agentfinx reports ...        -> acquisition.cli        (report discovery and import)
    agentfinx refresh-symbols    -> acquisition.refresh_symbols (CafeF ticker catalog)
    agentfinx batch ...          -> evaluation.batch       (many questions, resumable)
    agentfinx predict ...        -> evaluation.predict     (answers + contexts for scoring)
    agentfinx score ...          -> evaluation.score       (RAGAS, diagnostic only)
    agentfinx recall ...         -> evaluation.recall      (deterministic factual-recall gate)
    agentfinx analyze ...        -> evaluation.metrics_report
    agentfinx triage ...         -> evaluation.triage      (prediction errors)
    agentfinx eval-routing ...   -> evaluation.task_routing (planner task/web decisions)
    agentfinx eval-context ...   -> evaluation.chat_context (follow-up question resolver)
"""

from __future__ import annotations

import sys
from typing import Callable

# Subcommand -> (module[:function], accepts an argv list).  argv-aware mains
# get the remaining args directly; the older no-argv mains read ``sys.argv``,
# so we hand them a rebuilt ``sys.argv`` instead.
_COMMANDS: dict[str, tuple[str, bool]] = {
    "ask": ("agentfinx.workflow.pipeline", False),
    "batch": ("agentfinx.evaluation.batch", False),
    "predict": ("agentfinx.evaluation.predict", True),
    "score": ("agentfinx.evaluation.score", True),
    "recall": ("agentfinx.evaluation.recall", True),
    "analyze": ("agentfinx.evaluation.metrics_report", True),
    "reports": ("agentfinx.acquisition.cli", True),
    "serve": ("agentfinx.api.cli:serve_main", True),
    "worker": ("agentfinx.api.cli:worker_main", True),
    "invites": ("agentfinx.api.cli:invites_main", True),
    "db": ("agentfinx.api.cli:db_main", True),
    "storage": ("agentfinx.datasets.storage_migration", True),
    "datasets": ("agentfinx.datasets.gc", True),
    "refresh-symbols": ("agentfinx.acquisition.refresh_symbols", False),
    "triage": ("agentfinx.evaluation.triage", True),
    "eval-routing": ("agentfinx.evaluation.task_routing", True),
    "eval-context": ("agentfinx.evaluation.chat_context", True),
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
