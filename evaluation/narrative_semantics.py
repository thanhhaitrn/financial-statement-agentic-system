"""Shared semantic guards for grounded narrative evidence.

These helpers deliberately inspect the fact payload itself, not retrieval
queries or planner requirements.  A route hint can explain why a fact was
retrieved, but it is never evidence that the routed premise is true.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any


def normalize_narrative_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").lower())
    return " ".join(
        "".join(
            character
            for character in normalized
            if unicodedata.category(character) != "Mn"
        )
        .replace("đ", "d")
        .split()
    )


_LISTING_POLICY_MARKERS = (
    "gia niem yet",
    "gia dong cua",
    "gia tham chieu",
    "gia tri hop ly",
    "xac dinh gia",
    "ky thuat dinh gia",
    "trich lap du phong",
    "lap du phong",
    "doi voi chung khoan niem yet",
    "doi voi co phieu da niem yet",
    "truong hop co phieu da niem yet",
    "huy niem yet",
    "dinh chi giao dich",
    "ngung giao dich",
)

_LISTING_EVENT_MARKERS = (
    "giay phep niem yet",
    "cap phep niem yet",
    "duoc niem yet",
    "chinh thuc niem yet",
    "bat dau niem yet",
    "dang ky niem yet",
    "ngay niem yet",
    "su kien niem yet",
)


def is_listing_event_text(value: Any, *, requirement: bool = False) -> bool:
    """Return whether text describes a corporate/share listing event.

    Merely discussing a quoted price, listed instruments, valuation policy, or
    delisting is not evidence that the reporting company itself was listed.
    """

    text = normalize_narrative_text(value)
    if "niem yet" not in text:
        return False
    if requirement:
        return True

    event_marker = any(marker in text for marker in _LISTING_EVENT_MARKERS)
    company_subject = any(
        marker in text
        for marker in (
            "co phieu cua cong ty",
            "co phieu cua doanh nghiep",
            "cong ty da niem yet",
            "cong ty duoc niem yet",
            "doanh nghiep da niem yet",
            "doanh nghiep duoc niem yet",
        )
    )
    dated_corporate_event = bool(
        re.search(r"\b(?:19|20)\d{2}\b", text)
        and any(
            marker in text
            for marker in (
                "co phieu",
                "cong ty",
                "doanh nghiep",
                "co phan hoa",
                "giay phep",
            )
        )
    )
    exchange_event = bool(
        "so giao dich chung khoan" in text
        and any(
            marker in text
            for marker in (
                "duoc niem yet",
                "giay phep niem yet",
                "cap phep niem yet",
                "co phieu cua cong ty",
            )
        )
    )
    if not (event_marker or company_subject or dated_corporate_event or exchange_event):
        return False

    policy_context = any(marker in text for marker in _LISTING_POLICY_MARKERS)
    if policy_context and not (
        "giay phep niem yet" in text
        or "cap phep niem yet" in text
        or company_subject
    ):
        return False
    return True


def narrative_concept_atoms(
    value: Any,
    *,
    requirement: bool = False,
) -> set[str]:
    """Extract conservative event/topic atoms used by premise binding."""

    text = normalize_narrative_text(value)
    atoms: set[str] = set()

    if "doanh nghiep nha nuoc" in text:
        if requirement or any(
            marker in text
            for marker in (
                "la doanh nghiep nha nuoc",
                "loai hinh doanh nghiep nha nuoc",
                "theo loai hinh doanh nghiep nha nuoc",
                "tu doanh nghiep nha nuoc",
                "chuyen tu doanh nghiep nha nuoc",
                "co phan hoa tu doanh nghiep nha nuoc",
            )
        ):
            atoms.add("state_owned")

    if "co phan hoa" in text:
        atoms.add("corporatization")

    if "dang ky" in text and "cong ty co phan" in text:
        registration_event = requirement or bool(
            re.search(
                r"\bdang ky(?:\s+\w+){0,5}\s+"
                r"(?:tro thanh|thanh|duoi hinh thuc)\b",
                text,
            )
            or "dang ky tro thanh mot cong ty co phan" in text
            or "dang ky thanh cong ty co phan" in text
        )
        if registration_event:
            atoms.add("joint_stock_registration")

    if is_listing_event_text(text, requirement=requirement):
        licensing_event = (
            "cap phep" in text
            or "giay phep niem yet" in text
        )
        if licensing_event:
            atoms.add("listing_license")
        # A licence authorises a future listing; it does not by itself prove
        # that the shares were subsequently listed.  Requirements containing
        # "cấp phép và niêm yết" intentionally request both atoms, while an
        # evidence payload receives ``listing`` only from an actual/combined
        # listing event.
        actual_listing_event = (
            requirement
            or not licensing_event
            or any(
                marker in text
                for marker in (
                    "duoc niem yet",
                    "chinh thuc niem yet",
                    "bat dau niem yet",
                    "dang ky niem yet",
                    "ngay niem yet",
                    "cap phep va niem yet",
                )
            )
        )
        if actual_listing_event:
            atoms.add("listing")

    if "chinh sach" in text:
        atoms.add("policy")
    if "kiem soat noi bo" in text:
        atoms.add("internal_control")
    if "hoat dong lien tuc" in text or "going concern" in text:
        atoms.add("going_concern")
    return atoms


_STRUCTURED_CONTEXT_LABEL_RE = re.compile(
    r"^\s*([^:\n]{1,80})\s*:\s*(.*)$"
)
_STRUCTURED_CONTEXT_MARKERS = {
    "fact id",
    "table",
    "item",
    "row",
    "value",
    "parsed value",
    "raw value",
    "source",
}
_VALUE_LABELS = ("value", "parsed value", "raw value")
_LABEL_CONTEXT_FIELDS = (
    "item",
    "row",
    "subheading",
    "section",
    "column",
)


def _mapping_evidence_surface(context: Mapping[str, Any]) -> str:
    value = next(
        (
            str(context.get(key, "") or "").strip()
            for key in (
                "value",
                "parsed_value",
                "raw_value",
                "normalized_value",
            )
            if str(context.get(key, "") or "").strip()
        ),
        "",
    )
    if not value:
        return ""
    value_tokens = normalize_narrative_text(value).split()
    if len(value_tokens) >= 3:
        return value
    labels = [
        str(context.get(key, "") or "").strip()
        for key in (
            "item_name",
            "row_label",
            "subheading",
            "section_path",
            "column_label",
        )
        if str(context.get(key, "") or "").strip()
    ]
    return "\n".join([*labels, value])


def narrative_evidence_surface(context: Any) -> str:
    """Project a retrieved context to evidence-bearing content only.

    Structured placeholders often repeat the question in ``Item`` while
    carrying no canonical value.  They must not satisfy a reviewed narrative
    proposition.  For real structured facts, the value is authoritative; fact
    labels are used only when the value is a short scalar/date.
    """

    if isinstance(context, Mapping):
        return _mapping_evidence_surface(context)

    raw = str(context or "").strip()
    if not raw:
        return ""
    parsed: dict[str, list[str]] = {}
    structured_labels = set()
    for line in raw.splitlines():
        match = _STRUCTURED_CONTEXT_LABEL_RE.match(line)
        if not match:
            continue
        label = normalize_narrative_text(match.group(1))
        value = match.group(2).strip()
        structured_labels.add(label)
        if value:
            parsed.setdefault(label, []).append(value)

    if not structured_labels.intersection(_STRUCTURED_CONTEXT_MARKERS):
        return raw

    value = next(
        (
            "\n".join(parsed[label])
            for label in _VALUE_LABELS
            if parsed.get(label)
        ),
        "",
    )
    if not value:
        return ""
    if len(normalize_narrative_text(value).split()) >= 3:
        return value
    labels = [
        item
        for label in _LABEL_CONTEXT_FIELDS
        for item in parsed.get(label, [])
    ]
    return "\n".join([*labels, value])


def split_grounded_answer_sections(value: Any) -> dict[str, str] | None:
    """Return normalized extracted-data and evaluative-inference sections."""

    text = normalize_narrative_text(value)
    fact_headers = ("du lieu trich xuat",)
    fact_positions = [
        (text.find(header), header)
        for header in fact_headers
        if text.find(header) >= 0
    ]
    if not fact_positions:
        return None
    fact_index, fact_header = min(fact_positions, key=lambda item: item[0])
    inference_headers = ("suy luan danh gia",)
    inference_positions = [
        (text.find(header), header)
        for header in inference_headers
        if text.find(header) >= 0
    ]
    if not inference_positions:
        return None
    inference_index, inference_header = min(
        inference_positions,
        key=lambda item: item[0],
    )
    if not fact_index < inference_index:
        return None
    return {
        "fact": text[
            fact_index + len(fact_header) : inference_index
        ].strip(" :*-"),
        "inference": text[
            inference_index + len(inference_header) :
        ].strip(" :*-"),
    }
