"""Deterministic factual-recall gate for the retrieval layer.

The official gate population is the version-controlled facts contract.  Every
contract fact defines ``entity``, ``metric``, ``period``, ``value``, ``unit``
and ``reference``.  Values are matched only inside a single retrieved
fact/context, so unrelated fields from different rows cannot accidentally
satisfy a fact.  A predictions report is optional enrichment; it can neither
add gate identities nor replace the contract's expected facts.

Legacy helper APIs remain readable for historical report analysis, including
deterministic value derivation when an old record has no ``expected_facts``.
That compatibility path is never used by the official CLI gate.  RAGAS scores
are deliberately not used by this module.

Usage:
  python eval_retrieval_recall.py --ids 211,212,213
  python eval_retrieval_recall.py --predictions-file optional_report.json
  EVIDENCE_FACTS_LIMIT=10 NOTE_FACTS_LIMIT=12 python eval_retrieval_recall.py

Environment knobs are read by pipeline modules at import time; run one process
per configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from evaluation.contracts import (
    REPORT_SCHEMA_VERSION,
    atomic_write_json as _atomic_write_json,
    sha256_file,
    stable_json_fingerprint,
)
from evaluation.financial_text import (
    _DATE_RE,
    _QUARTER_RE,
    _infer_unit,
    _plain_text,
    normalize_fact,
    normalize_period,
    normalize_reference,
    normalize_text,
    normalize_unit,
    normalize_value,
)


FACT_FIELDS = ("entity", "metric", "period", "value", "unit", "reference")
FACTUAL_RECALL_THRESHOLD = 0.95
DEFAULT_FACTS_CONTRACT = "tests/fixtures/apec_q211_250_factual_facts.json"
OFFICIAL_CONTRACT_MARKER = "_official_factual_contract"
ANALYTICAL_MARKERS = (
    "đánh giá",
    "phân tích",
    "giải thích",
    "tác động",
    "rủi ro",
    "ý nghĩa",
    "nhận xét",
    "dự báo",
    "tại sao",
    "vì sao",
    "nếu ",
    "điều này",
    "hàm ý",
    "xu hướng",
)

# Long financial amounts are the backwards-compatible fact source for old
# reports.  Short counts are intentionally not guessed: they need an explicit
# expected_facts entry to avoid confusing dates, note numbers and quantities.
GOLD_NUMBER_RE = re.compile(r"(?<!\d)\(?-?(?:\d{1,3}(?:[.,]\d{3}){1,}|\d{4,})\)?(?!\d)")
_LABEL_RE = re.compile(
    r"(?im)^(?P<label>Entity|Metric|Period|Value|Unit|Reference|Table|Subheading|Item|"
    r"Note ref|Note number|Note title)\s*:\s*"
)


def _parse_labeled_context(text: str) -> dict[str, str]:
    matches = list(_LABEL_RE.finditer(str(text or "")))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        fields[match.group("label").casefold()] = text[match.end():end].strip()
    return fields


def _context_fact(text: str) -> dict[str, Any]:
    fields = _parse_labeled_context(text)
    subheading = fields.get("subheading", "")
    item = fields.get("item", "")
    metric = fields.get("metric", "") or " ".join(value for value in (subheading, item) if value)
    mapped = {
        "entity": fields.get("entity", ""),
        "metric": metric,
        "period": fields.get("period", ""),
        "value": fields.get("value", ""),
        "unit": fields.get("unit", ""),
        "reference": fields.get("reference", "") or fields.get("note ref", "") or fields.get("note number", ""),
    }
    normalized = normalize_fact(mapped)
    normalized["_raw"] = normalize_text(text)
    normalized["_raw_values"] = sorted(_values_in_text(text))
    normalized["_raw_periods"] = sorted(_periods_in_text(text))
    normalized["_raw_unit"] = _infer_unit(text)
    normalized["_raw_references"] = sorted(_references_in_text(text))
    return normalized


def facts_from_contexts(contexts: Iterable[Any]) -> list[dict[str, Any]]:
    output = []
    for context in contexts:
        if not str(context or "").strip():
            continue
        fact = _context_fact(str(context))
        raw_values = fact.get("_raw_values", []) or []
        if len(raw_values) <= 1:
            output.append(fact)
            continue
        # A compact context may encode several period/value columns.  Expand it
        # into value-specific facts so two expected values can both be matched,
        # while still requiring their other fields to coexist in this context.
        for value in raw_values:
            expanded = dict(fact)
            expanded["value"] = value
            output.append(expanded)
    return output


def facts_from_mappings(facts: Iterable[Any]) -> list[dict[str, Any]]:
    output = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        normalized = normalize_fact(fact)
        raw = " ".join(
            str(fact.get(field, "") or "")
            for field in (
                "entity",
                "company",
                "metric",
                "item_name",
                "subheading",
                "period",
                "time_hint",
                "value",
                "unit",
                "reference",
                "note_ref",
                "note_number",
                "evidence_text",
            )
        )
        normalized["_raw"] = normalize_text(raw)
        normalized["_raw_values"] = sorted(_values_in_text(raw))
        normalized["_raw_periods"] = sorted(_periods_in_text(raw))
        normalized["_raw_unit"] = _infer_unit(raw)
        normalized["_raw_references"] = sorted(_references_in_text(raw))
        raw_values = normalized.get("_raw_values", []) or []
        if len(raw_values) <= 1:
            output.append(normalized)
        else:
            for value in raw_values:
                expanded = dict(normalized)
                expanded["value"] = value
                output.append(expanded)
    return output


def _values_in_text(text: Any) -> set[str]:
    return {normalize_value(match.group(0)) for match in GOLD_NUMBER_RE.finditer(str(text or ""))}


def gold_numbers(text: str) -> set[str]:
    """Backwards-compatible alias returning normalized long numeric values."""
    return _values_in_text(text)


def _periods_in_text(text: Any) -> set[str]:
    plain = normalize_text(text)
    periods = {
        normalize_period(match.group(0))
        for match in _DATE_RE.finditer(plain)
        if 1 <= int(match.group("day")) <= 31 and 1 <= int(match.group("month")) <= 12
    }
    periods.update(normalize_period(match.group(0)) for match in _QUARTER_RE.finditer(plain))
    if re.search(r"\b(?:cuoi ky|cuoi nam|so cuoi)\b", plain):
        periods.add("ending")
    if re.search(r"\b(?:dau ky|dau nam|so dau)\b", plain):
        periods.add("beginning")
    if re.search(r"\b(?:nam hien tai|nam nay|current year)\b", plain):
        periods.add("current_year")
    if re.search(r"\b(?:nam truoc|previous year|prior year)\b", plain):
        periods.add("prior_year")
    return {period for period in periods if period}


def _references_in_text(text: Any) -> set[str]:
    plain = normalize_text(text)
    refs = re.findall(r"\b(?:thuyet minh|note|ref)\s*(?:so)?\s*([a-z]?\s*\d+[a-z]?)\b", plain)
    return {normalize_reference(ref) for ref in refs}


def _text_field_matches(expected: str, actual: str, raw: str) -> bool:
    if not expected:
        return True
    if actual and (expected == actual or expected in actual):
        return True
    return bool(raw and expected in raw)


def fact_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    expected_fact = normalize_fact(expected)
    actual_fact = normalize_fact(actual)
    raw = str(actual.get("_raw", "") or "")

    if not _text_field_matches(expected_fact["entity"], actual_fact["entity"], raw):
        return False
    if not _text_field_matches(expected_fact["metric"], actual_fact["metric"], raw):
        return False

    expected_value = expected_fact["value"]
    if expected_value:
        actual_values = set(actual.get("_raw_values", []) or [])
        if actual_fact["value"] != expected_value and expected_value not in actual_values:
            return False

    expected_period = expected_fact["period"]
    if expected_period:
        actual_periods = set(actual.get("_raw_periods", []) or [])
        if actual_fact["period"] != expected_period and expected_period not in actual_periods:
            return False

    expected_unit = expected_fact["unit"]
    if expected_unit:
        actual_units = {actual_fact["unit"], str(actual.get("_raw_unit", "") or "")}
        if expected_unit not in actual_units:
            return False

    expected_reference = expected_fact["reference"]
    if expected_reference:
        actual_references = set(actual.get("_raw_references", []) or [])
        if actual_fact["reference"] != expected_reference and expected_reference not in actual_references:
            return False
    return any(expected_fact[field] for field in FACT_FIELDS)


def is_analytical(question: str) -> bool:
    question_text = str(question or "").casefold()
    return any(marker in question_text for marker in ANALYTICAL_MARKERS)


def _explicit_expected_facts(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("expected_facts", record.get("factual_facts", []))
    if not isinstance(value, list):
        return []
    return [fact for fact in value if isinstance(fact, dict) and any(normalize_fact(fact).values())]


def expected_facts_for_record(record: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    explicit = _explicit_expected_facts(record)
    if explicit:
        return explicit, "explicit"
    ground_truth = str(record.get("ground_truth", "") or "")
    values = sorted(_values_in_text(ground_truth))
    derived_hints = set()
    for match in GOLD_NUMBER_RE.finditer(ground_truth):
        prefix = normalize_text(ground_truth[max(0, match.start() - 48):match.start()])
        if any(marker in prefix for marker in ("giam", "tang", "chenh lech", "thay doi")):
            derived_hints.add(normalize_value(match.group(0)))

    def derived_from_other_values(target: str) -> bool:
        if target not in derived_hints:
            return False
        try:
            target_value = Decimal(target)
            source_values = [Decimal(value) for value in values if value != target]
        except InvalidOperation:
            return False
        for index, first in enumerate(source_values):
            for second in source_values[index + 1:]:
                if target_value in {first + second, abs(first - second)}:
                    return True
        return False

    # A calculated change/total is not a source fact the retriever must return;
    # its component values remain in the denominator.
    legacy = [{"value": value} for value in values if not derived_from_other_values(value)]
    return legacy, "legacy_derived" if legacy else "none"


def _normalized_question_identity(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _numeric_record_id(value: Any, *, source: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{source} record id must be a positive integer")
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError(f"{source} record id must be a positive integer: {value!r}")
    return int(text)


def load_factual_contract_records(
    contract_path: str | Path,
    *,
    expected_dataset_id: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and validate the authoritative fixed factual-gate population.

    The returned records retain contract order and are marked as factual even
    when the wording contains analytical-looking verbs (for example, a request
    to compare two explicitly reviewed source facts).
    """

    path = Path(contract_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid factual-recall contract {path}: {exc}") from exc

    if isinstance(payload, list):
        metadata: dict[str, Any] = {}
        raw_records = payload
    elif isinstance(payload, dict):
        metadata = {key: value for key, value in payload.items() if key != "records"}
        raw_records = payload.get("records")
    else:
        raise ValueError("factual-recall contract must be an object with a records list")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("factual-recall contract must contain a non-empty records list")

    contract_dataset_id = str(metadata.get("dataset_id", "") or "").strip()
    if expected_dataset_id:
        if not contract_dataset_id:
            raise ValueError("factual-recall contract must define dataset_id")
        if contract_dataset_id != expected_dataset_id:
            raise ValueError(
                "factual-recall contract dataset mismatch: "
                f"expected {expected_dataset_id!r}, found {contract_dataset_id!r}"
            )

    records: list[dict[str, Any]] = []
    ids_seen: set[int] = set()
    identities_seen: set[tuple[int, str]] = set()
    for position, raw_record in enumerate(raw_records, start=1):
        if not isinstance(raw_record, dict):
            raise ValueError(f"factual-recall contract record {position} must be an object")
        record_id = _numeric_record_id(raw_record.get("id"), source="contract")
        question = _normalized_question_identity(raw_record.get("question"))
        if not question:
            raise ValueError(f"contract record {record_id} must define a non-empty question")
        if record_id in ids_seen:
            raise ValueError(f"duplicate factual-recall contract id: {record_id}")
        identity = (record_id, question)
        if identity in identities_seen:
            raise ValueError(f"duplicate factual-recall contract identity: {record_id}")

        facts = raw_record.get("expected_facts")
        if not isinstance(facts, list) or not facts:
            raise ValueError(f"contract record {record_id} must define non-empty expected_facts")
        reviewed_facts: list[dict[str, Any]] = []
        for fact_index, fact in enumerate(facts, start=1):
            if not isinstance(fact, dict) or any(
                fact.get(field) is None or not str(fact.get(field)).strip()
                for field in FACT_FIELDS
            ):
                raise ValueError(
                    f"contract record {record_id} fact {fact_index} must define all fields: "
                    f"{', '.join(FACT_FIELDS)}"
                )
            reviewed_facts.append(dict(fact))

        record = dict(raw_record)
        record.update(
            {
                "id": record_id,
                "question": question,
                "expected_facts": reviewed_facts,
                "facts_contract": str(path),
                OFFICIAL_CONTRACT_MARKER: True,
            }
        )
        # A legacy alias inside the contract must never compete with the
        # authoritative expected_facts list.
        record.pop("factual_facts", None)
        records.append(record)
        ids_seen.add(record_id)
        identities_seen.add(identity)

    metadata.update(
        {
            "path": str(path),
            "records_n": len(records),
            "expected_facts_n": sum(len(record["expected_facts"]) for record in records),
        }
    )
    return metadata, records


def _load_predictions_report(predictions_path: str | Path) -> dict[str, Any]:
    path = Path(predictions_path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid predictions report {path}: {exc}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("predictions"), list):
        raise ValueError("predictions report must be an object with a predictions list")
    return report


def enrich_contract_records_from_predictions(
    contract_records: Iterable[dict[str, Any]],
    predictions: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    """Merge optional report fields without changing the official denominator."""

    records = [dict(record) for record in contract_records]
    contract_by_id = {int(record["id"]): record for record in records}
    contract_id_by_question = {
        _normalized_question_identity(record["question"]): int(record["id"])
        for record in records
    }
    prediction_by_id: dict[int, dict[str, Any]] = {}
    ignored_n = 0
    for position, prediction in enumerate(predictions, start=1):
        if not isinstance(prediction, dict):
            raise ValueError(f"predictions report record {position} must be an object")
        prediction_id = _numeric_record_id(prediction.get("id"), source="prediction")
        if prediction_id in prediction_by_id:
            raise ValueError(f"duplicate prediction id: {prediction_id}")
        prediction_question = _normalized_question_identity(prediction.get("question"))
        if prediction_id in contract_by_id:
            expected_question = _normalized_question_identity(contract_by_id[prediction_id]["question"])
            if not prediction_question:
                raise ValueError(f"prediction {prediction_id} is missing its contract question")
            if prediction_question != expected_question:
                raise ValueError(
                    f"prediction question mismatch for contract id {prediction_id}: "
                    f"expected {expected_question!r}, found {prediction_question!r}"
                )
            prediction_by_id[prediction_id] = prediction
            continue

        # A reviewed question associated with another id is an identity error,
        # not an ignorable out-of-contract prediction.
        reviewed_id = contract_id_by_question.get(prediction_question)
        if reviewed_id is not None:
            raise ValueError(
                f"prediction identity mismatch: contract question for id {reviewed_id} "
                f"was reported as id {prediction_id}"
            )
        ignored_n += 1

    enriched: list[dict[str, Any]] = []
    for contract_record in records:
        record_id = int(contract_record["id"])
        prediction = prediction_by_id.get(record_id, {})
        merged = dict(prediction)
        # Contract fields deliberately win, including identity, expected facts,
        # and the official-factual marker used by the scorer.
        merged.update(contract_record)
        enriched.append(merged)

    matched_n = len(prediction_by_id)
    return enriched, {
        "provided": True,
        "matched_contract_records_n": matched_n,
        "contract_records_without_prediction_n": len(records) - matched_n,
        "ignored_out_of_contract_predictions_n": ignored_n,
    }


def matched_official_gate_records(
    contract_records: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only candidate/contract identity intersections in contract order.

    This is the in-memory adapter for diagnostic report readers.  Unrelated or
    malformed non-contract rows are ignored.  A candidate that claims a
    reviewed id must, however, carry the exact normalized-space contract
    question; otherwise the report is ambiguous and rejected.
    """

    records = [dict(record) for record in contract_records]
    contract_by_id = {int(record["id"]): record for record in records}
    matched_by_id: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            candidate_id = _numeric_record_id(candidate.get("id"), source="candidate")
        except ValueError:
            continue
        contract_record = contract_by_id.get(candidate_id)
        if contract_record is None:
            continue
        expected_question = _normalized_question_identity(contract_record.get("question"))
        candidate_question = _normalized_question_identity(candidate.get("question"))
        if candidate_question != expected_question:
            raise ValueError(
                f"candidate question mismatch for contract id {candidate_id}: "
                f"expected {expected_question!r}, found {candidate_question!r}"
            )
        if candidate_id in matched_by_id:
            raise ValueError(f"duplicate candidate id for factual-recall contract: {candidate_id}")
        merged = dict(candidate)
        merged.update(contract_record)
        matched_by_id[candidate_id] = merged
    return [
        matched_by_id[int(record["id"])]
        for record in records
        if int(record["id"]) in matched_by_id
    ]


def select_contract_records(
    records: Iterable[dict[str, Any]],
    ids_expression: str = "",
) -> list[dict[str, Any]]:
    """Select a contract subset while rejecting malformed or unknown ids."""

    records = [dict(record) for record in records]
    if not str(ids_expression or "").strip():
        return records
    raw_parts = str(ids_expression).split(",")
    if any(not part.strip() for part in raw_parts):
        raise ValueError("--ids must be a comma-separated list of positive integers")
    requested = [
        _numeric_record_id(part.strip(), source="requested")
        for part in raw_parts
    ]
    if len(set(requested)) != len(requested):
        raise ValueError("--ids contains duplicate ids")
    available = {int(record["id"]) for record in records}
    missing = sorted(set(requested) - available)
    if missing:
        raise ValueError(
            "--ids contains ids absent from the factual-recall contract: "
            + ", ".join(str(record_id) for record_id in missing)
        )
    requested_set = set(requested)
    return [record for record in records if int(record["id"]) in requested_set]


def prepare_official_gate_records(
    contract_path: str | Path,
    *,
    predictions_path: str | Path = "",
    ids_expression: str = "",
    expected_dataset_id: str = "",
) -> dict[str, Any]:
    """Prepare official records from the contract, optionally enriched by a report."""

    contract_metadata, records = load_factual_contract_records(
        contract_path,
        expected_dataset_id=expected_dataset_id,
    )
    report: dict[str, Any] = {"metadata": {}, "predictions": []}
    enrichment: dict[str, int | bool] = {
        "provided": False,
        "matched_contract_records_n": 0,
        "contract_records_without_prediction_n": len(records),
        "ignored_out_of_contract_predictions_n": 0,
    }
    if str(predictions_path or "").strip():
        report = _load_predictions_report(predictions_path)
        records, enrichment = enrich_contract_records_from_predictions(
            records,
            report["predictions"],
        )
    selected = select_contract_records(records, ids_expression)
    return {
        "records": selected,
        "report": report,
        "contract_metadata": contract_metadata,
        "prediction_enrichment": enrichment,
    }


def attach_expected_facts_from_seed(
    records: Iterable[dict[str, Any]],
    *,
    report: dict[str, Any],
    report_path: str | Path,
) -> list[dict[str, Any]]:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    seed_file = str(metadata.get("seed_file", "") or "dau_tu_APEC_ragas_seed.json")
    seed_path = Path(seed_file)
    if not seed_path.is_absolute():
        repository_candidate = Path(__file__).resolve().parent / seed_path
        report_candidate = Path(report_path).resolve().parent / seed_path
        seed_path = repository_candidate if repository_candidate.exists() else report_candidate
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(record) for record in records]
    if not isinstance(seed, list):
        return [dict(record) for record in records]

    def identity(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("id", "") or "").strip(),
            " ".join(str(item.get("question", "") or "").split()).strip(),
        )

    facts_by_identity = {
        identity(item): item.get("expected_facts", item.get("factual_facts"))
        for item in seed
        if isinstance(item, dict)
        and isinstance(item.get("expected_facts", item.get("factual_facts")), list)
    }
    output = []
    for record in records:
        item = dict(record)
        facts = facts_by_identity.get(identity(item))
        if facts is not None and "expected_facts" not in item:
            item["expected_facts"] = facts
        output.append(item)
    return output


def attach_expected_facts_from_contract(
    records: Iterable[dict[str, Any]],
    contract_path: str | Path,
) -> list[dict[str, Any]]:
    """Compatibility overlay by stable id + question identity.

    Unlike :func:`prepare_official_gate_records`, unmatched input records stay
    in the result so historical report readers retain their old behavior.
    """

    path = Path(contract_path)
    _metadata, contract_records = load_factual_contract_records(path)

    def identity(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("id", "") or "").strip(),
            _normalized_question_identity(item.get("question")),
        )

    reviewed = {
        identity(item): item["expected_facts"]
        for item in contract_records
    }

    output = []
    for record in records:
        item = dict(record)
        facts = reviewed.get(identity(item))
        if facts is not None:
            item["expected_facts"] = [dict(fact) for fact in facts]
            item["facts_contract"] = str(path)
        output.append(item)
    return output


def score_factual_record(
    record: dict[str, Any],
    *,
    actual_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expected, source = expected_facts_for_record(record)
    if actual_facts is None:
        mapped = record.get("retrieved_facts", [])
        actual_facts = facts_from_mappings(mapped) if isinstance(mapped, list) and mapped else facts_from_contexts(
            record.get("retrieved_contexts", []) or []
        )

    unmatched_actual = list(range(len(actual_facts)))
    matched = []
    missing = []
    for expected_fact in expected:
        match_index = next(
            (index for index in unmatched_actual if fact_matches(expected_fact, actual_facts[index])),
            None,
        )
        normalized_expected = normalize_fact(expected_fact)
        if match_index is None:
            missing.append(normalized_expected)
        else:
            matched.append(normalized_expected)
            unmatched_actual.remove(match_index)

    recall = len(matched) / len(expected) if expected else None
    return {
        "id": record.get("id"),
        "question": str(record.get("question", "") or ""),
        "bucket": (
            "factual"
            if record.get(OFFICIAL_CONTRACT_MARKER) is True
            else "analytical" if is_analytical(record.get("question", "")) else "factual"
        ),
        "official_contract": record.get(OFFICIAL_CONTRACT_MARKER) is True,
        "fact_source": source,
        "expected_n": len(expected),
        "matched_n": len(matched),
        "recall": recall,
        "missing_facts": missing,
    }


def aggregate_factual_rows(
    rows: Iterable[dict[str, Any]],
    *,
    threshold: float = FACTUAL_RECALL_THRESHOLD,
) -> dict[str, Any]:
    if not FACTUAL_RECALL_THRESHOLD <= threshold <= 1:
        raise ValueError("deterministic factual-recall threshold must be between 0.95 and 1")
    rows = [row for row in rows if isinstance(row, dict)]
    gate_rows = [row for row in rows if row["bucket"] == "factual" and row["expected_n"]]
    expected_n = sum(row["expected_n"] for row in gate_rows)
    matched_n = sum(row["matched_n"] for row in gate_rows)
    recall = matched_n / expected_n if expected_n else None
    unscored_factual_n = sum(
        1 for row in rows if row["bucket"] == "factual" and not row["expected_n"]
    )
    if recall is None:
        status = "not_evaluated"
    else:
        status = "pass" if recall >= threshold else "fail"
    return {
        "name": "deterministic_factual_recall",
        "threshold": float(threshold),
        "comparison": ">=",
        "status": status,
        "recall": round(recall, 6) if recall is not None else None,
        "matched_facts_n": matched_n,
        "expected_facts_n": expected_n,
        "scored_factual_records_n": len(gate_rows),
        "unscored_factual_records_n": unscored_factual_n,
        "explicit_records_n": sum(row["fact_source"] == "explicit" for row in gate_rows),
        "legacy_derived_records_n": sum(row["fact_source"] == "legacy_derived" for row in gate_rows),
        "rows": rows,
        "ragas_role": "diagnostic_only",
    }


def evaluate_factual_recall(
    records: Iterable[dict[str, Any]],
    *,
    threshold: float = FACTUAL_RECALL_THRESHOLD,
) -> dict[str, Any]:
    rows = [score_factual_record(record) for record in records if isinstance(record, dict)]
    return aggregate_factual_rows(rows, threshold=threshold)


def atomic_write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    return _atomic_write_json(output_path, payload)


def facts_fingerprint(facts: Iterable[dict[str, Any]]) -> str:
    normalized = [normalize_fact(fact) for fact in facts]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def git_worktree_provenance(repository: str | Path | None = None) -> dict[str, Any]:
    """Fingerprint HEAD plus tracked and untracked worktree content.

    ``git_revision`` alone is insufficient when a benchmark is run before its
    code is committed.  This hash never serializes diff contents into the
    report; it records only deterministic digests.
    """

    root = Path(repository or Path(__file__).resolve().parent).resolve()

    def run_git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout

    try:
        revision = run_git("rev-parse", "HEAD").decode("utf-8", errors="replace").strip()
        status = run_git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        tracked_diff = run_git("diff", "--binary", "HEAD", "--", ".")
        untracked_output = run_git("ls-files", "--others", "--exclude-standard", "-z")
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "worktree_dirty": None,
            "git_revision": "",
            "worktree_diff_sha256": None,
            "code_sha256": None,
            "untracked_files_n": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    untracked_paths = sorted(
        os.fsdecode(raw_path)
        for raw_path in untracked_output.split(b"\0")
        if raw_path
    )
    diff_digest = hashlib.sha256()
    diff_digest.update(b"agentfinx-worktree-diff-v1\0")
    diff_digest.update(tracked_diff)
    for relative_name in untracked_paths:
        diff_digest.update(b"\0untracked\0")
        diff_digest.update(relative_name.encode("utf-8", errors="surrogateescape"))
        candidate = root / relative_name
        try:
            if candidate.is_symlink():
                diff_digest.update(b"\0symlink\0")
                diff_digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
            elif candidate.is_file():
                diff_digest.update(b"\0file\0")
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        diff_digest.update(chunk)
            else:
                diff_digest.update(b"\0missing-or-non-file\0")
        except OSError as exc:
            diff_digest.update(f"\0read-error:{type(exc).__name__}\0".encode("ascii"))

    worktree_diff_sha256 = diff_digest.hexdigest()
    code_sha256 = stable_json_fingerprint(
        {
            "scheme": "git-head-plus-worktree-diff-v1",
            "git_revision": revision,
            "worktree_diff_sha256": worktree_diff_sha256,
        }
    )
    return {
        "worktree_dirty": bool(status),
        "git_revision": revision,
        "worktree_diff_sha256": worktree_diff_sha256,
        "code_sha256": code_sha256,
        "untracked_files_n": len(untracked_paths),
        "error": "",
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--predictions-file",
        default="",
        help=(
            "Optional report JSON used only to enrich matching contract records; "
            "it cannot add identities or expected facts to the official gate."
        ),
    )
    parser.add_argument("--dataset-id", default="apec")
    parser.add_argument(
        "--facts-contract",
        default=DEFAULT_FACTS_CONTRACT,
        help="Version-controlled explicit entity/metric/period/value/unit/reference fixture.",
    )
    parser.add_argument("--ids", default="", help="Comma-separated id subset; default all.")
    parser.add_argument("--json-out", default="", help="Optional path for the JSON summary.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=FACTUAL_RECALL_THRESHOLD,
        help="Hard-gate threshold; may be raised but never set below 0.95.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not FACTUAL_RECALL_THRESHOLD <= args.threshold <= 1:
        raise SystemExit("--threshold must be between 0.95 and 1")
    if not str(args.facts_contract or "").strip():
        raise SystemExit("--facts-contract is required for the official retrieval gate")

    facts_contract_path = Path(args.facts_contract)
    try:
        prepared = prepare_official_gate_records(
            facts_contract_path,
            predictions_path=args.predictions_file,
            ids_expression=args.ids,
            expected_dataset_id=args.dataset_id,
        )
    except ValueError as exc:
        raise SystemExit(f"Cannot prepare deterministic factual-recall gate: {exc}") from exc
    records = prepared["records"]
    report = prepared["report"]
    contract_metadata = prepared["contract_metadata"]
    prediction_enrichment = prepared["prediction_enrichment"]

    from dataset_catalog.registry import get_dataset
    from test import ensure_built
    from config.allowed_keywords import TABLE_BS, TABLE_CF, TABLE_IS, TABLE_NOTE
    import graph.evidence as graph_evidence
    from tools.evidence import result_to_facts
    from tools.tools import get_related_info

    dataset = get_dataset(args.dataset_id)
    if dataset is None:
        raise SystemExit(f"Dataset not found: {args.dataset_id}")
    dataset, _conn, collection = ensure_built(dataset)

    tables = [TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE]
    rows = []
    for record in records:
        record_id = int(record.get("id", 0))
        question = str(record.get("question", "") or "")

        state = {"user_query": question}
        surviving_facts = []
        for table in tables:
            facts_limit = graph_evidence._facts_limit_for_table(state, {}, table)
            retrieval_limit = (
                max(graph_evidence.NOTE_REF_FACTS_SCAN_LIMIT, facts_limit)
                if table == TABLE_NOTE
                else facts_limit
            )
            raw_result = get_related_info(
                query=question,
                table=table,
                collection=collection,
                strict_table=(table == TABLE_NOTE),
                limit=retrieval_limit,
                intent=question,
            )
            facts = result_to_facts(raw_result, table=table, query=question, limit=retrieval_limit)
            facts = graph_evidence._limit_evidence_facts_for_table(
                table, facts, state=state, worker_plan={}
            )
            surviving_facts.extend(facts)

        actual_facts = facts_from_mappings(surviving_facts)
        row = score_factual_record(record, actual_facts=actual_facts)
        if row["fact_source"] != "explicit" or not row["official_contract"]:
            raise RuntimeError(
                f"official gate record {record_id} lost its explicit contract facts"
            )
        row.update(
            {
                "facts_n": len(surviving_facts),
                "retrieved_facts": [
                    {
                        key: fact.get(key, "")
                        for key in (
                            "company",
                            "table",
                            "item_name",
                            "subheading",
                            "time_hint",
                            "value_type",
                            "value",
                            "unit",
                            "note_ref",
                            "reference",
                            "source",
                            "status",
                        )
                        if fact.get(key, "") not in ("", None)
                    }
                    for fact in surviving_facts
                    if isinstance(fact, dict)
                ],
                "gold_bearing_facts_n": sum(
                    any(fact_matches(expected, actual) for expected in expected_facts_for_record(record)[0])
                    for actual in actual_facts
                ),
            }
        )
        rows.append(row)
        recall_text = f"{row['recall']:.2f}" if row["recall"] is not None else "n/a"
        print(
            f"id {record_id} | facts {row['matched_n']}/{row['expected_n']} recall={recall_text}"
            f" | retrieved={len(surviving_facts)} source={row['fact_source']}",
            flush=True,
        )

    # Rows already contain production retrieval output; aggregate them directly
    # instead of re-reading contexts from the input report.
    gate = aggregate_factual_rows(rows, threshold=args.threshold)
    gate_rows = [row for row in rows if row["bucket"] == "factual" and row["expected_n"]]
    status = gate["status"]
    summary = {
        **{key: value for key, value in gate.items() if key != "rows"},
        "ids_n": len(rows),
        "official_contract_records_n": len(records),
        "official_contract_expected_facts_n": sum(
            len(record["expected_facts"]) for record in records
        ),
        "total_facts": sum(row["facts_n"] for row in rows),
        "avg_facts_per_question": round(
            sum(row["facts_n"] for row in rows) / len(rows), 2
        ) if rows else 0,
        # Kept only as a diagnostic for readers of schema v1.
        "macro_recall": round(
            sum(row["recall"] for row in gate_rows) / len(gate_rows), 6
        ) if gate_rows else None,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    if args.json_out:
        from ingestion.pipeline import PARSER_CONTRACT_VERSION
        from kb.sqlite_repo import read_kb_manifest
        from llm.client import get_llm_identity
        from vectorstore.qdrant_store import (
            EMBEDDING_DOCUMENT_INSTRUCTION,
            EMBEDDING_MODEL,
            QDRANT_VECTOR_SIZE,
        )

        input_report_path = Path(args.predictions_file) if args.predictions_file else None
        seed_name = str(
            (report.get("metadata", {}) or {}).get("seed_file", "")
            or contract_metadata.get("seed_file", "")
            or ""
        )
        seed_path = Path(seed_name) if seed_name else None
        if seed_path is not None and not seed_path.is_absolute():
            repository_seed = Path(__file__).resolve().parent / seed_path
            report_seed = (
                input_report_path.resolve().parent / seed_path
                if input_report_path is not None
                else repository_seed
            )
            seed_path = repository_seed if repository_seed.exists() else report_seed
        model_identity = get_llm_identity()
        index_fingerprint = str(
            getattr(collection, "build_fingerprint", "")
            or getattr(collection, "generation", "")
            or getattr(collection, "qdrant_name", "")
            or ""
        )
        worktree = git_worktree_provenance()
        metadata = {
            "run_kind": "retrieval_quality_gate",
            "dataset_id": args.dataset_id,
            "git_revision": worktree["git_revision"],
            "worktree_dirty": worktree["worktree_dirty"],
            "code_fingerprint_kind": "git-head-plus-worktree-diff-v1",
            "contract": contract_metadata,
            "prediction_enrichment": prediction_enrichment,
            "fingerprints": {
                "input_report_sha256": (
                    sha256_file(input_report_path) if input_report_path is not None else None
                ),
                "seed_sha256": (
                    sha256_file(seed_path) if seed_path is not None and seed_path.is_file() else None
                ),
                "facts_contract_sha256": sha256_file(facts_contract_path),
                "expected_facts_sha256": facts_fingerprint(
                    fact
                    for record in records
                    for fact in expected_facts_for_record(record)[0]
                ),
                "worktree_diff_sha256": worktree["worktree_diff_sha256"],
                "code_sha256": worktree["code_sha256"],
                "dataset_sha256": stable_json_fingerprint(
                    dataset.model_dump(mode="json") if hasattr(dataset, "model_dump") else dataset
                ),
                "kb_sha256": stable_json_fingerprint(read_kb_manifest(_conn)),
                "index_sha256": index_fingerprint,
                "model_sha256": stable_json_fingerprint(model_identity),
                "embedding_sha256": stable_json_fingerprint(
                    {
                        "model": EMBEDDING_MODEL,
                        "document_instruction": EMBEDDING_DOCUMENT_INSTRUCTION,
                        "vector_size": QDRANT_VECTOR_SIZE,
                    }
                ),
                "prompt_sha256": stable_json_fingerprint(
                    {"prompt": "not_used_by_deterministic_retrieval_gate"}
                ),
                "config_sha256": stable_json_fingerprint(
                    {
                        "threshold": args.threshold,
                        "parser_contract": PARSER_CONTRACT_VERSION,
                        "tables": tables,
                        "selected_ids": [record["id"] for record in records],
                    }
                ),
            },
            "worktree_provenance": {
                "untracked_files_n": worktree["untracked_files_n"],
                "error": worktree["error"],
            },
            "provider_limit_status": {
                "observed_in_gate_errors": False,
                "limit_free_environment_attested": False,
                "latency_eligible": False,
                "reason": "quality gate run; no clean latency attestation",
            },
        }
        atomic_write_json(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "metadata": metadata,
                "summary": summary,
                "rows": rows,
            },
            args.json_out,
        )
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
