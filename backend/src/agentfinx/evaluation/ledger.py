"""The evidence ledger of a run: what was retrieved for each requirement and
how calculations were sourced, plus the contract checks every evaluation
report applies to it. Shared by ``agentfinx batch`` and ``agentfinx predict``.
"""

from __future__ import annotations

import math
from typing import Any


def extract_evidence_ledger(final_state: dict) -> dict[str, Any]:
    """Return retrieval transitions and calculation provenance verbatim."""

    if not isinstance(final_state, dict):
        return {"schema_version": 1, "entries": []}
    raw = final_state.get("evidence_ledger")
    if raw is None:
        raw = final_state.get("calculation_evidence_ledger")
    if not raw:
        return {"schema_version": 1, "entries": []}
    if not isinstance(raw, dict):
        return {"schema_version": 1, "entries": []}
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    return {
        "schema_version": int(raw.get("schema_version", 1) or 1),
        "entries": [
            dict(entry)
            for entry in entries
            if isinstance(entry, dict)
        ],
    }


def evidence_ledger_contract_errors(ledger: dict[str, Any]) -> list[str]:
    """Validate retrieval audit entries and strict calculation provenance."""

    entries = ledger.get("entries", []) if isinstance(ledger, dict) else []
    if not entries:
        return []
    errors = []
    if ledger.get("schema_version") != 1:
        errors.append("evidence_ledger: unsupported schema_version")
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"evidence_ledger: entry {entry_index} is not an object")
            continue
        kind = str(entry.get("kind", "") or "").strip()
        if kind == "retrieval_requirement":
            errors.extend(
                _retrieval_ledger_contract_errors(entry, entry_index)
            )
            continue
        if kind not in {"", "derived_calculation"}:
            errors.append(
                f"evidence_ledger: entry {entry_index} unsupported kind {kind!r}"
            )
            continue
        if not str(entry.get("operation", "") or "").strip():
            errors.append(f"evidence_ledger: entry {entry_index} missing operation")
        if entry.get("result") in ("", None):
            errors.append(f"evidence_ledger: entry {entry_index} missing result")
        operands = entry.get("operands", [])
        if not isinstance(operands, list) or not operands:
            errors.append(f"evidence_ledger: entry {entry_index} missing operands")
            continue
        for operand_index, operand in enumerate(operands):
            if not isinstance(operand, dict):
                errors.append(
                    f"evidence_ledger: entry {entry_index} operand "
                    f"{operand_index} is not an object"
                )
                continue
            missing = [
                field
                for field in ("role", "fact_id", "source")
                if not str(operand.get(field, "") or "").strip()
            ]
            if missing:
                errors.append(
                    f"evidence_ledger: entry {entry_index} operand "
                    f"{operand_index} missing {','.join(missing)}"
                )
    return errors


def _retrieval_ledger_contract_errors(
    entry: dict[str, Any],
    entry_index: int,
) -> list[str]:
    prefix = f"evidence_ledger: entry {entry_index}"
    errors = []
    for field in ("requirement", "table"):
        if not str(entry.get(field, "") or "").strip():
            errors.append(f"{prefix} missing {field}")

    parsed_slots = entry.get("parsed_query_slots")
    if not isinstance(parsed_slots, dict):
        errors.append(f"{prefix} parsed_query_slots must be an object")

    routes = entry.get("route_candidates")
    if not isinstance(routes, list) or not routes:
        errors.append(f"{prefix} missing route_candidates")
    else:
        for route_index, route in enumerate(routes):
            if not isinstance(route, dict):
                errors.append(
                    f"{prefix} route {route_index} is not an object"
                )
                continue
            missing = [
                field
                for field in ("rank", "table", "confidence", "reason")
                if route.get(field) in ("", None)
            ]
            if missing:
                errors.append(
                    f"{prefix} route {route_index} missing {','.join(missing)}"
                )

    transition = entry.get("requirement_state")
    if not isinstance(transition, dict):
        errors.append(f"{prefix} requirement_state must be an object")
    else:
        missing = [
            field
            for field in ("before_retry", "after_retry")
            if not str(transition.get(field, "") or "").strip()
        ]
        if missing:
            errors.append(
                f"{prefix} requirement_state missing {','.join(missing)}"
            )

    retry = entry.get("targeted_retry")
    if not isinstance(retry, dict):
        errors.append(f"{prefix} targeted_retry must be an object")
    else:
        performed = retry.get("performed")
        if not isinstance(performed, bool):
            errors.append(f"{prefix} targeted_retry.performed must be boolean")
        elif performed and not str(retry.get("query", "") or "").strip():
            errors.append(
                f"{prefix} targeted_retry.query required when performed"
            )

    selected_facts = entry.get("selected_facts")
    if not isinstance(selected_facts, list):
        errors.append(f"{prefix} selected_facts must be a list")
    else:
        for fact_index, fact in enumerate(selected_facts):
            if not isinstance(fact, dict):
                errors.append(
                    f"{prefix} selected fact {fact_index} is not an object"
                )
                continue
            if not str(fact.get("fact_id", "") or "").strip():
                errors.append(
                    f"{prefix} selected fact {fact_index} missing fact_id"
                )
            rank = fact.get("rank")
            if (
                not isinstance(rank, int)
                or isinstance(rank, bool)
                or rank <= 0
            ):
                errors.append(
                    f"{prefix} selected fact {fact_index} has invalid rank"
                )
            rerank_score = fact.get("rerank_score")
            if (
                not isinstance(rerank_score, (int, float))
                or isinstance(rerank_score, bool)
                or not math.isfinite(float(rerank_score))
            ):
                errors.append(
                    f"{prefix} selected fact {fact_index} missing finite "
                    "rerank_score"
                )
            for score_field in (
                "similarity_score",
                "retrieval_score",
                "score",
                "distance",
            ):
                score_value = fact.get(score_field)
                if score_value in ("", None):
                    continue
                if (
                    not isinstance(score_value, (int, float))
                    or isinstance(score_value, bool)
                    or not math.isfinite(float(score_value))
                ):
                    errors.append(
                        f"{prefix} selected fact {fact_index} has invalid "
                        f"{score_field}"
                    )
            for provenance_field in (
                "score_query",
                "score_intent",
                "retrieval_origin",
            ):
                if not str(fact.get(provenance_field, "") or "").strip():
                    errors.append(
                        f"{prefix} selected fact {fact_index} missing "
                        f"{provenance_field}"
                    )
    return errors
