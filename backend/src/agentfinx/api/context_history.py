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
    # A completed answer closes earlier clarification threads. Only the
    # unfinished chain after the most recent QA answer can clarify a reply.
    last_answer = max((index for index, row in enumerate(rows)
                       if row["context_status"] != "needs_clarification"), default=-1)
    for index, row in enumerate(rows):
        is_clarification = row["context_status"] == "needs_clarification"
        if is_clarification and index < last_answer:
            continue
        try:
            resolution = json.loads(row["context_resolution"] or "{}")
        except (TypeError, ValueError):
            resolution = {}
        if not isinstance(resolution, dict):
            resolution = {}
        pending = resolution.get("resolution") or {}
        clarification_question = pending.get("clarification_question", "") if isinstance(pending, dict) else ""
        if is_clarification and (not isinstance(clarification_question, str)
                                 or not clarification_question.strip()
                                 or len(clarification_question) > 2000):
            continue
        turns.append(ContextTurn(
            run_id=row["id"],
            effective_question=row["effective_question"] or row["question"],
            legacy=resolution.get("contract_version") != CONTEXT_CONTRACT_VERSION,
            snapshot=read_context_snapshot(row["context_snapshot"], run_id=row["id"],
                                           dataset_id=run["dataset_id"], generation=run["dataset_generation"]),
            clarification_question=clarification_question if is_clarification else "",
        ))
    return bounded_history(turns)
