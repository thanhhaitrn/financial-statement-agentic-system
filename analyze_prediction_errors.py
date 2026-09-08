"""Triage prediction failures: errored questions and wrong-vs-ground-truth answers.

Deterministic-first classification (high confidence), with RAGAS scores used only
as a soft review signal.  A prediction may carry a reviewed ``gold_atoms`` contract
whose atoms have one of these kinds: ``amount``, ``count``, ``percent``,
``multiple``, ``date``, ``identifier`` or ``entity``.  When the contract is absent,
the tool derives conservative typed atoms from the ground truth for backwards
compatibility.  Typed atoms prevent a year, report number and financial amount from
being treated as interchangeable "numbers".

For every prediction it assigns:
  * a label       -- ERROR / WRONG_REFUSAL / WRONG_NUMBER / WRONG_OVERLAP / OK
  * a root cause   -- INFRA_ERROR / RETRIEVAL_MISS / SYNTHESIS_MISS / GT_DESIGN
  * evidence flags -- coverage of the gold fact in retrieved_contexts vs in answer

The root cause uses the 2x2 "is the gold fact in the retrieved context? x is it in
the answer?" matrix: the fact missing from context is a retrieval failure; the fact
present in context but absent from the answer (e.g. a refusal despite evidence) is a
synthesis/prompt failure.

Usage:
  python analyze_prediction_errors.py ragas_runs/vnm_predictions.json [more.json ...]
      [--gold-contract reviewed_gold_atoms.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from eval_retrieval_recall import is_analytical
from evaluation.financial_text import (
    _DATE_RE,
    _plain_text,
    normalize_period,
    normalize_text,
    normalize_unit,
    normalize_value,
)
from evaluation.contracts import provider_limit_reason
from evaluation.narrative_semantics import (
    is_listing_event_text,
    narrative_evidence_surface,
    split_grounded_answer_sections,
)

METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

# Normalized (accent-free, lowercased) refusal markers.  normalize_text() folds
# answers into the same form before the substring test.
REFUSAL_PHRASES = (
    "khong co thong tin",
    "khong co du lieu",
    "khong co so lieu",
    "khong tim thay",
    "khong tim thay so lieu",
    "khong tim thay thong tin",
    "khong du du lieu",
    "trong du lieu hien co khong",
    "khong the tra loi",
    "khong the tinh",
    "khong tinh duoc",
    "chua the thuc hien phep tinh",
    "khong thuc hien phep tinh",
    "unable to perform the calculation",
    "cannot perform the calculation",
    "khong duoc de cap",
    "khong duoc cung cap",
    "khong duoc trinh bay",
    "khong neu ro",
    "khong xac dinh duoc",
    "khong the xac dinh",
    "khong ro",
)

# Small Vietnamese stopword set (normalized) so entity-overlap keeps only the
# distinctive answer tokens (names, places) and ignores glue words.
STOPWORDS = {
    "la", "cua", "va", "cac", "mot", "co", "cho", "den", "tu", "trong", "tai",
    "theo", "voi", "nam", "cong", "ty", "cp", "the", "nhung", "duoc", "ve",
    "hay", "hoac", "so", "ngay", "thang", "cong ty", "co phan", "khi", "da",
    "cua cong", "ban", "cua ong", "cua ba", "ong", "ba",
}

SOFT_THRESHOLD = 0.5           # RAGAS answer_relevancy/faithfulness review band
COVERAGE_PRESENT = 0.5         # >= this fraction of the gold fact => "present"
HARD_ERROR_GATE_THRESHOLD = 0.95

GOLD_ATOM_KINDS = frozenset(
    {"amount", "count", "percent", "multiple", "date", "identifier", "entity"}
)
QUANTITATIVE_ATOM_KINDS = frozenset({"amount", "count", "percent", "multiple"})
HARD_ERROR_LABELS = frozenset(
    {"ERROR", "WRONG_REFUSAL", "WRONG_NUMBER", "WRONG_OVERLAP"}
)
STRUCTURED_ABSTENTION_STATUSES = frozenset(
    {"abstain", "abstained", "abstention", "insufficient_evidence"}
)
STRUCTURED_ABSTENTION_REASONS = frozenset(
    {
        "missing_operand_contract",
        "unbound_required_operands",
        "incomplete_calculation_contract",
        "missing_required_evidence",
    }
)
NARRATIVE_SUPPORT_MODES = frozenset(
    {"explicit", "bounded_inference", "external_required"}
)
NARRATIVE_EVIDENCE_SEMANTICS = frozenset({"", "listing_event"})

_NUMBER_TOKEN_RE = re.compile(r"(?<![\w])\(?-?\d+(?:[.,]\d+)*\)?(?![\w])")
_PERCENT_RE = re.compile(
    r"(?P<value>\(?-?\d+(?:[.,]\d+)*\)?)\s*(?:%|phan\s+tram\b)"
)
_MULTIPLE_RE = re.compile(
    r"(?P<value>\(?-?\d+(?:[.,]\d+)*\)?)\s*(?:lan|x)\b"
)
_ZERO_BALANCE_RE = re.compile(
    r"\b(?:khong\s+(?:con|co)\s+so\s+du|so\s+du(?:\s+\w+){0,5}\s+bang\s+0)\b"
)
_EPISTEMIC_ABSENCE_RE = re.compile(
    r"\b(?:khong\s+(?:co|tim\s+thay)\s+(?:thong\s+tin|du\s+lieu|so\s+lieu|"
    r"so\s+(?:du|chi\s+phi|gia\s+tri)|chi\s+phi|gia\s+tri)|"
    r"(?:thong\s+tin|du\s+lieu|so\s+lieu)\s+(?:hien\s+co\s+)?khong\s+)"
)
_EVIDENCE_SCOPE_RE = re.compile(
    r"\b(?:trong|tu|theo)\s+(?:du\s+lieu|bao\s+cao|tai\s+lieu|"
    r"thong\s+tin|nguon|context)\b|\b(?:du\s+lieu|thong\s+tin)\s+hien\s+co\b"
)
_COUNT_RE = re.compile(
    r"(?P<value>\d{1,3})\s+"
    r"(?P<unit>nha\s+may|chi\s+nhanh|cong\s+ty\s+con|don\s+vi|thanh\s+vien|"
    r"nguoi|nhan\s+su|du\s+an|hop\s+dong|khoan\s+vay|co\s+dong)\b"
)
_IDENTIFIER_RE = re.compile(
    r"\b(?:bao\s+cao|giay|quyet\s+dinh|hop\s+dong|ma|so)\s+(?:so\s+)?"
    r"(?P<value>(?:(?=[a-z0-9./-]{3,}\b)(?=[a-z0-9./-]*[a-z])"
    r"(?=[a-z0-9./-]*\d)[a-z0-9]+(?:[./-][a-z0-9]+)+|\d{6,}))\b"
)
_YEAR_MARKER_RE = re.compile(r"\bnam\s+(?P<value>20\d{2})\b")
_YEAR_TOKEN_RE = re.compile(r"\b20\d{2}\b")
_AMOUNT_WITH_UNIT_RE = re.compile(
    r"(?P<value>\(?-?\d+(?:[.,]\d+)*\)?)\s*"
    r"(?P<unit>nghin|trieu|ty)?\s*(?:vnd|dong)\b"
)
_LONG_NUMBER_RE = re.compile(
    r"(?<!\d)\(?-?(?:\d{1,3}(?:[.,]\d{3}){1,}|\d{4,})\)?(?!\d)"
)
_ROLE_CANONICAL_ALIASES = {
    "current": "current",
    "current_operand": "current",
    "current_period": "current",
    "current_value": "current",
    "new": "current",
    "new_value": "current",
    "previous": "previous",
    "previous_operand": "previous",
    "previous_period": "previous",
    "previous_value": "previous",
    "prior": "previous",
    "prior_value": "previous",
    "old": "previous",
    "old_value": "previous",
    "numerator": "numerator",
    "denominator": "denominator",
    "derived": "derived_result",
    "derived_result": "derived_result",
    "calculated_result": "derived_result",
    "calculation_result": "derived_result",
    "result": "derived_result",
}
_ROLE_MARKER_PATTERNS = {
    "current": re.compile(
        r"\b(?:current(?:\s+(?:operand|period|value|year))?|"
        r"new\s+value|nam\s+nay|ky\s+nay|gia\s+tri\s+hien\s+tai|"
        r"hien\s+tai|cuoi\s+ky|cuoi\s+nam)\b"
    ),
    "previous": re.compile(
        r"\b(?:previous(?:\s+(?:operand|period|value|year))?|"
        r"prior(?:\s+(?:period|value|year))?|old\s+value|"
        r"nam\s+truoc|ky\s+truoc|gia\s+tri\s+truoc|dau\s+ky|dau\s+nam)\b"
    ),
    "numerator": re.compile(r"\b(?:numerator|tu\s+so)\b"),
    "denominator": re.compile(r"\b(?:denominator|mau\s+so)\b"),
    "derived_result": re.compile(
        r"\b(?:derived\s+result|calculated\s+result|calculation\s+result|"
        r"ket\s+qua(?:\s+tinh(?:\s+duoc)?)?|gia\s+tri\s+tinh\s+duoc)\b"
    ),
}
_ROLE_CLAUSE_BOUNDARY_RE = re.compile(r"[;.!?\n]")
_FORMULA_RE = re.compile(
    r"(?P<numerator>[^;.!?\n]{1,120}?)\s*/\s*"
    r"(?P<denominator>[^=;.!?\n]{1,120})"
    r"(?:\s*=\s*(?P<derived_result>[^;.!?\n]{1,120}))?"
)


@dataclass(frozen=True)
class GoldAtom:
    """One reviewed, independently verifiable fact in a gold answer."""

    kind: str
    value: str
    unit: str = ""
    aliases: tuple[str, ...] = ()
    required: bool = True
    role: str = ""
    sign_policy: str = "exact"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "value": self.value}
        if self.unit:
            result["unit"] = self.unit
        if self.aliases:
            result["aliases"] = list(self.aliases)
        if not self.required:
            result["required"] = False
        if self.role:
            result["role"] = self.role
        if self.sign_policy != "exact":
            result["sign_policy"] = self.sign_policy
        return result


@dataclass(frozen=True)
class GoldAtomContract:
    """Reviewed atom records addressable by dataset/id and exact question."""

    path: str
    schema_version: int
    dataset_id: str
    entries: tuple[dict[str, Any], ...]


def _norm_tokens(text: Any) -> list[str]:
    return [tok for tok in normalize_text(text).split() if len(tok) > 1]


def _is_zero_balance_fact(text: Any) -> bool:
    """Return true for an asserted zero balance, not a missing-data statement."""
    plain = _plain_text(text)
    if _EPISTEMIC_ABSENCE_RE.search(plain) and _EVIDENCE_SCOPE_RE.search(plain):
        return False
    return bool(_ZERO_BALANCE_RE.search(plain))


def _is_refusal(text: Any) -> bool:
    plain = normalize_text(text)
    if any(phrase in plain for phrase in REFUSAL_PHRASES):
        return True
    return bool(
        _EPISTEMIC_ABSENCE_RE.search(plain)
        and _EVIDENCE_SCOPE_RE.search(plain)
    )


def _structured_abstention_reason(prediction: dict[str, Any]) -> str:
    """Return a stable abstention signal without treating partial prose as infra."""

    nested = prediction.get("synth_decision", {})
    nested = nested if isinstance(nested, dict) else {}
    statuses = (
        prediction.get("synth_status"),
        prediction.get("answer_status"),
        nested.get("status"),
    )
    for status in statuses:
        normalized = normalize_text(status).replace(" ", "_")
        if normalized in STRUCTURED_ABSTENTION_STATUSES:
            return normalized

    reasons = (
        prediction.get("synth_reason_code"),
        prediction.get("abstention_reason"),
        prediction.get("reason_code"),
        nested.get("reason_code"),
        nested.get("abstention_reason"),
    )
    for reason in reasons:
        normalized = normalize_text(reason).replace(" ", "_")
        if normalized in STRUCTURED_ABSTENTION_REASONS:
            return normalized
    return ""


def _normalize_decimal(value: Any) -> str:
    normalized = normalize_value(value)
    try:
        return format(Decimal(normalized).normalize(), "f")
    except InvalidOperation:
        return normalized


def _normalize_atom_value(kind: str, value: Any) -> str:
    if kind in QUANTITATIVE_ATOM_KINDS:
        return _normalize_decimal(value)
    if kind == "date":
        return normalize_period(value)
    return normalize_text(value)


def _normalize_atom_unit(kind: str, value: Any) -> str:
    if kind == "percent":
        return "percent"
    if kind == "multiple":
        return "times"
    if not value:
        return ""
    return normalize_unit(value)


def _canonical_atom_role(value: Any) -> str:
    normalized = normalize_text(value).replace(" ", "_")
    return _ROLE_CANONICAL_ALIASES.get(normalized, normalized)


def _coerce_gold_atom(raw: Any) -> GoldAtom:
    if not isinstance(raw, dict):
        raise ValueError(f"gold atom must be an object, got {type(raw).__name__}")
    kind = str(raw.get("kind", "") or "").strip().casefold()
    if kind not in GOLD_ATOM_KINDS:
        allowed = ", ".join(sorted(GOLD_ATOM_KINDS))
        raise ValueError(f"unsupported gold atom kind {kind!r}; expected one of: {allowed}")
    if "value" not in raw or raw.get("value") is None or str(raw.get("value")).strip() == "":
        raise ValueError(f"gold atom {kind!r} must define a non-empty value")
    value = _normalize_atom_value(kind, raw["value"])
    if not value:
        raise ValueError(f"gold atom {kind!r} has an unnormalizable value")
    aliases_raw = raw.get("aliases", []) or []
    if not isinstance(aliases_raw, list):
        raise ValueError(f"gold atom aliases must be a list, got {type(aliases_raw).__name__}")
    aliases = tuple(
        alias
        for alias in (_normalize_atom_value(kind, candidate) for candidate in aliases_raw)
        if alias and alias != value
    )
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError("gold atom required must be a boolean")
    role = _canonical_atom_role(raw.get("role", ""))
    operand_role = _canonical_atom_role(raw.get("operand_role", ""))
    if role and operand_role and role != operand_role:
        raise ValueError(
            "gold atom role and operand_role must identify the same semantic role"
        )
    role = role or operand_role
    sign_policy = str(raw.get("sign_policy", "exact") or "exact").strip().casefold()
    if sign_policy not in {"exact", "accounting_magnitude"}:
        raise ValueError(
            "gold atom sign_policy must be exact or accounting_magnitude"
        )
    return GoldAtom(
        kind=kind,
        value=value,
        unit=_normalize_atom_unit(kind, raw.get("unit", "")),
        aliases=tuple(dict.fromkeys(aliases)),
        required=required,
        role=role,
        sign_policy=sign_policy,
    )


def _span_overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in occupied)


def _derived_gold_atoms(ground_truth: str) -> list[GoldAtom]:
    """Conservatively derive typed atoms for legacy reports without a contract."""
    surface = _plain_text(ground_truth)
    atoms: list[GoldAtom] = []
    occupied: list[tuple[int, int]] = []

    if _is_zero_balance_fact(ground_truth):
        atoms.append(GoldAtom(kind="amount", value="0"))

    for match in _PERCENT_RE.finditer(surface):
        atoms.append(
            GoldAtom(kind="percent", value=_normalize_decimal(match.group("value")), unit="percent")
        )
        occupied.append(match.span())
    for match in _MULTIPLE_RE.finditer(surface):
        atoms.append(
            GoldAtom(kind="multiple", value=_normalize_decimal(match.group("value")), unit="times")
        )
        occupied.append(match.span())
    for match in _DATE_RE.finditer(surface):
        atoms.append(GoldAtom(kind="date", value=normalize_period(match.group(0))))
        occupied.append(match.span())
    for match in _YEAR_MARKER_RE.finditer(surface):
        if _span_overlaps(match.span("value"), occupied):
            continue
        atoms.append(GoldAtom(kind="date", value=normalize_period(match.group("value"))))
        occupied.append(match.span("value"))
    # A bare 20xx token is much more likely to be a reporting period than an
    # amount.  Even if it is not promoted to a gold atom, never reinterpret it
    # as money in the legacy long-number fallback.
    occupied.extend(match.span() for match in _YEAR_TOKEN_RE.finditer(surface))
    for match in _IDENTIFIER_RE.finditer(surface):
        atoms.append(GoldAtom(kind="identifier", value=normalize_text(match.group("value"))))
        occupied.append(match.span("value"))
    for match in _COUNT_RE.finditer(surface):
        if _span_overlaps(match.span(), occupied):
            continue
        atoms.append(
            GoldAtom(
                kind="count",
                value=_normalize_decimal(match.group("value")),
                unit=normalize_unit(match.group("unit")),
            )
        )
        occupied.append(match.span())
    for match in _AMOUNT_WITH_UNIT_RE.finditer(surface):
        if _span_overlaps(match.span(), occupied):
            continue
        magnitude = str(match.group("unit") or "")
        unit = {
            "nghin": "thousand_vnd",
            "trieu": "million_vnd",
            "ty": "billion_vnd",
        }.get(magnitude, "vnd")
        atoms.append(
            GoldAtom(kind="amount", value=_normalize_decimal(match.group("value")), unit=unit)
        )
        occupied.append(match.span())
    for match in _LONG_NUMBER_RE.finditer(surface):
        if _span_overlaps(match.span(), occupied):
            continue
        atoms.append(GoldAtom(kind="amount", value=_normalize_decimal(match.group(0))))

    # Retain order for readable reports, but do not make one fact count twice
    # merely because both a typed and legacy pattern found the same span.
    return list(dict.fromkeys(atoms))


def gold_atoms_for_prediction(prediction: dict[str, Any]) -> tuple[list[GoldAtom], str]:
    """Return reviewed atoms when present, else conservative derived atoms."""
    has_contract = "gold_atoms" in prediction or "expected_atoms" in prediction
    raw_atoms = prediction.get("gold_atoms", prediction.get("expected_atoms", []))
    if has_contract:
        if not isinstance(raw_atoms, list):
            raise ValueError("gold_atoms/expected_atoms must be a list")
        atoms = [_coerce_gold_atom(atom) for atom in raw_atoms]
        if not atoms:
            raise ValueError("an explicit gold_atoms contract must not be empty")
        if not any(atom.required for atom in atoms):
            raise ValueError(
                "an explicit gold_atoms contract must contain a required atom"
            )
        return list(dict.fromkeys(atoms)), "explicit"
    # Open-ended analytical prose needs a reviewed proposition contract.  Numeric
    # fallback would otherwise turn a historical year such as 1993 into an
    # ``amount`` and attribute narrative correctness/root cause to that accident.
    if is_analytical(str(prediction.get("question", "") or "")):
        return [], "derived_narrative"
    return _derived_gold_atoms(str(prediction.get("ground_truth", "") or "")), "derived"


def _canonical_gold_atoms(raw_record: dict[str, Any]) -> list[dict[str, Any]]:
    present = [
        key for key in ("gold_atoms", "expected_atoms")
        if key in raw_record
    ]
    if not present:
        raise ValueError("every gold-atom contract record requires gold_atoms")

    canonical: dict[str, list[dict[str, Any]]] = {}
    for key in present:
        atoms, _ = gold_atoms_for_prediction({key: raw_record.get(key)})
        canonical[key] = [atom.as_dict() for atom in atoms]

    if (
        "gold_atoms" in canonical
        and "expected_atoms" in canonical
        and _gold_atoms_fingerprint(canonical["gold_atoms"])
        != _gold_atoms_fingerprint(canonical["expected_atoms"])
    ):
        raise ValueError("gold_atoms and expected_atoms contracts disagree")
    return canonical.get("gold_atoms", canonical.get("expected_atoms", []))


def _gold_atoms_fingerprint(atoms: list[dict[str, Any]]) -> tuple[str, ...]:
    """Compare contracts semantically while allowing atom ordering to differ."""

    return tuple(
        sorted(
            json.dumps(atom, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for atom in atoms
        )
    )


def _canonical_phrase_pattern(raw: Any, *, field: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} pattern must be an object")
    output: dict[str, list[str]] = {}
    for key in ("all_of", "any_of", "none_of"):
        values = raw.get(key, []) or []
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise ValueError(f"{field}.{key} must be a string list")
        normalized = list(
            dict.fromkeys(
                phrase
                for phrase in (normalize_text(value) for value in values)
                if phrase
            )
        )
        if normalized:
            output[key] = normalized
    if not output.get("all_of") and not output.get("any_of"):
        raise ValueError(f"{field} pattern requires all_of or any_of")
    return output


def _canonical_phrase_patterns(raw: Any, *, field: str) -> list[dict[str, list[str]]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must be a non-empty pattern list")
    return [
        _canonical_phrase_pattern(pattern, field=f"{field}[{index}]")
        for index, pattern in enumerate(raw)
    ]


def _canonical_narrative_contract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("narrative_contract must be an object")
    propositions_raw = raw.get("propositions", [])
    if not isinstance(propositions_raw, list) or not propositions_raw:
        raise ValueError("narrative_contract requires propositions")

    propositions: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(propositions_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"narrative proposition {index} must be an object")
        name = str(item.get("name", "") or "").strip()
        if not name:
            raise ValueError(f"narrative proposition {index} requires name")
        if name in names:
            raise ValueError(f"duplicate narrative proposition name: {name}")
        names.add(name)

        flags = {}
        for field in (
            "required_in_evidence",
            "required_in_answer",
            "optional",
            "validate_if_present",
            "prohibited",
        ):
            value = item.get(field, False)
            if not isinstance(value, bool):
                raise ValueError(f"narrative proposition {name!r} {field} must be boolean")
            flags[field] = value
        if flags["optional"] and (
            flags["required_in_evidence"] or flags["required_in_answer"]
        ):
            raise ValueError(
                f"optional narrative proposition {name!r} cannot be required"
            )
        if flags["prohibited"] and (
            flags["required_in_evidence"] or flags["required_in_answer"]
        ):
            raise ValueError(
                f"prohibited narrative proposition {name!r} cannot be required"
            )

        support = str(item.get("support", "explicit") or "explicit").strip()
        if support not in NARRATIVE_SUPPORT_MODES:
            raise ValueError(
                f"narrative proposition {name!r} has unsupported support mode"
            )
        patterns = _canonical_phrase_patterns(
            item.get("patterns"),
            field=f"narrative proposition {name!r}.patterns",
        )
        evidence_patterns = _canonical_phrase_patterns(
            item.get("evidence_patterns", patterns),
            field=f"narrative proposition {name!r}.evidence_patterns",
        )
        support_patterns_raw = item.get("support_patterns")
        support_patterns = (
            _canonical_phrase_patterns(
                support_patterns_raw,
                field=f"narrative proposition {name!r}.support_patterns",
            )
            if support_patterns_raw not in (None, [])
            else []
        )
        if support == "external_required" and not support_patterns:
            raise ValueError(
                f"external_required narrative proposition {name!r} "
                "requires support_patterns"
            )
        evidence_semantic = str(
            item.get("evidence_semantic", "") or ""
        ).strip()
        if evidence_semantic not in NARRATIVE_EVIDENCE_SEMANTICS:
            raise ValueError(
                f"narrative proposition {name!r} has unsupported "
                "evidence_semantic"
            )

        propositions.append(
            {
                "name": name,
                "patterns": patterns,
                "evidence_patterns": evidence_patterns,
                "support_patterns": support_patterns,
                "support": support,
                "evidence_semantic": evidence_semantic,
                **flags,
            }
        )

    if not any(item["required_in_answer"] for item in propositions):
        raise ValueError(
            "narrative_contract requires at least one required_in_answer proposition"
        )
    return {"propositions": propositions}


def _narrative_contract_fingerprint(contract: dict[str, Any]) -> str:
    return json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dataset_key(value: Any) -> str:
    return normalize_text(value)


def load_gold_atom_contract(
    path: str | Path | None,
) -> GoldAtomContract | None:
    """Load reviewed atom or narrative contracts for legacy prediction reports.

    Accepted shapes are ``{"records": [...]}``, the fixture-compatible
    ``{"cases": [...]}``, or a bare record list.  Each record must have an
    ``id`` or an exact ``question`` (both are preferred), plus ``gold_atoms``
    and/or a ``narrative_contract``.
    """

    if not path:
        return None
    contract_path = Path(path)
    payload = json.loads(contract_path.read_text(encoding="utf-8"))

    if isinstance(payload, list):
        schema_version = 1
        dataset_id = ""
        records = payload
    elif isinstance(payload, dict):
        schema_version = int(payload.get("schema_version", 1) or 1)
        dataset_id = str(payload.get("dataset_id", "") or "").strip()
        containers = [
            key for key in ("records", "cases")
            if key in payload
        ]
        if len(containers) != 1:
            raise ValueError(
                "gold-atom contract must contain exactly one records or cases list"
            )
        records = payload.get(containers[0])
    else:
        raise ValueError("gold-atom contract must be an object or a record list")

    if schema_version not in {1, 2}:
        raise ValueError(
            f"unsupported gold-atom contract schema_version: {schema_version}"
        )
    if not isinstance(records, list):
        raise ValueError("gold-atom contract records/cases must be a list")

    entries: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str]] = set()
    seen_questions: set[tuple[str, str]] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"gold-atom contract record {index} must be an object")
        record_id = (
            ""
            if raw.get("id") in ("", None)
            else str(raw.get("id"))
        )
        question = str(raw.get("question", "") or "").strip()
        question_key = normalize_text(question)
        if not record_id and not question_key:
            raise ValueError(
                f"gold-atom contract record {index} requires id or question"
            )
        record_dataset = str(
            raw.get("dataset_id") or dataset_id or ""
        ).strip()
        record_dataset_key = _dataset_key(record_dataset)
        has_atoms = "gold_atoms" in raw or "expected_atoms" in raw
        has_narrative = "narrative_contract" in raw
        if not has_atoms and not has_narrative:
            raise ValueError(
                f"reviewed contract record {index} requires gold_atoms "
                "or narrative_contract"
            )
        atoms = _canonical_gold_atoms(raw) if has_atoms else []
        narrative_contract = (
            _canonical_narrative_contract(raw.get("narrative_contract"))
            if has_narrative
            else None
        )
        benchmark_issues_raw = raw.get("benchmark_issues", []) or []
        if not isinstance(benchmark_issues_raw, list) or any(
            not isinstance(issue, str) for issue in benchmark_issues_raw
        ):
            raise ValueError(
                f"reviewed contract record {index} benchmark_issues "
                "must be a string list"
            )
        benchmark_issues = list(
            dict.fromkeys(
                issue.strip()
                for issue in benchmark_issues_raw
                if issue.strip()
            )
        )

        if record_id:
            id_key = (record_dataset_key, record_id)
            if id_key in seen_ids:
                raise ValueError(
                    f"duplicate gold-atom contract id: {record_id}"
                )
            seen_ids.add(id_key)
        if question_key:
            exact_question_key = (record_dataset_key, question_key)
            if exact_question_key in seen_questions:
                raise ValueError(
                    f"duplicate gold-atom contract question: {question}"
                )
            seen_questions.add(exact_question_key)

        entries.append(
            {
                "id": record_id,
                "question": question,
                "question_key": question_key,
                "dataset_id": record_dataset,
                "dataset_key": record_dataset_key,
                "gold_atoms": atoms,
                "narrative_contract": narrative_contract,
                "benchmark_issues": benchmark_issues,
            }
        )

    if not entries:
        raise ValueError("gold-atom contract has no records")
    return GoldAtomContract(
        path=str(contract_path),
        schema_version=schema_version,
        dataset_id=dataset_id,
        entries=tuple(entries),
    )


def _report_dataset_id(report: dict[str, Any]) -> str:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    if not isinstance(metadata, dict):
        return ""
    dataset = metadata.get("dataset", {})
    if isinstance(dataset, dict) and dataset.get("dataset_id") not in ("", None):
        return str(dataset.get("dataset_id"))
    return str(metadata.get("dataset_id", "") or "")


def _resolve_gold_contract_entry(
    prediction: dict[str, Any],
    contract: GoldAtomContract,
    *,
    report_dataset_id: str,
) -> dict[str, Any] | None:
    prediction_id = (
        ""
        if prediction.get("id") in ("", None)
        else str(prediction.get("id"))
    )
    question = str(prediction.get("question", "") or "").strip()
    question_key = normalize_text(question)
    dataset_key = _dataset_key(report_dataset_id)
    eligible = [
        entry
        for entry in contract.entries
        if not entry["dataset_key"]
        or not dataset_key
        or entry["dataset_key"] == dataset_key
    ]
    id_matches = [
        entry for entry in eligible
        if prediction_id and entry["id"] == prediction_id
    ]
    question_matches = [
        entry for entry in eligible
        if question_key and entry["question_key"] == question_key
    ]
    if len(id_matches) > 1:
        raise ValueError(
            f"ambiguous gold-atom contract id {prediction_id!r} across datasets"
        )
    if len(question_matches) > 1:
        raise ValueError(
            f"ambiguous gold-atom contract question {question!r} across datasets"
        )

    by_id = id_matches[0] if id_matches else None
    by_question = question_matches[0] if question_matches else None
    if (
        by_id
        and by_id["question_key"]
        and question_key
        and by_id["question_key"] != question_key
    ):
        raise ValueError(
            f"prediction id {prediction_id!r} question disagrees with "
            "the gold-atom contract"
        )
    if (
        by_question
        and prediction_id
        and by_question["id"]
        and by_question["id"] != prediction_id
    ):
        raise ValueError(
            f"prediction question {question!r} id disagrees with "
            "the gold-atom contract"
        )
    if by_id and by_question and by_id is not by_question:
        raise ValueError(
            f"prediction id {prediction_id!r} and question resolve to "
            "different gold-atom contract records"
        )
    return by_id or by_question


def _bind_gold_atom_contract(
    prediction: dict[str, Any],
    contract: GoldAtomContract,
    *,
    report_dataset_id: str,
) -> tuple[dict[str, Any], bool]:
    entry = _resolve_gold_contract_entry(
        prediction,
        contract,
        report_dataset_id=report_dataset_id,
    )
    if entry is None:
        return prediction, False

    bound = dict(prediction)
    external_atoms = list(entry["gold_atoms"])
    if external_atoms:
        if "gold_atoms" in prediction or "expected_atoms" in prediction:
            embedded_atoms = _canonical_gold_atoms(prediction)
            if (
                _gold_atoms_fingerprint(embedded_atoms)
                != _gold_atoms_fingerprint(external_atoms)
            ):
                raise ValueError(
                    f"prediction id {prediction.get('id')!r} embedded gold atoms "
                    "disagree with the external contract"
                )
        bound["gold_atoms"] = external_atoms

    external_narrative = entry.get("narrative_contract")
    if external_narrative is not None:
        embedded_raw = prediction.get("narrative_contract")
        if embedded_raw is not None:
            embedded_narrative = _canonical_narrative_contract(embedded_raw)
            if (
                _narrative_contract_fingerprint(embedded_narrative)
                != _narrative_contract_fingerprint(external_narrative)
            ):
                raise ValueError(
                    f"prediction id {prediction.get('id')!r} embedded narrative "
                    "contract disagrees with the external contract"
                )
        bound["narrative_contract"] = external_narrative
    bound["benchmark_issues"] = list(entry.get("benchmark_issues", []) or [])
    return bound, True


def _key_gt_tokens(question: str, ground_truth: str) -> list[str]:
    """Distinctive ground-truth tokens: those not already in the question."""
    q = set(_norm_tokens(question))
    key = []
    for tok in _norm_tokens(ground_truth):
        if tok in q or tok in STOPWORDS or tok.isdigit():
            continue
        key.append(tok)
    return key


def _number_mentions(text: Any) -> list[str]:
    surface = _plain_text(text)
    return [_normalize_decimal(match.group(0)) for match in _NUMBER_TOKEN_RE.finditer(surface)]


def _amount_mentions(text: Any) -> list[tuple[str, str]]:
    surface = _plain_text(text)
    mentions: list[tuple[str, str]] = []
    unit_spans: list[tuple[int, int]] = []
    for match in _AMOUNT_WITH_UNIT_RE.finditer(surface):
        magnitude = str(match.group("unit") or "")
        unit = {
            "nghin": "thousand_vnd",
            "trieu": "million_vnd",
            "ty": "billion_vnd",
        }.get(magnitude, "vnd")
        mentions.append((_normalize_decimal(match.group("value")), unit))
        unit_spans.append(match.span("value"))
    for match in _NUMBER_TOKEN_RE.finditer(surface):
        if _span_overlaps(match.span(), unit_spans):
            continue
        mentions.append((_normalize_decimal(match.group(0)), ""))
    return mentions


def _canonical_amount(value: str, unit: str) -> Decimal | None:
    multiplier = {
        "": Decimal(1),
        "vnd": Decimal(1),
        "thousand_vnd": Decimal(1_000),
        "million_vnd": Decimal(1_000_000),
        "billion_vnd": Decimal(1_000_000_000),
    }.get(unit)
    if multiplier is None:
        return None
    try:
        return Decimal(value) * multiplier
    except InvalidOperation:
        return None


def _quantitative_value_matches(expected: str, actual: str) -> bool:
    try:
        return Decimal(expected) == Decimal(actual)
    except InvalidOperation:
        return expected == actual


def _signed_quantitative_value_matches(
    expected: str,
    actual: str,
    *,
    sign_policy: str,
) -> bool:
    if sign_policy == "exact":
        return _quantitative_value_matches(expected, actual)
    try:
        return abs(Decimal(expected)) == abs(Decimal(actual))
    except InvalidOperation:
        return expected == actual


def _amount_matches(atom: GoldAtom, text: Any) -> bool:
    if atom.value == "0" and _is_zero_balance_fact(text):
        return True
    targets = (atom.value, *atom.aliases)
    for actual, actual_unit in _amount_mentions(text):
        for expected in targets:
            if _signed_quantitative_value_matches(
                expected,
                actual,
                sign_policy=atom.sign_policy,
            ):
                if not atom.unit or (
                    actual_unit and atom.unit == actual_unit
                ):
                    return True
            expected_base = _canonical_amount(expected, atom.unit)
            actual_base = _canonical_amount(actual, actual_unit)
            if (
                expected_base is not None
                and actual_base is not None
                and atom.unit
                and actual_unit
                and (
                    expected_base == actual_base
                    if atom.sign_policy == "exact"
                    else abs(expected_base) == abs(actual_base)
                )
            ):
                return True
    return False


def _typed_numeric_matches(atom: GoldAtom, text: Any) -> bool:
    surface = _plain_text(text)
    pattern = _PERCENT_RE if atom.kind == "percent" else _MULTIPLE_RE
    targets = (atom.value, *atom.aliases)
    return any(
        any(
            _signed_quantitative_value_matches(
                expected,
                _normalize_decimal(match.group("value")),
                sign_policy=atom.sign_policy,
            )
            for expected in targets
        )
        for match in pattern.finditer(surface)
    )


def _count_matches(atom: GoldAtom, text: Any) -> bool:
    surface = _plain_text(text)
    targets = (atom.value, *atom.aliases)
    if atom.unit:
        expected_unit = atom.unit.replace("_", " ")
        for match in _NUMBER_TOKEN_RE.finditer(surface):
            actual = _normalize_decimal(match.group(0))
            if not any(
                _signed_quantitative_value_matches(
                    expected,
                    actual,
                    sign_policy=atom.sign_policy,
                )
                for expected in targets
            ):
                continue
            following = normalize_text(surface[match.end():match.end() + 64])
            if expected_unit in following:
                return True
        return False
    return any(
        any(
            _signed_quantitative_value_matches(
                expected,
                actual,
                sign_policy=atom.sign_policy,
            )
            for expected in targets
        )
        for actual in _number_mentions(text)
    )


def _date_mentions(text: Any) -> set[str]:
    surface = _plain_text(text)
    mentions = {normalize_period(match.group(0)) for match in _DATE_RE.finditer(surface)}
    mentions.update(
        normalize_period(match.group("value")) for match in _YEAR_MARKER_RE.finditer(surface)
    )
    return mentions


def _atom_value_matches(atom: GoldAtom, text: Any) -> bool:
    """Match an atom's value and type without applying its semantic role."""

    if atom.kind == "amount":
        return _amount_matches(atom, text)
    if atom.kind in {"percent", "multiple"}:
        return _typed_numeric_matches(atom, text)
    if atom.kind == "count":
        return _count_matches(atom, text)
    targets = (atom.value, *atom.aliases)
    if atom.kind == "date":
        mentions = _date_mentions(text)
        return any(target in mentions for target in targets)
    plain = normalize_text(text)
    return any(target in plain for target in targets)


def _role_pattern(role: str) -> re.Pattern[str]:
    pattern = _ROLE_MARKER_PATTERNS.get(role)
    if pattern is not None:
        return pattern
    literal = re.escape(role.replace("_", " "))
    return re.compile(rf"\b{literal}\b")


def _role_scoped_surfaces(role: str, text: Any) -> list[str]:
    """Return clauses explicitly bound to ``role`` in prose or ``A / B = C``.

    A reviewed operand role is a structural assertion, not decorative metadata.
    The value therefore has to occur beside the corresponding role marker (or in
    the corresponding position of an explicit division formula).  Delimiting at
    neighbouring role markers prevents a swapped numerator/denominator pair from
    passing merely because both numbers occur somewhere in the answer.
    """

    surface = _plain_text(text)
    if not surface:
        return []

    target_pattern = _role_pattern(role)
    target_matches = list(target_pattern.finditer(surface))
    marker_matches: list[tuple[int, int]] = []
    for pattern in _ROLE_MARKER_PATTERNS.values():
        marker_matches.extend(match.span() for match in pattern.finditer(surface))
    if role not in _ROLE_MARKER_PATTERNS:
        marker_matches.extend(match.span() for match in target_matches)
    marker_matches = sorted(set(marker_matches))
    boundaries = [match.span() for match in _ROLE_CLAUSE_BOUNDARY_RE.finditer(surface)]

    scopes: list[str] = []
    for target in target_matches:
        previous_boundary = max(
            (end for start, end in boundaries if end <= target.start()),
            default=0,
        )
        next_boundary = min(
            (start for start, end in boundaries if start >= target.end()),
            default=len(surface),
        )
        previous_marker = max(
            (end for start, end in marker_matches if end <= target.start()),
            default=0,
        )
        next_marker = min(
            (start for start, end in marker_matches if start >= target.end()),
            default=len(surface),
        )
        start = max(previous_boundary, previous_marker)
        end = min(next_boundary, next_marker)
        scopes.extend(
            part.strip(" \t,:=-()[]")
            for part in (
                surface[start:target.start()],
                surface[target.end():end],
            )
            if part.strip(" \t,:=-()[]")
        )

    if role in {"numerator", "denominator", "derived_result"}:
        scopes.extend(
            str(match.group(role) or "").strip()
            for match in _FORMULA_RE.finditer(surface)
            if match.group(role)
        )
    return list(dict.fromkeys(scope for scope in scopes if scope))


def _atom_matches(atom: GoldAtom, text: Any) -> bool:
    if not atom.role:
        return _atom_value_matches(atom, text)
    return any(
        _atom_value_matches(atom, role_scope)
        for role_scope in _role_scoped_surfaces(atom.role, text)
    )


def _atom_coverage(
    atoms: list[GoldAtom],
    text: Any,
    *,
    required_only: bool = False,
) -> tuple[float | None, list[bool]]:
    if not atoms:
        return None, []
    matches = [_atom_matches(atom, text) for atom in atoms]
    indexes = [
        index
        for index, atom in enumerate(atoms)
        if atom.required or not required_only
    ]
    if not indexes:
        return None, matches
    return sum(matches[index] for index in indexes) / len(indexes), matches


def _token_coverage(key_tokens: list[str], text: Any) -> float | None:
    if not key_tokens:
        return None
    present = set(_norm_tokens(text))
    hit = sum(1 for tok in key_tokens if tok in present)
    return hit / len(key_tokens)


def _short(text: Any, limit: int = 90) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _score_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in report.get("scores", []) or []
        if isinstance(item, dict)
    }


def _phrase_pattern_matches(pattern: dict[str, list[str]], text: Any) -> bool:
    plain = normalize_text(text)
    if not plain:
        return False
    if any(phrase not in plain for phrase in pattern.get("all_of", [])):
        return False
    any_of = pattern.get("any_of", [])
    if any_of and not any(phrase in plain for phrase in any_of):
        return False
    return not any(phrase in plain for phrase in pattern.get("none_of", []))


def _phrase_patterns_match(
    patterns: list[dict[str, list[str]]],
    text: Any,
) -> bool:
    return any(_phrase_pattern_matches(pattern, text) for pattern in patterns)


def _phrase_patterns_match_contexts(
    patterns: list[dict[str, list[str]]],
    contexts: list[Any],
) -> bool:
    surfaces = [
        surface
        for context in contexts
        if (surface := narrative_evidence_surface(context))
    ]
    if any(_phrase_patterns_match(patterns, surface) for surface in surfaces):
        return True
    # Some reviewed propositions legitimately span atomized siblings from one
    # evidence requirement.  Joined matching is a recall fallback; contracts
    # should still make each phrase specific enough to avoid unrelated joins.
    return _phrase_patterns_match(patterns, "\n".join(surfaces))


def _semantic_evidence_contexts_match(
    semantic: str,
    contexts: list[Any],
) -> bool:
    if not semantic:
        return True
    surfaces = [
        surface
        for context in contexts
        if (surface := narrative_evidence_surface(context))
    ]
    if semantic == "listing_event":
        return any(is_listing_event_text(surface) for surface in surfaces)
    return False


def _evaluate_narrative_contract(
    contract: dict[str, Any],
    *,
    answer: str,
    contexts: list[Any],
) -> dict[str, Any]:
    grounded_sections = split_grounded_answer_sections(answer)
    propositions = []
    for proposition in contract["propositions"]:
        if grounded_sections is None:
            answer_surface = answer
            answer_section = "whole_answer"
        elif proposition["support"] == "bounded_inference":
            answer_surface = grounded_sections["inference"]
            answer_section = "inference"
        elif proposition["support"] == "explicit" and (
            proposition["required_in_evidence"]
            or proposition["required_in_answer"]
        ):
            answer_surface = grounded_sections["fact"]
            answer_section = "fact"
        else:
            answer_surface = answer
            answer_section = "whole_answer"
        answer_match = _phrase_patterns_match(
            proposition["patterns"],
            answer_surface,
        )
        if (
            answer_match
            and answer_section == "fact"
            and proposition["evidence_semantic"]
        ):
            answer_match = _semantic_evidence_contexts_match(
                proposition["evidence_semantic"],
                [answer_surface],
            )
        evidence_match = (
            _phrase_patterns_match_contexts(
                proposition["evidence_patterns"],
                contexts,
            )
            and _semantic_evidence_contexts_match(
                proposition["evidence_semantic"],
                contexts,
            )
        )
        propositions.append(
            {
                **proposition,
                "answer_match": answer_match,
                "evidence_match": evidence_match,
                "answer_section": answer_section,
            }
        )

    required_evidence = [
        item for item in propositions if item["required_in_evidence"]
    ]
    anchors_complete = bool(required_evidence) and all(
        item["evidence_match"] for item in required_evidence
    )
    grounded_anchors_complete = anchors_complete and all(
        item["evidence_match"]
        and (
            grounded_sections is None
            or not item["required_in_answer"]
            or item["answer_match"]
        )
        for item in required_evidence
    )
    for item in propositions:
        if item["support"] == "bounded_inference":
            support_match = anchors_complete
        elif item["support"] == "external_required":
            support_match = _phrase_patterns_match_contexts(
                item["support_patterns"],
                contexts,
            )
        else:
            support_match = item["evidence_match"]
        item["support_match"] = support_match

    required_answer = [
        item for item in propositions if item["required_in_answer"]
    ]
    missing_required_answer = [
        item for item in required_answer if not item["answer_match"]
    ]
    missing_required_evidence = [
        item for item in required_evidence if not item["evidence_match"]
    ]
    unsupported = [
        item
        for item in propositions
        if item["answer_match"]
        and (item["required_in_answer"] or item["validate_if_present"])
        and not item["support_match"]
    ]
    prohibited = [
        item
        for item in propositions
        if item["prohibited"] and item["answer_match"]
    ]
    answer_coverage = (
        sum(item["answer_match"] for item in required_answer)
        / len(required_answer)
    )
    context_coverage = (
        sum(item["evidence_match"] for item in required_evidence)
        / len(required_evidence)
        if required_evidence
        else None
    )
    return {
        "propositions": propositions,
        "anchors_complete": anchors_complete,
        "grounded_anchors_complete": grounded_anchors_complete,
        "missing_required_answer": missing_required_answer,
        "missing_required_evidence": missing_required_evidence,
        "unsupported": unsupported,
        "prohibited": prohibited,
        "answer_coverage": answer_coverage,
        "context_coverage": context_coverage,
    }


def _narrative_root(
    evaluation: dict[str, Any],
    *,
    refusal: bool = False,
) -> str:
    if evaluation["missing_required_evidence"]:
        return "RETRIEVAL_MISS"
    if evaluation["unsupported"] or evaluation["prohibited"]:
        return "SYNTHESIS_MISS"
    missing_answer = evaluation["missing_required_answer"]
    if missing_answer and all(item["support_match"] for item in missing_answer):
        return "SYNTHESIS_MISS"
    if refusal and evaluation["anchors_complete"]:
        return "SYNTHESIS_MISS"
    return "RETRIEVAL_MISS"


def _classify_narrative_contract(
    result: dict[str, Any],
    *,
    contract: dict[str, Any],
    answer: str,
    contexts: list[Any],
    refuses_requested_slot: bool,
    score: dict[str, Any] | None,
) -> dict[str, Any]:
    evaluation = _evaluate_narrative_contract(
        contract,
        answer=answer,
        contexts=contexts,
    )
    public_props = [
        {
            key: item[key]
            for key in (
                "name",
                "support",
                "evidence_semantic",
                "required_in_evidence",
                "required_in_answer",
                "optional",
                "validate_if_present",
                "prohibited",
                "answer_match",
                "evidence_match",
                "answer_section",
                "support_match",
            )
        }
        for item in evaluation["propositions"]
    ]
    result["evidence"] = {
        "fact_kind": "narrative_propositions",
        "contract_source": "explicit_narrative",
        "expected": public_props,
        "answer_coverage": round(evaluation["answer_coverage"], 3),
        "context_coverage": (
            None
            if evaluation["context_coverage"] is None
            else round(evaluation["context_coverage"], 3)
        ),
        "anchors_complete": evaluation["anchors_complete"],
        "grounded_anchors_complete": evaluation[
            "grounded_anchors_complete"
        ],
        "missing_required_answer": [
            item["name"] for item in evaluation["missing_required_answer"]
        ],
        "missing_required_evidence": [
            item["name"] for item in evaluation["missing_required_evidence"]
        ],
        "unsupported_propositions": [
            item["name"] for item in evaluation["unsupported"]
        ],
        "prohibited_propositions": [
            item["name"] for item in evaluation["prohibited"]
        ],
    }

    if refuses_requested_slot and evaluation["answer_coverage"] < 1.0:
        result["label"] = "WRONG_REFUSAL"
        result["root_cause"] = _narrative_root(evaluation, refusal=True)
        result["detail"] = (
            "answer refuses although reviewed narrative anchors support "
            "a bounded answer"
        )
        return result

    if (
        evaluation["missing_required_answer"]
        or evaluation["missing_required_evidence"]
        or evaluation["unsupported"]
        or evaluation["prohibited"]
    ):
        result["label"] = "WRONG_OVERLAP"
        result["root_cause"] = _narrative_root(evaluation)
        reasons = []
        for key, label in (
            ("missing_required_answer", "missing answer propositions"),
            ("missing_required_evidence", "missing evidence propositions"),
            ("unsupported", "unsupported propositions"),
            ("prohibited", "prohibited propositions"),
        ):
            if evaluation[key]:
                reasons.append(
                    f"{label}="
                    + ",".join(item["name"] for item in evaluation[key])
                )
        result["detail"] = "; ".join(reasons)
        return result

    return _apply_soft(result, score)


def classify(prediction: dict[str, Any], score: dict[str, Any] | None) -> dict[str, Any]:
    pid = prediction.get("id")
    question = str(prediction.get("question", "") or "")
    answer = str(prediction.get("answer", "") or "")
    ground_truth = str(prediction.get("ground_truth", "") or "")
    contexts = prediction.get("retrieved_contexts") or []
    ctx_blob = "\n".join(str(c) for c in contexts)
    errors = prediction.get("errors")
    synth_status = str(prediction.get("synth_status", "") or "")
    structured_abstention = _structured_abstention_reason(prediction)
    benchmark_issues_raw = prediction.get("benchmark_issues", []) or []
    if not isinstance(benchmark_issues_raw, list) or any(
        not isinstance(issue, str) for issue in benchmark_issues_raw
    ):
        raise ValueError("prediction benchmark_issues must be a string list")
    benchmark_issues = list(
        dict.fromkeys(
            issue.strip()
            for issue in benchmark_issues_raw
            if issue.strip()
        )
    )
    provider_failure = provider_limit_reason(
        "\n".join(
            [
                answer,
                synth_status,
                *(
                    str(item)
                    for item in (errors if isinstance(errors, list) else [errors])
                    if item
                ),
            ]
        )
    )

    result: dict[str, Any] = {
        "id": pid,
        "question": _short(question),
        "ground_truth": _short(ground_truth),
        "answer": _short(answer),
        "analytical": is_analytical(question),
        "label": "OK",
        "root_cause": None,
        "review": False,
        "verified_correct": False,
        "evidence": {},
        "detail": "",
        "benchmark_issues": benchmark_issues,
        "benchmark_issue": benchmark_issues[0] if benchmark_issues else "",
    }

    # 1) Hard pipeline failures.
    if (
        errors
        or not answer.strip()
        or synth_status.lower() in {"error", "failed", "timeout"}
        or provider_failure
    ):
        result["label"] = "ERROR"
        result["root_cause"] = "INFRA_ERROR"
        result["detail"] = (
            f"provider_failure={provider_failure}"
            if provider_failure
            else f"errors={json.dumps(errors, ensure_ascii=False)} synth_status={synth_status!r}"
            if errors else f"empty_answer synth_status={synth_status!r}"
        )
        return result

    gt_refusal = _is_refusal(ground_truth) and not _is_zero_balance_fact(ground_truth)
    # The ground truth is itself a non-answer -> the seed record, not the model,
    # is the thing to review; a matching refusal from the model is actually right.
    if gt_refusal:
        result["root_cause"] = "GT_DESIGN"
        result["detail"] = "ground_truth is itself a refusal / non-answer"
        if not (_is_refusal(answer) or structured_abstention):
            result["review"] = True
        return _apply_soft(result, score)

    answer_refusal = bool(_is_refusal(answer) or structured_abstention)
    narrative_raw = prediction.get("narrative_contract")
    if narrative_raw is not None:
        narrative_contract = _canonical_narrative_contract(narrative_raw)
        return _classify_narrative_contract(
            result,
            contract=narrative_contract,
            answer=answer,
            contexts=contexts,
            refuses_requested_slot=answer_refusal,
            score=score,
        )

    atoms, atom_source = gold_atoms_for_prediction(prediction)

    key_tokens = [] if atoms else _key_gt_tokens(question, ground_truth)
    if atoms:
        ans_cov, answer_matches = _atom_coverage(
            atoms,
            answer,
            required_only=True,
        )
        context_matches = [
            any(_atom_matches(atom, context) for context in contexts)
            for atom in atoms
        ]
        required_indexes = [
            index for index, atom in enumerate(atoms) if atom.required
        ]
        ctx_cov = (
            sum(context_matches[index] for index in required_indexes)
            / len(required_indexes)
        )
        all_answer_cov, _ = _atom_coverage(atoms, answer)
        all_context_cov = (
            sum(context_matches) / len(context_matches)
            if context_matches
            else None
        )
        kinds = sorted({atom.kind for atom in atoms})
        fact_kind = kinds[0] if len(kinds) == 1 else "typed_atoms"
    else:
        ans_cov = _token_coverage(key_tokens, answer)
        ctx_cov = _token_coverage(key_tokens, ctx_blob)
        answer_matches = []
        context_matches = []
        all_answer_cov = ans_cov
        all_context_cov = ctx_cov
        fact_kind = "entity"

    result["evidence"] = {
        "fact_kind": fact_kind,
        "contract_source": atom_source if atoms else "derived_entity_tokens",
        "expected": [atom.as_dict() for atom in atoms] if atoms else key_tokens,
        "answer_coverage": None if ans_cov is None else round(ans_cov, 3),
        "context_coverage": None if ctx_cov is None else round(ctx_cov, 3),
    }
    if atoms:
        result["evidence"]["answer_matches"] = answer_matches
        result["evidence"]["context_matches"] = context_matches
        result["evidence"]["required_atoms_n"] = sum(
            1 for atom in atoms if atom.required
        )
        result["evidence"]["optional_atoms_n"] = sum(
            1 for atom in atoms if not atom.required
        )
        result["evidence"]["all_answer_coverage"] = (
            None if all_answer_cov is None else round(all_answer_cov, 3)
        )
        result["evidence"]["all_context_coverage"] = (
            None if all_context_cov is None else round(all_context_cov, 3)
        )

    # 2) Refusal despite an answerable ground truth (high confidence).
    # A caveat about an auxiliary field is not a refusal of the requested slot
    # when every mandatory atom is already present in the answer.
    refuses_requested_slot = answer_refusal and not (
        atoms and ans_cov == 1.0
    )
    if refuses_requested_slot:
        result["label"] = "WRONG_REFUSAL"
        result["root_cause"] = (
            _root_for_missing_atoms(
                [
                    index
                    for index, atom in enumerate(atoms)
                    if atom.required
                ],
                context_matches,
            )
            if atoms
            else _root_from_context(ctx_cov)
        )
        result["detail"] = (
            "answer refuses although ground_truth provides an answer"
            + (
                f"; structured_abstention={structured_abstention}"
                if structured_abstention
                else ""
            )
        )
        return result

    # 3) No verifiable gold fact (analytical / unextractable) -> soft only.
    if ans_cov is None:
        result["detail"] = "no extractable gold fact (analytical/text) -> RAGAS-soft only"
        return _apply_soft(result, score)

    # 4) A reviewed atom contract is exact: partial coverage is also a hard miss.
    # For legacy, automatically-derived analytical answers remain a soft review.
    contract_miss = atom_source == "explicit" and ans_cov < 1.0
    deterministic_miss = ans_cov == 0.0 and not result["analytical"]
    if contract_miss or deterministic_miss:
        missing_indexes = [
            index
            for index, (atom, matched) in enumerate(
                zip(atoms, answer_matches, strict=True)
            )
            if atom.required and not matched
        ]
        missing_atoms = [atoms[index] for index in missing_indexes]
        result["label"] = (
            "WRONG_NUMBER"
            if any(atom.kind in QUANTITATIVE_ATOM_KINDS for atom in missing_atoms)
            else "WRONG_OVERLAP"
        )
        result["root_cause"] = _root_for_missing_atoms(
            missing_indexes,
            context_matches,
        )
        qualifier = "not all" if contract_miss and ans_cov > 0.0 else "none"
        result["detail"] = f"answer contains {qualifier} of the gold {fact_kind}(s)"
        return result

    # 5) Partial coverage -> not a hard verdict, flag for review.
    if ans_cov < 1.0:
        result["review"] = True
        result["detail"] = f"partial gold-{fact_kind} coverage {ans_cov:.2f}"

    return _apply_soft(result, score)


def _root_from_context(ctx_cov: float | None) -> str:
    if ctx_cov is not None and ctx_cov >= COVERAGE_PRESENT:
        return "SYNTHESIS_MISS"
    return "RETRIEVAL_MISS"


def _root_for_missing_atoms(
    missing_indexes: list[int],
    context_matches: list[bool],
) -> str:
    """Attribute synth only when every answer-missing required atom was retrieved."""

    if missing_indexes and all(
        index < len(context_matches) and context_matches[index]
        for index in missing_indexes
    ):
        return "SYNTHESIS_MISS"
    return "RETRIEVAL_MISS"


def _apply_soft(result: dict[str, Any], score: dict[str, Any] | None) -> dict[str, Any]:
    evidence = result.get("evidence") or {}
    result["verified_correct"] = (
        result.get("label") == "OK"
        and result.get("root_cause") != "GT_DESIGN"
        and evidence.get("answer_coverage") == 1.0
    )
    if not score:
        return result
    weak = []
    for metric in ("answer_relevancy", "faithfulness"):
        value = score.get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < SOFT_THRESHOLD:
            weak.append(f"{metric}={value:.2f}")
    if weak:
        result["review"] = True
        extra = "RAGAS-soft: " + ", ".join(weak)
        result["detail"] = f"{result['detail']}; {extra}" if result["detail"] else extra
    return result


def is_verified_recovery(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A refusal flip is a recovery only when the new answer fully matches gold."""
    return before.get("label") != "OK" and after.get("verified_correct") is True


def evaluate_reviewed_hard_error_gate(
    cases: list[dict[str, Any]],
    *,
    threshold: float = HARD_ERROR_GATE_THRESHOLD,
) -> dict[str, Any]:
    """Measure hard-error precision/recall and exact labels on reviewed pairs.

    Each reviewed case supplies a ``correct_answer`` (expected ``OK``) and a
    ``wrong_answer`` with its reviewed ``wrong_label``.  Evaluating both classes
    prevents a mark-everything-wrong evaluator from passing on recall alone.
    Exact-label accuracy also prevents WRONG_REFUSAL/WRONG_NUMBER confusion from
    disappearing inside the same binary hard-error bucket.
    """

    if isinstance(threshold, bool) or not 0.0 <= float(threshold) <= 1.0:
        raise ValueError("hard-error gate threshold must be between 0 and 1")
    if not isinstance(cases, list) or not cases:
        raise ValueError("hard-error gate requires reviewed cases")

    true_positive = false_positive = false_negative = true_negative = 0
    exact_label_matches = 0
    reviewed_samples = 0
    mismatches: list[dict[str, Any]] = []

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"reviewed case {index} must be an object")
        missing = [
            key
            for key in (
                "question",
                "ground_truth",
                "gold_atoms",
                "correct_answer",
                "wrong_answer",
                "wrong_label",
            )
            if key not in case
        ]
        if missing:
            raise ValueError(
                f"reviewed case {index} is missing: {', '.join(missing)}"
            )

        samples = (
            ("correct_answer", "OK"),
            ("wrong_answer", str(case["wrong_label"] or "").strip()),
        )
        for answer_key, expected_label in samples:
            if expected_label not in HARD_ERROR_LABELS | {"OK"}:
                raise ValueError(
                    f"reviewed case {index} has unsupported label "
                    f"{expected_label!r}"
                )
            prediction = {
                "question": case["question"],
                "ground_truth": case["ground_truth"],
                "answer": case[answer_key],
                "gold_atoms": case["gold_atoms"],
                "retrieved_contexts": case.get("retrieved_contexts", []),
            }
            actual_label = str(classify(prediction, None)["label"])
            expected_hard = expected_label in HARD_ERROR_LABELS
            actual_hard = actual_label in HARD_ERROR_LABELS
            reviewed_samples += 1
            exact_label_matches += int(actual_label == expected_label)

            if expected_hard and actual_hard:
                true_positive += 1
            elif not expected_hard and actual_hard:
                false_positive += 1
            elif expected_hard and not actual_hard:
                false_negative += 1
            else:
                true_negative += 1

            if actual_label != expected_label:
                mismatches.append(
                    {
                        "name": str(case.get("name", index)),
                        "answer_key": answer_key,
                        "expected_label": expected_label,
                        "actual_label": actual_label,
                    }
                )

    expected_positive = true_positive + false_negative
    expected_negative = true_negative + false_positive
    if not expected_positive or not expected_negative:
        raise ValueError(
            "hard-error gate requires reviewed hard-error and reviewed OK samples"
        )
    predicted_positive = true_positive + false_positive
    precision = (
        true_positive / predicted_positive
        if predicted_positive
        else 0.0
    )
    recall = true_positive / expected_positive
    exact_label_accuracy = exact_label_matches / reviewed_samples
    threshold = float(threshold)
    return {
        "status": (
            "pass"
            if (
                precision >= threshold
                and recall >= threshold
                and exact_label_accuracy >= threshold
            )
            else "fail"
        ),
        "threshold": threshold,
        "reviewed_samples_n": reviewed_samples,
        "expected_hard_errors_n": expected_positive,
        "expected_ok_n": expected_negative,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "exact_label_accuracy": round(exact_label_accuracy, 6),
        "mismatches": mismatches,
    }


def load_adjudications(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Load reviewed seed dispositions without baking question IDs into code."""

    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("adjudication file must contain a records list")

    output: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict) or raw.get("id") in ("", None):
            raise ValueError("every adjudication record requires an id")
        disposition = str(raw.get("disposition", "") or "").strip()
        if disposition not in {"include", "exclude_product_error_baseline"}:
            raise ValueError(
                "adjudication disposition must be include or "
                "exclude_product_error_baseline"
            )
        key = str(raw["id"])
        if key in output:
            raise ValueError(f"duplicate adjudication id: {key}")
        output[key] = {
            "disposition": disposition,
            "reason": str(raw.get("reason", "") or "").strip(),
        }
    return output


def analyze(
    path: str | Path,
    *,
    adjudications: dict[str, dict[str, Any]] | None = None,
    gold_contract: GoldAtomContract | None = None,
) -> dict[str, Any]:
    report_path = Path(path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    predictions = [p for p in report.get("predictions", []) or [] if isinstance(p, dict)]
    scores = _score_map(report)
    report_dataset_id = _report_dataset_id(report)
    if (
        gold_contract
        and gold_contract.dataset_id
        and report_dataset_id
        and _dataset_key(gold_contract.dataset_id) != _dataset_key(report_dataset_id)
    ):
        raise ValueError(
            f"gold-atom contract dataset {gold_contract.dataset_id!r} does not "
            f"match report dataset {report_dataset_id!r}"
        )

    contract_matches = 0
    prepared_predictions = []
    for prediction in predictions:
        prepared = prediction
        matched = False
        if gold_contract is not None:
            prepared, matched = _bind_gold_atom_contract(
                prediction,
                gold_contract,
                report_dataset_id=report_dataset_id,
            )
        prepared_predictions.append(prepared)
        contract_matches += int(matched)
    rows = [
        classify(pred, scores.get(str(pred.get("id"))))
        for pred in prepared_predictions
    ]
    adjudications = adjudications or {}
    for row in rows:
        adjudication = adjudications.get(str(row.get("id")), {})
        disposition = str(adjudication.get("disposition", "include") or "include")
        row["baseline_disposition"] = disposition
        row["adjudication_reason"] = str(adjudication.get("reason", "") or "")

    label_counts: dict[str, int] = {}
    cause_counts: dict[str, int] = {}
    baseline_label_counts: dict[str, int] = {}
    baseline_cause_counts: dict[str, int] = {}
    benchmark_issue_counts: dict[str, int] = {}
    excluded_rows = []
    for row in rows:
        for issue in row.get("benchmark_issues", []) or []:
            benchmark_issue_counts[issue] = benchmark_issue_counts.get(issue, 0) + 1
        label_counts[row["label"]] = label_counts.get(row["label"], 0) + 1
        if row["label"] != "OK" and row["root_cause"]:
            cause_counts[row["root_cause"]] = cause_counts.get(row["root_cause"], 0) + 1
        if row["baseline_disposition"] == "exclude_product_error_baseline":
            excluded_rows.append(row)
            continue
        baseline_label_counts[row["label"]] = (
            baseline_label_counts.get(row["label"], 0) + 1
        )
        if row["label"] != "OK" and row["root_cause"]:
            baseline_cause_counts[row["root_cause"]] = (
                baseline_cause_counts.get(row["root_cause"], 0) + 1
            )
    review_only = sum(1 for row in rows if row["label"] == "OK" and row["review"])

    summary = {
        "path": str(report_path),
        "total": len(rows),
        "label_counts": label_counts,
        "cause_counts": cause_counts,
        "benchmark_issue_counts": benchmark_issue_counts,
        "review_only": review_only,
        "product_error_baseline": {
            "total": len(rows) - len(excluded_rows),
            "excluded_n": len(excluded_rows),
            "excluded_ids": [row.get("id") for row in excluded_rows],
            "label_counts": baseline_label_counts,
            "cause_counts": baseline_cause_counts,
        },
        "rows": rows,
    }
    if gold_contract is not None:
        summary["gold_atom_contract"] = {
            "path": gold_contract.path,
            "schema_version": gold_contract.schema_version,
            "dataset_id": gold_contract.dataset_id,
            "records_n": len(gold_contract.entries),
            "matched_predictions_n": contract_matches,
            "unmatched_predictions_n": len(predictions) - contract_matches,
        }
    return summary


LABEL_ORDER = ("ERROR", "WRONG_REFUSAL", "WRONG_NUMBER", "WRONG_OVERLAP", "OK")
CAUSE_ORDER = ("INFRA_ERROR", "RETRIEVAL_MISS", "SYNTHESIS_MISS", "GT_DESIGN")


def _print_summary(summary: dict[str, Any]) -> None:
    print("=" * 92)
    print(f"{summary['path']}  (n={summary['total']})")
    labels = summary["label_counts"]
    print("labels:   " + "  ".join(
        f"{name}={labels.get(name, 0)}" for name in LABEL_ORDER if labels.get(name)
    ) + (f"  REVIEW_only={summary['review_only']}" if summary["review_only"] else ""))
    causes = summary["cause_counts"]
    if causes:
        print("causes:   " + "  ".join(
            f"{name}={causes.get(name, 0)}" for name in CAUSE_ORDER if causes.get(name)
        ))
    baseline = summary.get("product_error_baseline", {}) or {}
    if baseline.get("excluded_n"):
        baseline_labels = baseline.get("label_counts", {}) or {}
        print(
            "product baseline: "
            + "  ".join(
                f"{name}={baseline_labels.get(name, 0)}"
                for name in LABEL_ORDER
                if baseline_labels.get(name)
            )
            + f"  excluded_reviewed={baseline['excluded_n']}"
        )
    flagged = [r for r in summary["rows"] if r["label"] != "OK" or r["review"]]
    if flagged:
        print("-" * 92)
        for row in flagged:
            tag = row["label"] if row["label"] != "OK" else "REVIEW"
            cause = f"/{row['root_cause']}" if row["root_cause"] else ""
            print(f"  id {row['id']:>3} [{tag}{cause}]")
            print(f"       Q : {row['question']}")
            print(f"       GT: {row['ground_truth']}")
            print(f"       A : {row['answer']}")
            if row["detail"]:
                print(f"       -> {row['detail']}")


def _write_markdown(summary: dict[str, Any], md_path: Path) -> None:
    lines = [
        f"# Triage lỗi prediction — {Path(summary['path']).name}",
        "",
        f"Tổng: **{summary['total']}** câu.",
        "",
        "| Nhãn | Số câu |",
        "|---|---|",
    ]
    for name in LABEL_ORDER:
        if summary["label_counts"].get(name):
            lines.append(f"| {name} | {summary['label_counts'][name]} |")
    if summary["review_only"]:
        lines.append(f"| REVIEW (OK nhưng RAGAS thấp) | {summary['review_only']} |")
    lines += ["", "## Nguyên nhân gốc (các câu lỗi)", "", "| Root cause | Số câu |", "|---|---|"]
    for name in CAUSE_ORDER:
        if summary["cause_counts"].get(name):
            lines.append(f"| {name} | {summary['cause_counts'][name]} |")
    baseline = summary.get("product_error_baseline", {}) or {}
    if baseline.get("excluded_n"):
        lines += [
            "",
            "## Product-error baseline đã adjudicate",
            "",
            f"Loại **{baseline['excluded_n']}** false-label đã review; "
            f"baseline còn **{baseline['total']}** câu.",
            "",
            "| Nhãn | Số câu |",
            "|---|---|",
        ]
        for name in LABEL_ORDER:
            if baseline.get("label_counts", {}).get(name):
                lines.append(
                    f"| {name} | {baseline['label_counts'][name]} |"
                )
    lines += ["", "## Chi tiết câu lỗi / cần rà soát", ""]
    for row in summary["rows"]:
        if row["label"] == "OK" and not row["review"]:
            continue
        tag = row["label"] if row["label"] != "OK" else "REVIEW"
        cause = f" / {row['root_cause']}" if row["root_cause"] else ""
        lines += [
            f"### id {row['id']} — {tag}{cause}",
            f"- **Q:** {row['question']}",
            f"- **GT:** {row['ground_truth']}",
            f"- **A:** {row['answer']}",
        ]
        if row["evidence"]:
            ev = row["evidence"]
            lines.append(
                f"- **Bằng chứng:** {ev.get('fact_kind')} | "
                f"answer_cov={ev.get('answer_coverage')} ctx_cov={ev.get('context_coverage')}"
            )
        if row["detail"]:
            lines.append(f"- **Ghi chú:** {row['detail']}")
        if row.get("baseline_disposition") == "exclude_product_error_baseline":
            reason = str(row.get("adjudication_reason", "") or "").strip()
            lines.append(
                "- **Adjudication:** loại khỏi product-error baseline"
                + (f" — {reason}" if reason else "")
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="Prediction report JSON files")
    parser.add_argument(
        "--out-dir",
        default="ragas_runs",
        help="Directory for the <stem>_error_triage.md/.json reports.",
    )
    parser.add_argument(
        "--adjudications",
        default="",
        help=(
            "Optional reviewed JSON contract whose records mark seed IDs as "
            "include or exclude_product_error_baseline."
        ),
    )
    parser.add_argument(
        "--gold-contract",
        default="",
        help=(
            "Optional reviewed typed gold-atom JSON contract. Records are "
            "matched by dataset/id and exact normalized question."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = args.paths or ["ragas_runs/vnm_predictions.json"]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    adjudications = load_adjudications(args.adjudications)
    gold_contract = load_gold_atom_contract(args.gold_contract)
    for path in paths:
        summary = analyze(
            path,
            adjudications=adjudications,
            gold_contract=gold_contract,
        )
        _print_summary(summary)
        stem = Path(path).stem
        md_path = out_dir / f"{stem}_error_triage.md"
        json_path = out_dir / f"{stem}_error_triage.json"
        _write_markdown(summary, md_path)
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport -> {md_path}\n         {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
