"""Shared helpers for normalizing retrieval requirements and fact status."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal

from config.allowed_keywords import ALLOWED_KEYWORDS, normalize_keyword_synonyms
from common import dedupe_keep_order as _dedupe_keep_order


FACT_STATUS_FOUND = "found"
FACT_STATUS_NOT_FOUND = "not_found_after_search"
FACT_STATUS_AMBIGUOUS = "ambiguous"
VALID_FACT_STATUSES = {
    FACT_STATUS_FOUND,
    FACT_STATUS_NOT_FOUND,
    FACT_STATUS_AMBIGUOUS,
}
USABLE_FACT_STATUSES = {"", FACT_STATUS_FOUND}

REQUIREMENT_MATCHED = "matched"
REQUIREMENT_UNMATCHED_TOPK = "unmatched_topk"
REQUIREMENT_AMBIGUOUS = "ambiguous"
REQUIREMENT_EXHAUSTIVE_ABSENT = "exhaustive_absent"
RequirementEvidenceState = Literal[
    "matched",
    "unmatched_topk",
    "ambiguous",
    "exhaustive_absent",
]


def normalize_fact_status(value: Any) -> str:
    # Missing status is the legacy representation of a successfully retrieved
    # fact. An explicit but unsupported status is not evidence of success: keep
    # it out of factual answers by degrading it to ``ambiguous``.
    if value is None or not str(value).strip():
        return FACT_STATUS_FOUND
    text = str(value).strip().lower()
    if text in VALID_FACT_STATUSES:
        return text
    return FACT_STATUS_AMBIGUOUS


def is_usable_fact_status(value: Any) -> bool:
    return normalize_fact_status(value) in USABLE_FACT_STATUSES


def _collapse(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _ascii(value: Any) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.lower().split())


_SEMANTIC_NOISE_TOKENS = {
    "ai",
    "bao",
    "bao nhieu",
    "bang",
    "can",
    "cho",
    "cong ty",
    "cua",
    "danh gia",
    "du lieu",
    "dua",
    "duoc",
    "gi",
    "la",
    "muc do",
    "nam",
    "nay",
    "phan tich",
    "so lieu",
    "tai",
    "theo",
    "thong tin",
    "trong",
    "tren",
    "voi",
}
_SEMANTIC_NOISE_WORDS = {
    token
    for phrase in _SEMANTIC_NOISE_TOKENS
    for token in phrase.split()
}
_SEMANTIC_ANALYSIS_WORDS = {
    "anh",
    "dang",
    "dieu",
    "dong",
    "hieu",
    "hoat",
    "huong",
    "ke",
    "muc",
    "nghia",
    "phan",
    "quan",
    "trong",
    "trong",
    "yeu",
}


def _semantic_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _ascii(value))
        if token not in _SEMANTIC_NOISE_WORDS
        and token not in _SEMANTIC_ANALYSIS_WORDS
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def _fact_semantic_text(fact: dict) -> str:
    return " ".join(
        str(fact.get(key, "") or "")
        for key in (
            "item_name",
            "subheading",
            "row_label",
            "column_label",
            "metric_label",
            "entity_label",
            "scope_label",
            "counterparty",
            "transaction_type",
            "movement_type",
            "geography",
            "policy_topic",
            "section_key",
            "section_path",
            "evidence_text",
        )
    )


def _directional_signature(value: Any) -> dict[str, str]:
    """Return mutually exclusive semantic directions named by the text."""

    text = _ascii(value)
    lending = any(
        marker in text
        for marker in ("cho vay", "phai thu ve cho vay", "lai cho vay")
    )
    borrowing = bool(re.search(r"\bvay\b", text)) and not lending
    signature = {}
    if lending:
        signature["loan_direction"] = "lending"
    elif borrowing:
        signature["loan_direction"] = "borrowing"

    if "phai thu" in text:
        signature["balance_direction"] = "receivable"
    elif "phai tra" in text:
        signature["balance_direction"] = "payable"

    if any(marker in text for marker in ("tien thu", "thu tu", "thu hoi")):
        signature["cash_direction"] = "inflow"
    elif any(marker in text for marker in ("tien chi", "chi tra", "da tra")):
        signature["cash_direction"] = "outflow"

    if any(marker in text for marker in ("mua hang", "mua dich vu", "giao dich mua")):
        signature["transaction_direction"] = "purchase"
    elif any(marker in text for marker in ("ban hang", "giao dich ban", "doanh thu voi")):
        signature["transaction_direction"] = "sale"

    if "phai nop" in text:
        signature["tax_movement"] = "accrued"
    elif "da nop" in text:
        signature["tax_movement"] = "paid"

    if "chi phi thue" in text:
        signature["tax_scope"] = "expense"
    elif any(
        marker in text
        for marker in (
            "tai san thue",
            "thue thu nhap hoan lai phai tra",
            "so du thue",
        )
    ):
        signature["tax_scope"] = "balance"
    return signature


def _has_directional_conflict(requirement: Any, fact_text: Any) -> bool:
    required = _directional_signature(requirement)
    actual = _directional_signature(fact_text)
    return any(
        key in actual and actual[key] != value
        for key, value in required.items()
    )


def _strip_requirement_noise(text: str) -> str:
    cleaned = _collapse(text).strip(" .;,-:")
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"^(cần|thiếu|bổ sung|lấy|truy xuất|tìm|kiểm tra)\s+"
        r"((dữ liệu|số liệu|thông tin|chi tiết|dòng|khoản mục)\s+)?"
        r"((về|cho|của|liên quan đến)\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    cleaned = re.sub(
        r"\s+để\s+(tính|đánh giá|phân tích|trả lời|xác định|kiểm tra)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    cleaned = re.sub(
        r"\b(cho năm|trong năm|tại năm|năm)\s+\d{4}\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    return " ".join(cleaned.split())


def _keyword_candidates(table: str = "") -> list[str]:
    table_name = str(table or "").strip()
    if table_name and table_name in ALLOWED_KEYWORDS:
        tables = [table_name]
    else:
        tables = list(ALLOWED_KEYWORDS.keys())

    candidates = []
    seen = set()
    for table_key in tables:
        for item in sorted(ALLOWED_KEYWORDS.get(table_key, set()) or set(), key=len, reverse=True):
            text = _collapse(item)
            if text and text not in seen:
                candidates.append(text)
                seen.add(text)
    return candidates


def normalize_requirement_text(value: Any, table: str = "") -> str:
    text = normalize_keyword_synonyms(_strip_requirement_noise(str(value or "")))
    if not text:
        return ""

    candidates = []
    for keyword in _keyword_candidates(table):
        if text == keyword:
            return keyword
        if keyword in text or text in keyword:
            candidates.append(keyword)

    candidates = _dedupe_keep_order(candidates)
    if len(candidates) == 1:
        return candidates[0]
    return text

def normalize_requirements_keep_order(
    items: Any,
    *,
    table: str = "",
    limit: int = 0,
) -> list[str]:
    if items is None:
        values = []
    elif isinstance(items, (list, tuple, set)):
        values = list(items)
    else:
        values = [items]

    normalized = _dedupe_keep_order(
        normalize_requirement_text(item, table=table)
        for item in values
    )
    if limit > 0:
        return normalized[:limit]
    return normalized


def extract_financial_statement_keywords(
    value: Any,
    *,
    table: str = "",
    limit: int = 3,
) -> list[str]:
    text = _strip_requirement_noise(str(value or ""))
    if not text:
        return []

    matches = []
    for keyword in _keyword_candidates(table):
        if keyword and (keyword in text or text in keyword):
            matches.append(keyword)

    matches = _dedupe_keep_order(matches)
    if matches:
        return matches[:limit] if limit > 0 else matches

    normalized = normalize_requirement_text(text, table=table)
    if normalized and normalized != text:
        return [normalized]
    return []


def requirement_name_matches_fact(requirement: Any, fact: dict, *, table: str = "") -> bool:
    if not isinstance(fact, dict):
        return False

    fact_table = str(fact.get("table", "") or table or "").strip()
    raw_requirement = _strip_requirement_noise(str(requirement or ""))
    canonical_requirement = normalize_requirement_text(
        raw_requirement,
        table=fact_table,
    )
    requirement_text = normalize_keyword_synonyms(
        canonical_requirement or raw_requirement
    )
    fact_text = _fact_semantic_text(fact)
    if not requirement_text or not fact_text.strip():
        return False

    if _has_directional_conflict(requirement_text, fact_text):
        return False

    # Typed fields are authoritative when present.  Keep the import local so
    # schema normalization remains usable in lightweight contexts.
    try:
        from tools.query_routing import (
            fact_matches_required_slots,
            parse_query_slots,
        )

        slots = parse_query_slots(raw_requirement)
        typed_meta = dict(fact)
        if not typed_meta.get("heading") and fact_table:
            typed_meta["heading"] = fact_table
        has_typed_metadata = any(
            str(fact.get(key, "") or "").strip()
            for key in (
                "row_label",
                "column_label",
                "metric_label",
                "entity_label",
                "scope_label",
                "counterparty",
                "transaction_type",
                "movement_type",
                "geography",
                "policy_topic",
                "period",
                "period_role",
                "value_type",
                "aggregation_level",
                "section_key",
            )
        )
        has_required_slots = bool(
            slots.metric
            or slots.entity
            or slots.period in {"cuối", "đầu", "both"}
            or slots.period_role in {"current", "previous", "both"}
            or slots.period_labels
            or slots.value_type
            or slots.aggregation in {"total", "component"}
            or slots.scope_label
            or slots.counterparty
            or slots.transaction_type
            or slots.movement_type
            or slots.geography
            or slots.policy_topic
            or slots.section_key
        )
        typed_slots_match = bool(
            has_typed_metadata
            and has_required_slots
            and fact_matches_required_slots(slots, typed_meta, fact_text)
        )
        if has_typed_metadata and has_required_slots and not typed_slots_match:
            return False
        # Comparison wording contains operation/period tokens that are not part
        # of a row label.  Once a typed metric and every explicit temporal slot
        # match, those prose tokens must not veto an otherwise exact leg.
        if (
            typed_slots_match
            and slots.metric
            and (
                slots.period == "both"
                or slots.period_role == "both"
                or len(slots.period_labels) > 1
            )
        ):
            return True
        # A sufficiently discriminative typed contract is itself the semantic
        # match.  Do not make it pass a second bag-of-words gate merely because
        # the user inserted paraphrase modifiers around the canonical metric.
        # Generic one/two-token metrics still fall through to strict lexical
        # coverage so "doanh thu năm nay" cannot close on any revenue subtype.
        typed_discriminant = bool(
            len(_semantic_tokens(slots.metric)) >= 3
            or slots.entity
            or slots.period_labels
            or slots.value_type
            or slots.aggregation in {"total", "component"}
            or slots.scope_label
            or slots.counterparty
            or slots.transaction_type
            or slots.movement_type
            or slots.geography
            or slots.policy_topic
            or slots.section_key
        )
        if typed_slots_match and typed_discriminant:
            return True
    except (ImportError, AttributeError):
        # Legacy/lightweight callers still receive the strict lexical contract.
        pass

    fact_tokens = _semantic_tokens(fact_text)
    # Alias normalization can intentionally expand a short label (for example
    # ``doanh thu thuần``) into the full canonical statement caption.  Requiring
    # every token from that expansion would reject a fact whose original label
    # exactly matches the requirement.  Compare both representations and accept
    # the stricter successful one; neither path uses substring containment.
    requirement_token_sets = [
        tokens
        for tokens in (
            _semantic_tokens(raw_requirement),
            _semantic_tokens(canonical_requirement),
            _semantic_tokens(requirement_text),
        )
        if tokens
    ]
    if not requirement_token_sets or not fact_tokens:
        return False
    for requirement_tokens in requirement_token_sets:
        matched = requirement_tokens & fact_tokens
        coverage = len(matched) / len(requirement_tokens)
        # No substring closure: every informative token of a short requirement
        # must match; longer analytical wording may contain one residual
        # non-fact token.
        required_coverage = 1.0 if len(requirement_tokens) <= 3 else 0.80
        if coverage >= required_coverage:
            return True
    return False


def requirement_matches_fact(requirement: Any, fact: dict, *, table: str = "") -> bool:
    if not requirement_name_matches_fact(requirement, fact, table=table):
        return False
    if not is_usable_fact_status(fact.get("status", FACT_STATUS_FOUND)):
        return False
    return fact.get("value", "") not in ("", None)


_CLOSED_COVERAGE_LEGS = {"components_closed", "transaction_categories"}


def _coverage_scope_key(fact: dict) -> tuple[str, ...] | None:
    """Return the parser-owned scope in which evidence legs may be combined."""

    block_id = _collapse(fact.get("block_id", ""))
    if not block_id:
        return None
    return (
        _collapse(fact.get("company", "")),
        _collapse(fact.get("fiscal_year", "")),
        _collapse(fact.get("index_generation", "")),
        _collapse(fact.get("table", "") or fact.get("heading", "")),
        _collapse(fact.get("source", "")),
        block_id,
    )


def _string_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        values = value
    elif value in ("", None):
        values = ()
    else:
        values = re.split(r"[,;|]", str(value))
    return {
        _collapse(item)
        for item in values
        if _collapse(item)
    }


def _coverage_requirement_is_complete(
    slots: Any,
    *,
    matched_facts: list[dict],
    usable_facts: list[dict],
) -> bool:
    """Require every declared leg in one compatible block before closure."""

    required_legs = {
        _collapse(leg)
        for leg in getattr(slots, "required_legs", ()) or ()
        if _collapse(leg)
    }
    if not required_legs:
        return True

    anchor_scopes = {
        scope
        for scope in (
            _coverage_scope_key(fact)
            for fact in matched_facts
        )
        if scope is not None
    }
    if not anchor_scopes:
        return False

    try:
        from tools.query_routing import coverage_legs_for_fact
    except (ImportError, AttributeError):
        return False

    for scope in anchor_scopes:
        scoped_facts = [
            fact
            for fact in usable_facts
            if _coverage_scope_key(fact) == scope
        ]
        if not scoped_facts or _matching_typed_facts_conflict(scoped_facts):
            continue

        observed_legs: set[str] = set()
        complete_closed_legs: set[str] = set()
        for fact in scoped_facts:
            observed_legs.update(
                _collapse(leg)
                for leg in coverage_legs_for_fact(
                    slots,
                    fact,
                    str(fact.get("evidence_text", "") or ""),
                )
                if _collapse(leg)
            )
            complete_closed_legs.update(
                _string_set(fact.get("coverage_complete_legs"))
            )

        if all(
            (
                leg in observed_legs
                and (
                    leg not in _CLOSED_COVERAGE_LEGS
                    or leg in complete_closed_legs
                )
            )
            for leg in required_legs
        ):
            return True
    return False


def _multi_period_requirement_is_complete(
    slots: Any,
    *,
    matched_facts: list[dict],
    usable_facts: list[dict],
) -> bool:
    """Require both requested temporal legs in one logical parser block."""

    required_positions = (
        {"cuối", "đầu"}
        if getattr(slots, "period", "") == "both"
        else set()
    )
    required_roles = (
        {"current", "previous"}
        if getattr(slots, "period_role", "") == "both"
        else set()
    )
    required_labels = (
        set(getattr(slots, "period_labels", ()) or ())
        if len(getattr(slots, "period_labels", ()) or ()) > 1
        else set()
    )
    if not required_positions and not required_roles and not required_labels:
        return True

    try:
        from ingestion.period_normalize import canonical_period
        from tools.query_routing import (
            fact_matches_required_slots,
            fact_period_labels,
            fact_period_role,
            fact_sibling_group_key,
        )
    except (ImportError, AttributeError):
        return False

    anchor_keys = {
        fact_sibling_group_key(fact)
        for fact in matched_facts
        if isinstance(fact, dict)
    }
    if not anchor_keys:
        return False

    observed_by_key: dict[tuple[str, ...], dict[str, set[str]]] = {}
    for fact in usable_facts:
        if not isinstance(fact, dict):
            continue
        key = fact_sibling_group_key(fact)
        if key not in anchor_keys:
            continue
        if not fact_matches_required_slots(
            slots,
            fact,
            _fact_semantic_text(fact),
        ):
            continue
        observed = observed_by_key.setdefault(
            key,
            {"positions": set(), "roles": set()},
        )
        position = str(fact.get("period", "") or "").strip() or canonical_period(
            " ".join(
                str(fact.get(field, "") or "")
                for field in ("period_label", "column_label", "item_name")
            )
        )
        role = fact_period_role(
            fact,
            str(fact.get("evidence_text", "") or ""),
        )
        labels = fact_period_labels(
            fact,
            str(fact.get("evidence_text", "") or ""),
        )
        if position in {"cuối", "đầu"}:
            observed["positions"].add(position)
        if role in {"current", "previous"}:
            observed["roles"].add(role)
        observed.setdefault("labels", set()).update(labels)

    return any(
        required_positions.issubset(observed["positions"])
        and required_roles.issubset(observed["roles"])
        and required_labels.issubset(observed.get("labels", set()))
        for observed in observed_by_key.values()
    )


def requirement_evidence_state(
    requirement: Any,
    facts: Iterable[dict] | None,
    *,
    table: str = "",
) -> RequirementEvidenceState:
    """Classify whether retrieved evidence actually closes a requirement.

    A top-k ``not_found_after_search`` placeholder is not proof that a fact is
    absent from the corpus.  Only a result explicitly marked as an exhaustive
    scan may close the requirement as ``exhaustive_absent``; otherwise the
    caller should perform its bounded targeted retry.
    """

    requirement_text = str(requirement or "").strip()
    if not requirement_text:
        return REQUIREMENT_MATCHED

    slots = None
    coverage_required = False
    fact_matches_required_slots = None
    try:
        from tools.query_routing import (
            fact_matches_required_slots as typed_fact_matches,
            parse_query_slots,
        )

        slots = parse_query_slots(requirement_text)
        coverage_required = bool(
            slots.coverage_template and slots.required_legs
        )
        fact_matches_required_slots = typed_fact_matches
    except (ImportError, AttributeError):
        pass

    saw_ambiguous = False
    saw_exhaustive_absent = False
    matched_facts: list[dict] = []
    usable_facts: list[dict] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue

        fact_table = str(fact.get("table", "") or table or "").strip()
        if table and fact_table and fact_table != table:
            continue
        status = normalize_fact_status(fact.get("status"))
        has_value = fact.get("value", "") not in ("", None)
        if status == FACT_STATUS_FOUND and has_value:
            usable_facts.append(fact)

        name_matches = requirement_name_matches_fact(
            requirement_text,
            fact,
            table=fact_table or table,
        )
        coverage_slot_matches = bool(
            coverage_required
            and slots is not None
            and callable(fact_matches_required_slots)
            and fact_matches_required_slots(
                slots,
                fact,
                _fact_semantic_text(fact),
            )
        )
        if not name_matches and not coverage_slot_matches:
            continue

        if status == FACT_STATUS_FOUND and has_value:
            matched_facts.append(fact)
            continue
        if status == FACT_STATUS_AMBIGUOUS:
            saw_ambiguous = True
            continue
        if status != FACT_STATUS_NOT_FOUND:
            continue

        evidence_state = str(
            fact.get("evidence_state", "")
            or fact.get("retrieval_status", "")
            or ""
        ).strip().lower()
        exhaustive = bool(fact.get("search_exhaustive")) or evidence_state in {
            REQUIREMENT_EXHAUSTIVE_ABSENT,
            "complete_scan_absent",
        }
        if exhaustive:
            saw_exhaustive_absent = True

    if _matching_typed_facts_conflict(matched_facts):
        return REQUIREMENT_AMBIGUOUS
    if matched_facts:
        temporal_complete = _multi_period_requirement_is_complete(
            slots,
            matched_facts=matched_facts,
            usable_facts=usable_facts,
        )
        coverage_complete = (
            not coverage_required
            or _coverage_requirement_is_complete(
                slots,
                matched_facts=matched_facts,
                usable_facts=usable_facts,
            )
        )
        if temporal_complete and coverage_complete:
            return REQUIREMENT_MATCHED
        if saw_ambiguous:
            return REQUIREMENT_AMBIGUOUS
        if saw_exhaustive_absent:
            return REQUIREMENT_EXHAUSTIVE_ABSENT
        return REQUIREMENT_UNMATCHED_TOPK
    if saw_ambiguous:
        return REQUIREMENT_AMBIGUOUS
    if saw_exhaustive_absent:
        return REQUIREMENT_EXHAUSTIVE_ABSENT
    return REQUIREMENT_UNMATCHED_TOPK


def _typed_slot_identity(fact: dict) -> tuple[str, ...] | None:
    """Return a conservative logical-cell identity for conflict detection."""

    row_label = _collapse(fact.get("row_label", ""))
    column_label = _collapse(fact.get("column_label", ""))
    value_kind = _collapse(fact.get("value_kind", ""))
    if not row_label or not column_label or not value_kind:
        return None

    return (
        _collapse(fact.get("company", "")),
        _collapse(fact.get("fiscal_year", "")),
        _collapse(fact.get("index_generation", "")),
        _collapse(fact.get("table", "")),
        _collapse(fact.get("note_ref", "")),
        _collapse(
            fact.get("section_path", "")
            or fact.get("subheading", "")
        ),
        row_label,
        column_label,
        _collapse(
            fact.get("period", "")
            or fact.get("period_label", "")
            or fact.get("period_role", "")
        ),
        _collapse(fact.get("value_type", "")),
        _collapse(fact.get("aggregation_level", "")),
        _collapse(fact.get("unit", "")),
        value_kind,
    )


def _canonical_fact_value(fact: dict) -> str:
    parsed = str(fact.get("parsed_value", "") or "").strip()
    if parsed:
        try:
            value = Decimal(parsed)
        except InvalidOperation:
            return _collapse(parsed)
        canonical = format(value.normalize(), "f")
        return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical

    return _collapse(
        fact.get("normalized_value", "")
        or fact.get("raw_value", "")
        or fact.get("value", "")
    )


def _matching_typed_facts_conflict(facts: Iterable[dict]) -> bool:
    values_by_slot: dict[tuple[str, ...], set[str]] = {}
    for fact in facts:
        identity = _typed_slot_identity(fact)
        if identity is None:
            continue
        value = _canonical_fact_value(fact)
        if not value:
            continue
        values_by_slot.setdefault(identity, set()).add(value)
    return any(len(values) > 1 for values in values_by_slot.values())


def not_found_after_search_message(item_name: Any, table: str = "") -> str:
    item = str(item_name or "").strip() or "khoản mục cần tìm"
    statement = str(table or "").strip() or "báo cáo tài chính"
    return (
        f"Không tìm thấy dòng {item} trong dữ liệu hiện có. "
        f"Có thể khoản này không phát sinh/không được trình bày riêng trong {statement}, "
        "nhưng cần xác nhận từ báo cáo gốc."
    )
