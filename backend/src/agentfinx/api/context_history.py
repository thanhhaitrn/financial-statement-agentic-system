"""Build the resolver's bounded, owner/generation-scoped effective history."""

from __future__ import annotations

import json

from agentfinx.core.schemas.chat_context import CONTEXT_CONTRACT_VERSION, ContextTurn
from agentfinx.api import db as webdb
from agentfinx.api.clarify import MAX_TURNS, bounded_history
from agentfinx.api.context_snapshot import read_context_snapshot


def load_context_history(database: webdb.WebDB, run) -> list[ContextTurn]:
    with database.read() as conn:
        rows = webdb.list_context_runs(conn, run, limit=MAX_TURNS)
    turns = []
    for row in rows:
        try:
            resolution = json.loads(row["context_resolution"] or "{}")
        except (TypeError, ValueError):
            resolution = {}
        if not isinstance(resolution, dict):
            resolution = {}
        turns.append(ContextTurn(
            run_id=row["id"],
            effective_question=row["effective_question"] or row["question"],
            legacy=resolution.get("contract_version") != CONTEXT_CONTRACT_VERSION,
            snapshot=read_context_snapshot(row["context_snapshot"], run_id=row["id"],
                                           dataset_id=run["dataset_id"], generation=run["dataset_generation"]),
        ))
    return bounded_history(turns)
