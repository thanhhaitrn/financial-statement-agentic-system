"""Deterministic consistency checks for synthesized financial answers.

The language model is allowed to explain financial facts, but signs, units and
evidence availability are data contracts.  These helpers keep those contracts
outside the prompt so a fluent answer cannot silently reverse a cash-flow sign,
shrink a VND amount by three orders of magnitude, or claim that a retrieved
metric is missing.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Mapping
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any

from schemas.numbers import parse_financial_decimal
from schemas.evidence_origin import is_report_fact
from tools.query_routing import (
    fact_reporting_basis,
    reporting_basis_compatible,
)


_FOUND_STATUSES = {"", "found", "matched", "ok", "success"}
_MISSING_MARKERS = (
    "chua du du lieu",
    "khong du du lieu",
    "khong co du lieu",
    "khong co so lieu",
    "thieu du lieu",
    "thieu so lieu",
    "du lieu khong co",
    "so lieu khong co",
    "khong tim thay",
    "khong duoc ghi nhan",
    "khong the tinh",
    "chua the tinh",
    "khong tinh duoc",
)
_NEGATIVE_CASHFLOW_MARKERS = (
    "dong tien am",
    "dong tien ra",
    "chi ra rong",
    "chi tien rong",
    "luu chuyen am",
    "cfi am",
    "cff am",
    "cfo am",
)
_POSITIVE_CASHFLOW_MARKERS = (
    "dong tien duong",
    "dong tien vao",
    "thu vao rong",
    "thu tien rong",
    "luu chuyen duong",
    "cfi duong",
    "cff duong",
    "cfo duong",
)
_COMPACT_AMOUNT_RE = re.compile(
    r"(?<!\w)[+-]?\d+(?:[.,]\d+)?\s*"
    r"(?:nghin\s+ty|ty|trieu)\s*(?:vnd|dong)?\b"
)

_CASHFLOW_ALIASES = {
    "cfo": (
        "cfo",
        "luu chuyen tien thuan tu hoat dong kinh doanh",
        "dong tien thuan tu hoat dong kinh doanh",
        "dong tien tu hoat dong kinh doanh",
    ),
    "cfi": (
        "cfi",
        "luu chuyen tien thuan tu hoat dong dau tu",
        "dong tien thuan tu hoat dong dau tu",
        "dong tien tu hoat dong dau tu",
    ),
    "cff": (
        "cff",
        "luu chuyen tien thuan tu hoat dong tai chinh",
        "luu chuyen thuan tu hoat dong tai chinh",
        "dong tien thuan tu hoat dong tai chinh",
        "dong tien tu hoat dong tai chinh",
    ),
    "net_cash_flow": (
        "luu chuyen tien thuan trong nam",
        "luu chuyen tien thuan trong ky",
    ),
}

_METRIC_ALIASES = {
    "net_profit": (
        "loi nhuan sau thue tndn",
        "loi nhuan sau thue thu nhap doanh nghiep",
        "loi nhuan sau thue",
        "lai rong",
        "net profit",
    ),
    "net_revenue": ("doanh thu thuan", "doanh thu ban hang va cung cap dich vu"),
    "cogs": ("gia von hang ban", "gia von hang ban va dich vu cung cap"),
    "total_assets": ("tong tai san", "tong cong tai san"),
    "total_liabilities": ("no phai tra", "tong no"),
    "equity": ("von chu so huu",),
    "current_assets": ("tai san ngan han",),
    "current_liabilities": ("no ngan han",),
    "inventory": ("hang ton kho",),
    "receivables": ("cac khoan phai thu ngan han", "phai thu ngan han"),
    "interest_expense": ("chi phi lai vay",),
    "capex": ("capex", "tien chi mua tai san co dinh", "chi dau tu tai san co dinh"),
    "borrowings": ("tien thu tu di vay", "vay moi"),
    "debt_repayment": ("tien chi tra no goc vay", "tra no vay"),
    "dividends": ("tien chi tra co tuc", "co tuc da tra", "chi tra co tuc"),
    **_CASHFLOW_ALIASES,
}

_NET_PROFIT_FACT_ALIASES = (
    "loi nhuan sau thue tndn",
    "loi nhuan sau thue thu nhap doanh nghiep",
)
_NET_REVENUE_FACT_ALIASES = (
    "doanh thu thuan ve ban hang va cung cap dich vu",
    "doanh thu thuan",
)
_CURRENT_ASSETS_FACT_ALIASES = ("tong tai san ngan han",)
_CURRENT_LIABILITIES_FACT_ALIASES = ("tong no ngan han",)
_INVENTORY_FACT_ALIASES = ("hang ton kho",)
_CASH_FACT_ALIASES = (
    "tien va cac khoan tuong duong tien",
    "tien va tuong duong tien",
)
_TOTAL_LIABILITIES_FACT_ALIASES = ("tong no phai tra",)
_NET_PROFIT_NAME_ALIASES = ("lai rong", "net profit")
_PROXY_MARKERS = (
    "proxy",
    "uoc luong",
    "dai dien cho",
    "thay the cho",
    "xap xi",
)
_SEPARATE_STATEMENT_MARKERS = (
    "bao cao tai chinh rieng",
    "bao cao ket qua hoat dong kinh doanh rieng",
    "bao cao tinh hinh tai chinh rieng",
    "cong ty me",
    "congtyme",
    "standalone",
    "separate financial statement",
)
_AVERAGE_BASIS_MARKERS = (
    "binh quan dau cuoi",
    "binh quan dau ky va cuoi ky",
    "binh quan dau nam va cuoi nam",
    "tai san binh quan",
    "von chu so huu binh quan",
    "average assets",
    "average equity",
)
_ENDING_BASIS_MARKERS = (
    "so cuoi ky",
    "so cuoi nam",
    "tai san cuoi ky",
    "tai san cuoi nam",
    "von chu so huu cuoi ky",
    "von chu so huu cuoi nam",
    "ending balance",
    "closing balance",
)
_SIMPLIFIED_RATIO_MARKERS = (
    "don gian hoa",
    "cach tinh don gian",
    "chi so so bo",
    "ty le so bo",
    "simplified",
)
_NET_MARGIN_ALIASES = (
    "bien loi nhuan rong",
    "bien rong",
    "ty suat loi nhuan rong",
    "ty le loi nhuan rong",
    "net profit margin",
    "net margin",
)
_ASSET_TURNOVER_ALIASES = (
    "vong quay tai san",
    "hieu suat su dung tai san",
    "asset turnover",
)
_NET_REVENUE_DEPENDENT_CALCULATION_ALIASES = (
    "bien loi nhuan",
    "bien gop",
    "bien hoat dong",
    "bien rong",
    "ty suat loi nhuan",
    "net margin",
    *_ASSET_TURNOVER_ALIASES,
)
_OPERATING_PROFIT_ALIASES = (
    "loi nhuan thuan tu hoat dong kinh doanh",
    "loi nhuan truoc thay doi von luu dong",
)
_OPERATING_PROFIT_FACT_ALIASES = (
    "loi nhuan thuan tu hoat dong kinh doanh",
)
_GROSS_PROFIT_ALIASES = (
    "loi nhuan gop ve ban hang va cung cap dich vu",
    "loi nhuan gop",
)
_GROSS_MARGIN_ALIASES = (
    "bien loi nhuan gop",
    "bien gop",
    "gross margin",
)
_OPERATING_MARGIN_ALIASES = (
    "bien loi nhuan hoat dong",
    "bien hoat dong",
    "operating margin",
)
_INCOME_STATEMENT_FLOW_ALIASES = {
    "net_revenue": (
        *_NET_REVENUE_FACT_ALIASES,
        "doanh thu ban hang va cung cap dich vu",
        "doanh thu hoat dong tai chinh",
    ),
    "gross_profit": _GROSS_PROFIT_ALIASES,
    "operating_profit": _OPERATING_PROFIT_FACT_ALIASES,
    "net_profit": (
        *_NET_PROFIT_FACT_ALIASES,
        "loi nhuan sau thue",
        "pat",
        "lnst",
    ),
}
_EBIT_NEGATION_MARKERS = (
    "khong phai ebit",
    "khong dong nhat voi ebit",
    "khong tu dong dong nhat",
)
_OPERATING_NUMERATOR_NEGATION_MARKERS = (
    "khong dung",
    "khong lay",
    "khong phai tu so",
    "khong duoc dung",
    "khong su dung",
)
_OPERATING_NUMERATOR_USE_MARKERS = (
    "tu so",
    "su dung",
    "dung de tinh",
    "dua tren",
    "tinh tu",
    "lay loi nhuan",
)
_CFO_PAT_COMPARISON_MARKERS = (
    "so voi",
    "ty le",
    "chiem",
    "tuong duong",
    "bao phu",
    "chuyen doi",
    "cao hon",
    "thap hon",
    "lon hon",
    "nho hon",
    "/",
    "÷",
)
_PROFITABILITY_DIRECTION_MARKERS = (
    "tang",
    "giam",
    "cai thien",
    "suy giam",
    "thu hep",
    "mo rong",
    "di ngang",
    "khong doi",
)
_PRIOR_COMPARISON_MARKERS = (
    "so voi",
    "nam truoc",
    "ky truoc",
    "cung ky",
    "tu nam",
)
_PROFITABILITY_CONTEXT_MARKERS = (
    "agent_profitability",
    "kha nang sinh loi",
    "profitability",
    "roa",
    "roe",
    *_NET_MARGIN_ALIASES,
)
_PROFITABILITY_CLAIM_MARKERS = (
    "kha nang sinh loi",
    "loi nhuan",
    "roa",
    "roe",
    "bien loi nhuan",
    "bien rong",
    "profitability",
    "net margin",
)
_LEVEL_MARKERS = (
    "cao",
    "thap",
    "tot",
    "manh",
    "yeu",
    "kem",
)
_NON_PROFITABILITY_LEVEL_SUBJECTS = (
    "chi phi",
    "doanh thu",
    "tai san",
    "von chu so huu",
    "no phai tra",
    "lai suat",
    "thue suat",
)
_EXPLICIT_BENCHMARK_MARKERS = (
    "so voi",
    "nam truoc",
    "ky truoc",
    "cung ky",
    "trung binh nganh",
    "binh quan nganh",
    "so voi nganh",
    "doi thu",
    "peer",
    "benchmark",
    "muc tieu",
    "ke hoach",
    "nguong",
    "lich su",
    "thi truong",
    "diem phan tram",
)


def normalize_financial_text(value: Any) -> str:
    """Lowercase, strip accents and collapse whitespace for rule matching."""

    normalized = unicodedata.normalize("NFD", str(value or "").lower())
    ascii_text = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    ).replace("đ", "d")
    return " ".join(ascii_text.split())


def iter_financial_facts(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield fact dictionaries from retrieval or mixed analysis synth payloads."""

    if not isinstance(payload, Mapping):
        return

    facts = payload.get("facts")
    if isinstance(facts, list):
        for fact in facts:
            if isinstance(fact, Mapping) and is_report_fact(fact):
                yield dict(fact)

    for key, value in payload.items():
        if key in {"facts", "analysis_outputs", "typed_decimal_calculation"} or str(key).upper() == "WEB":
            continue
        if isinstance(value, Mapping):
            yield from iter_financial_facts(value)


def _fact_is_found(fact: Mapping[str, Any]) -> bool:
    status = normalize_financial_text(fact.get("status", "found"))
    return status in _FOUND_STATUSES


def _fact_text(fact: Mapping[str, Any]) -> str:
    return normalize_financial_text(
        " ".join(
            str(fact.get(key, "") or "")
            for key in (
                "item_name",
                "metric_label",
                "row_label",
                "section_key",
                "subheading",
            )
        )
    )


def _fact_primary_text(fact: Mapping[str, Any]) -> str:
    return normalize_financial_text(
        " ".join(
            str(fact.get(key, "") or "")
            for key in ("item_name", "metric_label", "row_label")
        )
    )


def _fact_is_profit_after_tax(fact: Mapping[str, Any]) -> bool:
    text = _fact_primary_text(fact)
    return (
        any(alias in text for alias in _NET_PROFIT_FACT_ALIASES)
        and "chua phan phoi" not in text
    )


def _fact_value(fact: Mapping[str, Any]) -> Decimal | None:
    parsed = fact.get("parsed_value")
    if parsed not in (None, ""):
        return parse_financial_decimal(parsed)
    return parse_financial_decimal(fact.get("value"))


def _cashflow_metric_for_fact(fact: Mapping[str, Any]) -> str:
    text = _fact_text(fact)
    for metric, aliases in _CASHFLOW_ALIASES.items():
        if any(alias in text for alias in aliases):
            return metric
    return ""


def cashflow_fact_values(payload: Any) -> dict[str, Decimal]:
    """Return one current-period value for each standard cash-flow subtotal."""

    candidates: dict[str, list[tuple[int, Decimal]]] = {}
    for fact in iter_financial_facts(payload):
        if not _fact_is_found(fact):
            continue
        metric = _cashflow_metric_for_fact(fact)
        value = _fact_value(fact)
        if not metric or value is None:
            continue
        period_surface = normalize_financial_text(
            " ".join(
                str(fact.get(key, "") or "")
                for key in ("period_role", "period", "time_hint", "period_label", "item_name")
            )
        )
        current_score = 1 if any(
            marker in period_surface
            for marker in ("current", "cuoi", "nam nay", "ky nay")
        ) else 0
        candidates.setdefault(metric, []).append((current_score, value))

    return {
        metric: sorted(values, key=lambda item: item[0], reverse=True)[0][1]
        for metric, values in candidates.items()
        if values
    }


def cashflow_identity_violation(payload: Any) -> str:
    """Return a violation when CFO + CFI + CFF does not equal net cash flow."""

    values = cashflow_fact_values(payload)
    required = {"cfo", "cfi", "cff", "net_cash_flow"}
    if not required.issubset(values):
        return ""
    calculated = values["cfo"] + values["cfi"] + values["cff"]
    if calculated == values["net_cash_flow"]:
        return ""
    return (
        "cashflow_identity: "
        f"CFO+CFI+CFF={calculated} != net_cash_flow={values['net_cash_flow']}"
    )


def _answer_clauses(answer: Any) -> list[str]:
    return [
        normalize_financial_text(item)
        # Dots inside a financial number are separators, not sentence ends.
        # Splitting ``38.988.400.000 VND - giảm`` at every dot detached the
        # metric from its direction claim and hid signed CFI/CFF mistakes.
        for item in re.split(r"[\n!?;]+|\.(?!\d)", str(answer or ""))
        if normalize_financial_text(item)
    ]


def _linked_note_ref_coverage_violations(
    answer: Any,
    facts: list[dict[str, Any]],
    plan_context: Any,
) -> list[str]:
    """Ask hard analysis to use at least one linked note detail it received."""

    plan = _coerce_plan_context(plan_context)
    if normalize_financial_text(plan.get("difficulty_level", "")) != "hard":
        return []
    note_refs = {
        normalize_financial_text(fact.get("note_ref", ""))
        for fact in facts
        if _fact_is_found(fact)
        and normalize_financial_text(fact.get("evidence_role", "")) == "note_detail"
        and str(fact.get("linked_parent_item", "") or "").strip()
        and str(fact.get("note_ref", "") or "").strip()
    }
    note_refs.discard("")
    if not note_refs:
        return []

    normalized_answer = normalize_financial_text(answer)
    for note_ref in note_refs:
        if re.search(
            rf"\bthuyet\s+minh\s+{re.escape(note_ref)}(?=\D|$)",
            normalized_answer,
        ):
            return []
    return ["note_ref_coverage:linked_detail_required"]


def _cashflow_claim_segments(clause: str) -> list[tuple[str, str]]:
    """Pair each cash-flow metric mention with its nearest narrative segment."""

    mentions: list[tuple[int, int, str]] = []
    for metric, aliases in _CASHFLOW_ALIASES.items():
        if metric == "net_cash_flow":
            continue
        for alias in aliases:
            for match in re.finditer(rf"\b{re.escape(alias)}\b", clause):
                mentions.append((match.start(), match.end(), metric))
    mentions.sort()
    # Prefer the longest alias when two aliases begin at the same position.
    collapsed: list[tuple[int, int, str]] = []
    for mention in mentions:
        if collapsed and mention[0] == collapsed[-1][0] and mention[2] == collapsed[-1][2]:
            if mention[1] > collapsed[-1][1]:
                collapsed[-1] = mention
            continue
        collapsed.append(mention)

    segments = []
    for index, (start, end, metric) in enumerate(collapsed):
        left = 0
        right = len(clause)
        if index:
            previous_end = collapsed[index - 1][1]
            left = (previous_end + start) // 2
        if index + 1 < len(collapsed):
            next_start = collapsed[index + 1][0]
            right = (end + next_start) // 2
        segments.append((metric, clause[left:right]))
    return segments


def _shared_cashflow_sign_violations(
    answer: Any,
    cash_values: Mapping[str, Decimal],
) -> list[str]:
    """Apply one trailing sign adjective to every metric in a joined phrase."""

    violations: list[str] = []
    for clause in _answer_clauses(answer):
        for match in re.finditer(
            r"\b((?:cfo|cfi|cff)(?:\s*(?:,|va)\s*(?:cfo|cfi|cff))+?)"
            r"\s+(am|duong)\b",
            clause,
        ):
            metrics = re.findall(r"\b(?:cfo|cfi|cff)\b", match.group(1))
            claimed_sign = match.group(2)
            for metric in metrics:
                value = cash_values.get(metric)
                if value is None:
                    continue
                if value > 0 and claimed_sign == "am":
                    violations.append(f"cashflow_sign:{metric}:expected_positive")
                elif value < 0 and claimed_sign == "duong":
                    violations.append(f"cashflow_sign:{metric}:expected_negative")
    return violations


def _cashflow_effect_direction_violations(
    answer: Any,
    cash_values: Mapping[str, Decimal],
) -> list[str]:
    """Check narrative cash-direction claims against the flows named nearby."""

    violations: list[str] = []
    for clause in _answer_clauses(answer):
        mentioned = [
            metric
            for metric, aliases in _CASHFLOW_ALIASES.items()
            if metric != "net_cash_flow"
            and metric in cash_values
            and any(re.search(rf"\b{re.escape(alias)}\b", clause) for alias in aliases)
        ]
        if not mentioned:
            continue
        effect = sum((cash_values[metric] for metric in mentioned), Decimal(0))
        decrease_claim = bool(
            re.search(
                r"\b(?:lam|khien|dan den|gay ra)\b.{0,60}"
                r"\bgiam\b.{0,30}\b(?:tien|tien mat)\b",
                clause,
            )
            or re.search(r"\b(?:tien|tien mat)\b.{0,30}\bgiam\b", clause)
        )
        increase_claim = bool(
            re.search(
                r"\b(?:lam|khien|dan den|gay ra)\b.{0,60}"
                r"\btang\b.{0,30}\b(?:tien|tien mat)\b",
                clause,
            )
            or re.search(r"\b(?:tien|tien mat)\b.{0,30}\btang\b", clause)
        )
        if effect > 0 and decrease_claim:
            violations.append("cashflow_effect_direction:expected_increase")
        elif effect < 0 and increase_claim:
            violations.append("cashflow_effect_direction:expected_decrease")
    return violations


def _query_requests_compact_amounts(user_query: Any) -> bool:
    query = normalize_financial_text(user_query)
    return bool(
        re.search(
            r"(?:bao nhieu|don vi|quy doi|tinh theo|theo)\s+"
            r"(?:trieu|ty|nghin\s+ty)(?:\s+dong|\s+vnd)?\b",
            query,
        )
    )


def _available_metric_keys(facts: list[dict[str, Any]]) -> set[str]:
    available = set()
    for fact in facts:
        if not _fact_is_found(fact) or not str(fact.get("value", "") or "").strip():
            continue
        text = _fact_text(fact)
        for metric, aliases in _METRIC_ALIASES.items():
            if metric == "net_profit" and not _fact_is_profit_after_tax(fact):
                continue
            if any(alias in text for alias in aliases):
                available.add(metric)
    return available


def _coerce_plan_context(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _nested_text(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
            return
        if item not in (None, ""):
            parts.append(str(item))

    visit(value)
    return normalize_financial_text(" ".join(parts))


def _fact_full_text(fact: Mapping[str, Any]) -> str:
    return normalize_financial_text(
        " ".join(
            str(fact.get(key, "") or "")
            for key in (
                "item_name",
                "metric_label",
                "row_label",
                "section_key",
                "subheading",
                "section_path",
                "scope_label",
                "table",
                "source",
            )
        )
    )


def _payload_is_separate_statement(facts: list[dict[str, Any]]) -> bool:
    return any(
        any(marker in _fact_full_text(fact) for marker in _SEPARATE_STATEMENT_MARKERS)
        for fact in facts
    )


def _has_profit_after_tax_fact(facts: list[dict[str, Any]]) -> bool:
    return any(
        _fact_is_found(fact)
        and _fact_value(fact) is not None
        and _fact_is_profit_after_tax(fact)
        for fact in facts
    )


def _net_profit_semantic_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    """Protect the PAT/net-profit equivalence for a standalone statement."""

    if not _has_profit_after_tax_fact(facts) or not _payload_is_separate_statement(facts):
        return []

    for clause in _answer_clauses(answer):
        has_pat = any(alias in clause for alias in _NET_PROFIT_FACT_ALIASES)
        has_net_profit_name = any(alias in clause for alias in _NET_PROFIT_NAME_ALIASES)
        if (
            has_pat
            and has_net_profit_name
            and any(marker in clause for marker in _PROXY_MARKERS)
        ):
            return ["profitability_net_profit_equivalence:pat_is_net_profit"]
    return []


def _fact_period_role(fact: Mapping[str, Any]) -> str:
    explicit = normalize_financial_text(fact.get("period_role", ""))
    if explicit in {"current", "previous"}:
        return explicit
    surface = normalize_financial_text(
        " ".join(
            str(fact.get(key, "") or "")
            for key in (
                "period",
                "period_label",
                "time_hint",
                "column_label",
                "item_name",
            )
        )
    )
    if any(marker in surface for marker in ("dau ky", "dau nam", "nam truoc", "ky truoc")):
        return "previous"
    if any(marker in surface for marker in ("cuoi ky", "cuoi nam", "nam nay", "ky nay")):
        return "current"
    return ""


def _current_metric_values(
    facts: list[dict[str, Any]],
    aliases: tuple[str, ...],
) -> list[Decimal]:
    values: list[Decimal] = []
    for fact in facts:
        if not _fact_is_found(fact) or not any(
            alias in _fact_text(fact) for alias in aliases
        ):
            continue
        value = _fact_value(fact)
        if value is None:
            continue
        role = _fact_period_role(fact)
        if role != "previous":
            values.append(value)
    return list(dict.fromkeys(values))


def _select_typed_metric_fact(
    facts: list[dict[str, Any]],
    aliases: tuple[str, ...],
    period_role: str,
    *,
    excluded_aliases: tuple[str, ...] = (),
    preferred_tables: tuple[str, ...] = (),
    reporting_basis: str = "",
) -> tuple[dict[str, Any], Decimal] | None:
    """Select one unambiguous typed fact; never resolve conflicts by rank."""

    candidates: list[tuple[dict[str, Any], Decimal]] = []
    for fact in facts:
        primary_text = _fact_primary_text(fact)
        if (
            not _fact_is_found(fact)
            or _fact_period_role(fact) != period_role
            or not any(alias in primary_text for alias in aliases)
            or any(alias in primary_text for alias in excluded_aliases)
        ):
            continue
        actual_reporting_basis = (
            str(fact.get("reporting_basis", "") or "").strip()
            or fact_reporting_basis(fact)
        )
        if (
            reporting_basis
            and actual_reporting_basis
            and not reporting_basis_compatible(
                reporting_basis,
                actual_reporting_basis,
            )
        ):
            continue
        value = _fact_value(fact)
        if value is not None:
            candidates.append((fact, value))
    if not candidates:
        return None

    preferred = [
        candidate
        for candidate in candidates
        if any(
            table in normalize_financial_text(
                candidate[0].get("table", "")
                or candidate[0].get("heading", "")
                or ""
            )
            for table in preferred_tables
        )
    ]
    if preferred:
        candidates = preferred

    distinct = {
        (value, normalize_financial_text(fact.get("unit", "")))
        for fact, value in candidates
    }
    if len(distinct) != 1:
        return None
    candidates.sort(
        key=lambda item: (
            bool(str(item[0].get("fact_id", "") or "").strip()),
            bool(str(item[0].get("source", "") or "").strip()),
            bool(str(item[0].get("source_page", "") or "").strip()),
        ),
        reverse=True,
    )
    return candidates[0]


def _fact_document(fact: Mapping[str, Any]) -> str:
    return str(fact.get("source", "") or "").strip().split("#", 1)[0]


def _profitability_facts_are_compatible(
    selected: Mapping[str, tuple[dict[str, Any], Decimal]],
) -> bool:
    units = {
        normalize_financial_text(fact.get("unit", ""))
        for fact, _value in selected.values()
        if str(fact.get("unit", "") or "").strip()
    }
    if len(units) != 1:
        return False
    companies = {
        normalize_financial_text(fact.get("company", ""))
        for fact, _value in selected.values()
        if str(fact.get("company", "") or "").strip()
    }
    if len(companies) > 1:
        return False
    current_fiscal_years = {
        str(selected[key][0].get("fiscal_year", "") or "").strip()
        for key in (
            "net_profit",
            "net_revenue",
            "gross_profit_current",
            "operating_profit_current",
            "total_assets_current",
            "equity_current",
            "cfo",
        )
        if key in selected
        if str(selected[key][0].get("fiscal_year", "") or "").strip()
    }
    if len(current_fiscal_years) > 1:
        return False
    opening_periods = {
        normalize_financial_text(
            selected[key][0].get("period_label", "")
            or selected[key][0].get("period", "")
            or selected[key][0].get("time_hint", "")
            or selected[key][0].get("column_label", "")
        )
        for key in ("total_assets_opening", "equity_opening")
        if key in selected
        if str(
            selected[key][0].get("period_label", "")
            or selected[key][0].get("period", "")
            or selected[key][0].get("time_hint", "")
            or selected[key][0].get("column_label", "")
            or ""
        ).strip()
    }
    if len(opening_periods) > 1:
        return False
    documents = {
        _fact_document(fact)
        for fact, _value in selected.values()
        if _fact_document(fact)
    }
    return len(documents) <= 1


def _fact_snapshot(fact: Mapping[str, Any], value: Decimal) -> dict[str, str]:
    snapshot = {
        "fact_id": str(fact.get("fact_id", "") or "").strip(),
        "label": str(
            fact.get("row_label", "")
            or fact.get("metric_label", "")
            or fact.get("item_name", "")
            or ""
        ).strip(),
        "value": format(value, "f"),
        "raw_value": str(fact.get("value", "") or "").strip(),
        "unit": str(fact.get("unit", "") or "").strip(),
        "fiscal_year": str(fact.get("fiscal_year", "") or "").strip(),
        "period": str(
            fact.get("period_label", "")
            or fact.get("period", "")
            or fact.get("time_hint", "")
            or fact.get("column_label", "")
            or ""
        ).strip(),
        "period_role": _fact_period_role(fact),
        "reporting_basis": (
            str(fact.get("reporting_basis", "") or "").strip()
            or fact_reporting_basis(fact)
        ),
        "table": str(fact.get("table", "") or "").strip(),
        "source": str(fact.get("source", "") or "").strip(),
        "source_page": str(fact.get("source_page", "") or "").strip(),
    }
    return {key: value for key, value in snapshot.items() if value}


def _rounded_percent(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def canonical_profitability_metrics(payload: Any) -> dict[str, Any]:
    """Build canonical annual profitability context when operands bind uniquely.

    Required typed facts are current PAT and net revenue plus current/opening
    total assets and equity.  When an unambiguous compatible current CFO fact is
    also present, the result additionally exposes an exact CFO/PAT comparison.
    If both prior PAT and prior net revenue are present, it exposes exact
    comparative metrics without inventing a cause or qualitative benchmark.
    """

    facts = list(iter_financial_facts(payload))
    selections = {
        "net_profit": _select_typed_metric_fact(
            facts,
            _NET_PROFIT_FACT_ALIASES,
            "current",
            excluded_aliases=("chua phan phoi",),
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "net_revenue": _select_typed_metric_fact(
            facts,
            _NET_REVENUE_FACT_ALIASES,
            "current",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "total_assets_current": _select_typed_metric_fact(
            facts,
            _METRIC_ALIASES["total_assets"],
            "current",
            excluded_aliases=("tai san ngan han", "tai san dai han", "binh quan"),
            preferred_tables=("bang can doi ke toan", "bao cao tinh hinh tai chinh"),
        ),
        "total_assets_opening": _select_typed_metric_fact(
            facts,
            _METRIC_ALIASES["total_assets"],
            "previous",
            excluded_aliases=("tai san ngan han", "tai san dai han", "binh quan"),
            preferred_tables=("bang can doi ke toan", "bao cao tinh hinh tai chinh"),
        ),
        "equity_current": _select_typed_metric_fact(
            facts,
            _METRIC_ALIASES["equity"],
            "current",
            preferred_tables=("bang can doi ke toan", "bao cao tinh hinh tai chinh"),
        ),
        "equity_opening": _select_typed_metric_fact(
            facts,
            _METRIC_ALIASES["equity"],
            "previous",
            preferred_tables=("bang can doi ke toan", "bao cao tinh hinh tai chinh"),
        ),
    }
    if any(selection is None for selection in selections.values()):
        return {}

    selected = {
        key: selection
        for key, selection in selections.items()
        if selection is not None
    }
    if not _profitability_facts_are_compatible(selected):
        return {}

    prior_selections = {
        "net_profit_previous": _select_typed_metric_fact(
            facts,
            _NET_PROFIT_FACT_ALIASES,
            "previous",
            excluded_aliases=("chua phan phoi",),
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "net_revenue_previous": _select_typed_metric_fact(
            facts,
            _NET_REVENUE_FACT_ALIASES,
            "previous",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
    }
    if all(selection is not None for selection in prior_selections.values()):
        selected_with_prior = {
            **selected,
            **{
                key: selection
                for key, selection in prior_selections.items()
                if selection is not None
            },
        }
        if _profitability_facts_are_compatible(selected_with_prior):
            selected = selected_with_prior
    elif prior_selections["net_profit_previous"] is not None:
        # Prior PAT is also useful for an optional CFO/PAT comparison even if
        # the prior revenue leg needed for margin comparisons is absent.
        selected_with_prior_pat = {
            **selected,
            "net_profit_previous": prior_selections["net_profit_previous"],
        }
        if _profitability_facts_are_compatible(selected_with_prior_pat):
            selected = selected_with_prior_pat

    optional_margin_selections = {
        "gross_profit_current": _select_typed_metric_fact(
            facts,
            _GROSS_PROFIT_ALIASES,
            "current",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "gross_profit_previous": _select_typed_metric_fact(
            facts,
            _GROSS_PROFIT_ALIASES,
            "previous",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "operating_profit_current": _select_typed_metric_fact(
            facts,
            _OPERATING_PROFIT_FACT_ALIASES,
            "current",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
        "operating_profit_previous": _select_typed_metric_fact(
            facts,
            _OPERATING_PROFIT_FACT_ALIASES,
            "previous",
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
            reporting_basis="full_period",
        ),
    }
    for prefix in ("gross_profit", "operating_profit"):
        current_key = f"{prefix}_current"
        previous_key = f"{prefix}_previous"
        current_selection = optional_margin_selections[current_key]
        if current_selection is None:
            continue
        selected_with_current = {**selected, current_key: current_selection}
        if not _profitability_facts_are_compatible(selected_with_current):
            continue
        selected = selected_with_current
        previous_selection = optional_margin_selections[previous_key]
        if previous_selection is None:
            continue
        selected_with_previous = {**selected, previous_key: previous_selection}
        if _profitability_facts_are_compatible(selected_with_previous):
            selected = selected_with_previous

    optional_balance_selections = {
        "current_assets": _select_typed_metric_fact(
            facts,
            _CURRENT_ASSETS_FACT_ALIASES,
            "current",
            preferred_tables=("bang can doi ke toan",),
        ),
        "current_liabilities": _select_typed_metric_fact(
            facts,
            _CURRENT_LIABILITIES_FACT_ALIASES,
            "current",
            preferred_tables=("bang can doi ke toan",),
        ),
        "inventory": _select_typed_metric_fact(
            facts,
            _INVENTORY_FACT_ALIASES,
            "current",
            preferred_tables=("bang can doi ke toan",),
        ),
        "cash": _select_typed_metric_fact(
            facts,
            _CASH_FACT_ALIASES,
            "current",
            preferred_tables=("bang can doi ke toan",),
        ),
        "total_liabilities": _select_typed_metric_fact(
            facts,
            _TOTAL_LIABILITIES_FACT_ALIASES,
            "current",
            preferred_tables=("bang can doi ke toan",),
        ),
    }
    for key, selection in optional_balance_selections.items():
        if selection is None:
            continue
        selected_with_balance_fact = {**selected, key: selection}
        if _profitability_facts_are_compatible(selected_with_balance_fact):
            selected = selected_with_balance_fact

    cfo_selection = _select_typed_metric_fact(
        facts,
        _CASHFLOW_ALIASES["cfo"],
        "current",
        preferred_tables=("bao cao luu chuyen tien te",),
        reporting_basis="full_period",
    )
    # CFO is optional, but when the ledger also contains its prior-year pair,
    # both values must share the same unit before we expose the current value
    # in a CFO/PAT calculation.  Otherwise a distractor/malformed prior fact
    # can make the current VND CFO look contract-safe while the comparative
    # cash-flow ledger is not.
    cfo_previous_selection = _select_typed_metric_fact(
        facts,
        _CASHFLOW_ALIASES["cfo"],
        "previous",
        preferred_tables=("bao cao luu chuyen tien te",),
        reporting_basis="full_period",
    )
    if (
        cfo_selection is not None
        and cfo_previous_selection is not None
        and normalize_financial_text(cfo_selection[0].get("unit", ""))
        != normalize_financial_text(cfo_previous_selection[0].get("unit", ""))
    ):
        cfo_selection = None
    if cfo_selection is not None:
        selected_with_cfo = {**selected, "cfo": cfo_selection}
        if _profitability_facts_are_compatible(selected_with_cfo):
            selected = selected_with_cfo
            if cfo_previous_selection is not None:
                selected_with_cfo_pair = {
                    **selected,
                    "cfo_previous": cfo_previous_selection,
                }
                if _profitability_facts_are_compatible(selected_with_cfo_pair):
                    selected = selected_with_cfo_pair

    net_profit = selected["net_profit"][1]
    net_revenue = selected["net_revenue"][1]
    assets_current = selected["total_assets_current"][1]
    assets_opening = selected["total_assets_opening"][1]
    equity_current = selected["equity_current"][1]
    equity_opening = selected["equity_opening"][1]
    with localcontext() as context:
        context.prec = 50
        average_assets = (assets_opening + assets_current) / Decimal(2)
        average_equity = (equity_opening + equity_current) / Decimal(2)
        if net_revenue == 0 or average_assets == 0 or average_equity == 0:
            return {}
        net_margin = net_profit / net_revenue * Decimal(100)
        gross_margin = (
            selected["gross_profit_current"][1] / net_revenue * Decimal(100)
            if "gross_profit_current" in selected
            else None
        )
        operating_margin = (
            selected["operating_profit_current"][1]
            / net_revenue
            * Decimal(100)
            if "operating_profit_current" in selected
            else None
        )
        roa = net_profit / average_assets * Decimal(100)
        roe = net_profit / average_equity * Decimal(100)
        asset_turnover = net_revenue / average_assets
        cfo_to_net_profit = (
            selected["cfo"][1] / net_profit * Decimal(100)
            if "cfo" in selected and net_profit != 0
            else None
        )
        current_ratio = None
        quick_ratio = None
        cash_ratio = None
        debt_to_equity = None
        if (
            "current_assets" in selected
            and "current_liabilities" in selected
            and selected["current_liabilities"][1] != 0
        ):
            current_assets = selected["current_assets"][1]
            current_liabilities = selected["current_liabilities"][1]
            current_ratio = current_assets / current_liabilities
            if "inventory" in selected:
                quick_ratio = (
                    current_assets - selected["inventory"][1]
                ) / current_liabilities
            if "cash" in selected:
                cash_ratio = selected["cash"][1] / current_liabilities
        if "total_liabilities" in selected and equity_current != 0:
            debt_to_equity = selected["total_liabilities"][1] / equity_current
        cfo_to_net_profit_previous = None
        cfo_to_net_profit_change = None
        cfo_growth = None
        cfo_change_amount = None
        cfo_sign_transition = ""
        if {
            "cfo",
            "cfo_previous",
            "net_profit_previous",
        }.issubset(selected) and net_profit != 0 and selected["net_profit_previous"][1] != 0:
            cfo_to_net_profit_previous = (
                selected["cfo_previous"][1]
                / selected["net_profit_previous"][1]
                * Decimal(100)
            )
            cfo_to_net_profit_change = (
                cfo_to_net_profit - cfo_to_net_profit_previous
            )
            current_cfo = selected["cfo"][1]
            previous_cfo = selected["cfo_previous"][1]
            cfo_change_amount = current_cfo - previous_cfo
            if current_cfo > 0 > previous_cfo:
                cfo_sign_transition = "negative_to_positive"
            elif current_cfo < 0 < previous_cfo:
                cfo_sign_transition = "positive_to_negative"
            if previous_cfo != 0 and not cfo_sign_transition:
                cfo_growth = (
                    cfo_change_amount
                    / abs(previous_cfo)
                    * Decimal(100)
                )
        comparatives: dict[str, Any] = {}
        if {
            "net_profit_previous",
            "net_revenue_previous",
        }.issubset(selected):
            prior_net_profit = selected["net_profit_previous"][1]
            prior_net_revenue = selected["net_revenue_previous"][1]
            if prior_net_revenue != 0:
                prior_net_margin = prior_net_profit / prior_net_revenue * Decimal(100)
                net_margin_change = net_margin - prior_net_margin
                comparatives = {
                    "net_margin_previous": {
                        "value": _rounded_percent(prior_net_margin),
                        "unit": "%",
                        "formula": "net_profit_previous / net_revenue_previous * 100",
                        "operand_keys": [
                            "net_profit_previous",
                            "net_revenue_previous",
                        ],
                    },
                    "net_margin_change": {
                        "value": _rounded_percent(net_margin_change),
                        "unit": "percentage_points",
                        "formula": "net_margin - net_margin_previous",
                        "direction": (
                            "increase"
                            if net_margin_change > 0
                            else "decrease"
                            if net_margin_change < 0
                            else "unchanged"
                        ),
                    },
                }
                if prior_net_profit != 0:
                    profit_growth = (
                        (net_profit - prior_net_profit)
                        / abs(prior_net_profit)
                        * Decimal(100)
                    )
                    comparatives["net_profit_growth"] = {
                        "value": _rounded_percent(profit_growth),
                        "unit": "%",
                        "formula": (
                            "(net_profit - net_profit_previous) / "
                            "abs(net_profit_previous) * 100"
                        ),
                        "operand_keys": ["net_profit", "net_profit_previous"],
                        "direction": (
                            "increase"
                            if profit_growth > 0
                            else "decrease"
                            if profit_growth < 0
                            else "unchanged"
                        ),
                    }
                revenue_growth = (
                    (net_revenue - prior_net_revenue)
                    / abs(prior_net_revenue)
                    * Decimal(100)
                )
                comparatives["net_revenue_growth"] = {
                    "value": _rounded_percent(revenue_growth),
                    "unit": "%",
                    "formula": (
                        "(net_revenue - net_revenue_previous) / "
                        "abs(net_revenue_previous) * 100"
                    ),
                    "operand_keys": ["net_revenue", "net_revenue_previous"],
                    "direction": (
                        "increase"
                        if revenue_growth > 0
                        else "decrease"
                        if revenue_growth < 0
                        else "unchanged"
                    ),
                }
                for margin_key, profit_key in (
                    ("gross_margin", "gross_profit"),
                    ("operating_margin", "operating_profit"),
                ):
                    current_profit_key = f"{profit_key}_current"
                    previous_profit_key = f"{profit_key}_previous"
                    if not {
                        current_profit_key,
                        previous_profit_key,
                    }.issubset(selected):
                        continue
                    current_margin = (
                        selected[current_profit_key][1]
                        / net_revenue
                        * Decimal(100)
                    )
                    previous_margin = (
                        selected[previous_profit_key][1]
                        / prior_net_revenue
                        * Decimal(100)
                    )
                    margin_change = current_margin - previous_margin
                    comparatives[f"{margin_key}_previous"] = {
                        "value": _rounded_percent(previous_margin),
                        "unit": "%",
                        "formula": (
                            f"{previous_profit_key} / net_revenue_previous * 100"
                        ),
                        "operand_keys": [
                            previous_profit_key,
                            "net_revenue_previous",
                        ],
                    }
                    comparatives[f"{margin_key}_change"] = {
                        "value": _rounded_percent(margin_change),
                        "unit": "percentage_points",
                        "formula": f"{margin_key} - {margin_key}_previous",
                        "direction": (
                            "increase"
                            if margin_change > 0
                            else "decrease"
                            if margin_change < 0
                            else "unchanged"
                        ),
                    }

        if (
            "net_profit_previous" in selected
            and selected["net_profit_previous"][1] != 0
            and "net_profit_growth" not in comparatives
        ):
            net_profit_growth = (
                (net_profit - selected["net_profit_previous"][1])
                / abs(selected["net_profit_previous"][1])
                * Decimal(100)
            )
            comparatives["net_profit_growth"] = {
                "value": _rounded_percent(net_profit_growth),
                "unit": "%",
                "formula": (
                    "(net_profit - net_profit_previous) / "
                    "abs(net_profit_previous) * 100"
                ),
                "operand_keys": ["net_profit", "net_profit_previous"],
                "direction": (
                    "increase"
                    if net_profit_growth > 0
                    else "decrease"
                    if net_profit_growth < 0
                    else "unchanged"
                ),
            }

        if cfo_to_net_profit_previous is not None and cfo_to_net_profit_change is not None:
            comparatives["cfo_to_net_profit_previous"] = {
                "value": _rounded_percent(cfo_to_net_profit_previous),
                "unit": "%",
                "formula": "cfo_previous / net_profit_previous * 100",
                "operand_keys": ["cfo_previous", "net_profit_previous"],
            }
            comparatives["cfo_to_net_profit_change"] = {
                "value": _rounded_percent(cfo_to_net_profit_change),
                "unit": "percentage_points",
                "formula": "cfo_to_net_profit - cfo_to_net_profit_previous",
                "direction": (
                    "increase"
                    if cfo_to_net_profit_change > 0
                    else "decrease"
                    if cfo_to_net_profit_change < 0
                    else "unchanged"
                ),
                "operand_keys": [
                    "cfo",
                    "net_profit",
                    "cfo_previous",
                    "net_profit_previous",
                ],
            }
            if cfo_growth is not None:
                comparatives["cfo_growth"] = {
                    "value": _rounded_percent(cfo_growth),
                    "unit": "%",
                    "formula": "(cfo - cfo_previous) / abs(cfo_previous) * 100",
                    "operand_keys": ["cfo", "cfo_previous"],
                    "direction": (
                        "increase"
                        if cfo_growth > 0
                        else "decrease"
                        if cfo_growth < 0
                        else "unchanged"
                    ),
                }
            if cfo_change_amount is not None:
                comparatives["cfo_change_amount"] = {
                    "value": format(cfo_change_amount, "f"),
                    "unit": str(
                        selected["cfo"][0].get("unit", "") or ""
                    ).strip(),
                    "formula": "cfo - cfo_previous",
                    "operand_keys": ["cfo", "cfo_previous"],
                    **(
                        {"sign_transition": cfo_sign_transition}
                        if cfo_sign_transition
                        else {}
                    ),
                }

    inputs = {
        key: _fact_snapshot(selection[0], selection[1])
        for key, selection in selected.items()
    }
    period = (
        inputs["net_profit"].get("fiscal_year", "")
        or inputs["net_profit"].get("period", "")
    )
    amount_unit = inputs["net_profit"].get("unit", "")
    metrics = {
        "net_margin": {
            "value": _rounded_percent(net_margin),
            "unit": "%",
            "formula": "net_profit / net_revenue * 100",
            "operand_keys": ["net_profit", "net_revenue"],
        },
        "roa": {
            "value": _rounded_percent(roa),
            "unit": "%",
            "formula": "net_profit / average_total_assets * 100",
            "basis": "average_opening_closing",
            "operand_keys": [
                "net_profit",
                "total_assets_opening",
                "total_assets_current",
            ],
        },
        "roe": {
            "value": _rounded_percent(roe),
            "unit": "%",
            "formula": "net_profit / average_equity * 100",
            "basis": "average_opening_closing",
            "operand_keys": [
                "net_profit",
                "equity_opening",
                "equity_current",
            ],
        },
        "asset_turnover": {
            "value": format(
                asset_turnover.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                ),
                "f",
            ),
            "unit": "x",
            "formula": "net_revenue / average_total_assets",
            "basis": "average_opening_closing",
            "operand_keys": [
                "net_revenue",
                "total_assets_opening",
                "total_assets_current",
            ],
        },
    }
    for metric_key, value, formula, operand_keys in (
        (
            "current_ratio",
            current_ratio,
            "current_assets / current_liabilities",
            ["current_assets", "current_liabilities"],
        ),
        (
            "quick_ratio",
            quick_ratio,
            "(current_assets - inventory) / current_liabilities",
            ["current_assets", "inventory", "current_liabilities"],
        ),
        (
            "cash_ratio",
            cash_ratio,
            "cash / current_liabilities",
            ["cash", "current_liabilities"],
        ),
        (
            "debt_to_equity",
            debt_to_equity,
            "total_liabilities / equity_current",
            ["total_liabilities", "equity_current"],
        ),
    ):
        if value is None:
            continue
        metrics[metric_key] = {
            "value": format(
                value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "f",
            ),
            "unit": "x",
            "formula": formula,
            "operand_keys": operand_keys,
        }
    if gross_margin is not None:
        metrics["gross_margin"] = {
            "value": _rounded_percent(gross_margin),
            "unit": "%",
            "formula": "gross_profit_current / net_revenue * 100",
            "operand_keys": ["gross_profit_current", "net_revenue"],
        }
    if operating_margin is not None:
        metrics["operating_margin"] = {
            "value": _rounded_percent(operating_margin),
            "unit": "%",
            "formula": "operating_profit_current / net_revenue * 100",
            "operand_keys": ["operating_profit_current", "net_revenue"],
        }
    if cfo_to_net_profit is not None:
        cfo_value = selected["cfo"][1]
        metrics["cfo_to_net_profit"] = {
            "value": _rounded_percent(cfo_to_net_profit),
            "unit": "%",
            "formula": "cfo / net_profit * 100",
            "operand_keys": ["cfo", "net_profit"],
            "direction": (
                "below_net_profit"
                if cfo_value < net_profit
                else "above_net_profit"
                if cfo_value > net_profit
                else "equal_to_net_profit"
            ),
            **(
                {
                    "interpretation_guard": (
                        "net_profit_non_positive_do_not_label_"
                        "earnings_conversion_quality"
                    )
                }
                if net_profit <= 0
                else {}
            ),
        }

    result = {
        "schema_version": 1,
        "status": "complete",
        "scope": "separate" if _payload_is_separate_statement(facts) else "unknown",
        "period": period,
        "amount_unit": amount_unit,
        "basis_disclosure": (
            "ROA/ROE sử dụng bình quân số đầu kỳ và cuối kỳ; "
            "các biên lợi nhuận sử dụng doanh thu thuần."
        ),
        "inputs": inputs,
        "derived_denominators": {
            "average_total_assets": {
                "value": format(average_assets, "f"),
                "unit": amount_unit,
                "formula": "(total_assets_opening + total_assets_current) / 2",
            },
            "average_equity": {
                "value": format(average_equity, "f"),
                "unit": amount_unit,
                "formula": "(equity_opening + equity_current) / 2",
            },
        },
        "metrics": metrics,
    }
    if comparatives:
        result["comparatives"] = comparatives
    return result


def _answer_contains_decimal(clause: str, value: Decimal) -> bool:
    digits = re.sub(r"\D", "", format(abs(value), "f"))
    if not digits:
        return False
    return digits in re.sub(r"\D", "", clause)


def _profitability_ratio_segments(answer: Any) -> list[tuple[str, str]]:
    """Keep formatted VND operands intact while scoping text to one ratio."""

    answer_text = normalize_financial_text(answer)
    mentions = [
        (match.start(), match.end(), match.group(0))
        for match in re.finditer(r"\b(?:roa|roe)\b", answer_text)
    ]
    segments: list[tuple[str, str]] = []
    for index, (start, _end, ratio) in enumerate(mentions):
        right = mentions[index + 1][0] if index + 1 < len(mentions) else len(answer_text)
        segments.append((ratio, answer_text[start:right]))
    return segments


def _annual_ratio_basis_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    """Require annual ROA/ROE answers to disclose average or simplified ending basis."""

    if not _has_profit_after_tax_fact(facts):
        return []

    ratio_denominators = {
        "roa": _current_metric_values(facts, _METRIC_ALIASES["total_assets"]),
        "roe": _current_metric_values(facts, _METRIC_ALIASES["equity"]),
    }
    answer_text = normalize_financial_text(answer)
    segments = _profitability_ratio_segments(answer)
    mentioned = {ratio for ratio, _clause in segments}
    valid_basis: set[str] = set()
    ending_without_label: set[str] = set()
    for ratio, clause in segments:
        current_values = ratio_denominators[ratio]
        has_average_basis = any(marker in clause for marker in _AVERAGE_BASIS_MARKERS)
        if has_average_basis:
            valid_basis.add(ratio)
            continue
        has_ending_basis = any(marker in clause for marker in _ENDING_BASIS_MARKERS)
        uses_current_denominator = any(
            _answer_contains_decimal(clause, value) for value in current_values
        )
        has_simplified_label = any(
            marker in clause for marker in _SIMPLIFIED_RATIO_MARKERS
        )
        if has_ending_basis and has_simplified_label:
            valid_basis.add(ratio)
        elif has_ending_basis or uses_current_denominator:
            ending_without_label.add(ratio)

    # A shared sentence such as "ROA và ROE được tính trên số bình quân..."
    # discloses the basis for both metrics even though the text segmenter binds
    # the trailing basis phrase to the second mention.
    paired = re.search(
        r"\broa\b\s*(?:va|/|,)\s*\broe\b|"
        r"\broe\b\s*(?:va|/|,)\s*\broa\b",
        answer_text,
    )
    if paired:
        left = max(0, paired.start() - 80)
        right = min(len(answer_text), paired.end() + 180)
        shared_clause = answer_text[left:right]
        if any(marker in shared_clause for marker in _AVERAGE_BASIS_MARKERS):
            valid_basis.update({"roa", "roe"})
        elif (
            any(marker in shared_clause for marker in _ENDING_BASIS_MARKERS)
            and any(marker in shared_clause for marker in _SIMPLIFIED_RATIO_MARKERS)
        ):
            valid_basis.update({"roa", "roe"})

    violations: list[str] = []
    for ratio in ("roa", "roe"):
        if ratio not in mentioned or ratio in valid_basis:
            continue
        if ratio in ending_without_label:
            violations.append(
                f"profitability_ratio_basis:{ratio}:ending_balance_must_be_labeled_simplified"
            )
        else:
            violations.append(f"profitability_ratio_basis:{ratio}:basis_undisclosed")
    return violations


def _reported_percentage_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in re.finditer(r"(?<!\w)([+-]?\d+(?:[.,]\d+)?)\s*%", text):
        value = parse_financial_decimal(match.group(1))
        if value is not None:
            values.append(value)
    return values


def _ratio_uses_operating_profit_text(
    ratio: str,
    segment: str,
    operating_profit: Decimal,
) -> bool:
    if any(marker in segment for marker in _OPERATING_NUMERATOR_NEGATION_MARKERS):
        return False
    match = re.search(rf"\b{ratio}\b", segment)
    if not match:
        return False
    formula_surface = segment[match.end() : match.end() + 240]
    numerator_surface = re.split(r"(?:/|÷)", formula_surface, maxsplit=1)[0]
    if any(alias in numerator_surface for alias in _OPERATING_PROFIT_FACT_ALIASES):
        return True
    if _answer_contains_decimal(numerator_surface, operating_profit):
        return True
    return bool(
        any(alias in formula_surface for alias in _OPERATING_PROFIT_FACT_ALIASES)
        and any(marker in formula_surface for marker in _OPERATING_NUMERATOR_USE_MARKERS)
    )


def _profitability_ratio_numerator_violations(
    answer: Any,
    payload: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    """Require standard annual ROA/ROE to use PAT rather than operating profit."""

    if not _has_profit_after_tax_fact(facts):
        return []
    operating_selection = _select_typed_metric_fact(
        facts,
        _OPERATING_PROFIT_FACT_ALIASES,
        "current",
        preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
    )
    if operating_selection is None:
        return []
    operating_profit = operating_selection[1]

    calculation = canonical_profitability_metrics(payload)
    metrics = calculation.get("metrics", {}) or {}
    denominators = calculation.get("derived_denominators", {}) or {}
    current_period = str(calculation.get("period", "") or "")
    current_year_match = re.search(r"\b(?:19|20)\d{2}\b", current_period)
    current_year = current_year_match.group(0) if current_year_match else ""
    expected_values = {
        ratio: parse_financial_decimal((metrics.get(ratio, {}) or {}).get("value"))
        for ratio in ("roa", "roe")
    }
    denominator_values = {
        "roa": parse_financial_decimal(
            (denominators.get("average_total_assets", {}) or {}).get("value")
        ),
        "roe": parse_financial_decimal(
            (denominators.get("average_equity", {}) or {}).get("value")
        ),
    }
    alternative_values: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 50
        for ratio, denominator in denominator_values.items():
            if denominator not in (None, Decimal(0)):
                alternative_values[ratio] = operating_profit / denominator * Decimal(100)

    violations: list[str] = []
    for ratio, segment in _profitability_ratio_segments(answer):
        if _ratio_uses_operating_profit_text(ratio, segment, operating_profit):
            violations.append(
                f"profitability_ratio_numerator:{ratio}:must_use_net_profit"
            )
            continue

        alternative = alternative_values.get(ratio)
        expected = expected_values.get(ratio)
        if alternative is None or expected is None:
            continue
        segment_years = set(re.findall(r"\b(?:19|20)\d{2}\b", segment[:260]))
        if current_year and segment_years and current_year not in segment_years:
            continue
        reported = _reported_percentage_values(segment[:260])
        has_expected = any(abs(value - expected) <= Decimal("0.15") for value in reported)
        has_alternative = any(
            abs(value - alternative) <= Decimal("0.15") for value in reported
        )
        if has_alternative and not has_expected:
            violations.append(
                f"profitability_ratio_numerator:{ratio}:must_use_net_profit"
            )

    # Also catch prose that introduces one operating-profit numerator for both
    # ratios before the first `ROA`/`ROE` mention.
    for clause in _answer_clauses(answer):
        if not any(alias in clause for alias in _OPERATING_PROFIT_FACT_ALIASES):
            continue
        if any(marker in clause for marker in _OPERATING_NUMERATOR_NEGATION_MARKERS):
            continue
        if not any(marker in clause for marker in _OPERATING_NUMERATOR_USE_MARKERS):
            continue
        for ratio in ("roa", "roe"):
            if re.search(rf"\b{ratio}\b", clause):
                violations.append(
                    f"profitability_ratio_numerator:{ratio}:must_use_net_profit"
                )
    return list(dict.fromkeys(violations))


def _hard_comprehensive_profitability_requested(
    plan_context: Any,
    user_query: Any,
) -> bool:
    plan = _coerce_plan_context(plan_context)
    if normalize_financial_text(plan.get("difficulty_level", "")) != "hard":
        return False
    surface = normalize_financial_text(f"{_nested_text(plan)} {user_query}")
    return (
        "kha nang sinh loi" in normalize_financial_text(user_query)
        and "agent_profitability" in surface
        and "agent_cashflow_analysis" in surface
        and "agent_efficiency" in surface
    )


def _hard_net_margin_requested(plan_context: Any, user_query: Any) -> bool:
    plan = _coerce_plan_context(plan_context)
    if normalize_financial_text(plan.get("difficulty_level", "")) != "hard":
        return False
    surface = normalize_financial_text(f"{_nested_text(plan)} {user_query}")
    explicit_request = (
        any(marker in surface for marker in _PROFITABILITY_CONTEXT_MARKERS)
        and any(alias in surface for alias in _NET_MARGIN_ALIASES)
    )
    return explicit_request or _hard_comprehensive_profitability_requested(
        plan_context,
        user_query,
    )


def _financial_answer_segments(answer: Any) -> list[tuple[str, str]]:
    raw_segments = [
        segment.strip()
        for segment in re.split(r"(?:\n+|(?<=[.!?;:])\s+)", str(answer or ""))
        if segment.strip()
    ]
    return [(raw, normalize_financial_text(raw)) for raw in raw_segments]


def _segment_has_missing_language(segment: str) -> bool:
    return any(marker in segment for marker in _MISSING_MARKERS)


def _balance_total_value_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    """Reject a reconstructed balance total that conflicts with its main row."""

    contracts = {
        "current_assets": (
            ("tong tai san ngan han",),
            ("tong tai san ngan han", "current assets"),
        ),
        "current_liabilities": (
            ("tong no ngan han",),
            ("tong no ngan han", "current liabilities"),
        ),
    }
    violations: list[str] = []
    for metric, (fact_aliases, answer_aliases) in contracts.items():
        selected = _select_typed_metric_fact(
            facts,
            fact_aliases,
            "current",
            preferred_tables=("bang can doi ke toan",),
        )
        if selected is None:
            continue
        expected = selected[1]
        for _raw, normalized in _financial_answer_segments(answer):
            alias = next(
                (item for item in answer_aliases if item in normalized),
                "",
            )
            if not alias:
                continue
            tail = normalized.split(alias, 1)[1]
            match = re.search(
                r"(?:=|\bla\b|\bdat\b)\s*\(?\s*"
                r"([+-]?\d[\d.,\s]*)\)?\s*(?:vnd|dong)\b",
                tail,
            )
            if match is None:
                continue
            claimed = parse_financial_decimal(match.group(1).strip())
            if claimed is not None and claimed != expected:
                violations.append(f"balance_total_value_mismatch:{metric}")
                break
    return violations


def _liquidity_ratio_value_violations(
    answer: Any,
    payload: Any,
) -> list[str]:
    """Compare stated liquidity/leverage ratios with the canonical operands."""

    canonical = canonical_profitability_metrics(payload)
    metrics = canonical.get("metrics", {}) or {}
    aliases = {
        "current_ratio": ("current ratio", "he so thanh toan hien hanh"),
        "quick_ratio": ("quick ratio", "he so thanh toan nhanh"),
        "cash_ratio": ("cash ratio", "he so thanh toan tien mat"),
        "debt_to_equity": ("debt-to-equity", "debt to equity", "d/e"),
    }
    violations: list[str] = []
    for metric_key, metric_aliases in aliases.items():
        expected = parse_financial_decimal(
            (metrics.get(metric_key, {}) or {}).get("value")
        )
        if expected is None:
            continue
        for _raw, normalized in _financial_answer_segments(answer):
            alias = next((item for item in metric_aliases if item in normalized), "")
            if not alias:
                continue
            tail = normalized.split(alias, 1)[1]
            match = re.search(
                r"(?:=|:)?[^0-9+-]{0,35}([+-]?\d+(?:[.,]\d+)?)\s*(?:x|lan)?\b",
                tail,
            )
            if match is None:
                continue
            claimed = parse_financial_decimal(match.group(1))
            if claimed is not None and abs(claimed - expected) > Decimal("0.015"):
                violations.append(f"liquidity_ratio_value_mismatch:{metric_key}")
                break
    return violations


def _net_margin_has_numeric_result(answer: Any) -> bool:
    segments = _financial_answer_segments(answer)
    for index, (raw, normalized) in enumerate(segments):
        if not any(alias in normalized for alias in _NET_MARGIN_ALIASES):
            continue
        combined_raw = raw
        combined_normalized = normalized
        if index + 1 < len(segments):
            combined_raw += " " + segments[index + 1][0]
            combined_normalized += " " + segments[index + 1][1]
        if _segment_has_missing_language(combined_normalized):
            continue
        if re.search(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?\s*%", combined_raw):
            return True
    return False


def _hard_profitability_coverage_violations(
    answer: Any,
    payload: Any,
    plan_context: Any,
    user_query: Any,
) -> list[str]:
    if not _hard_net_margin_requested(plan_context, user_query):
        return []
    answer_text = normalize_financial_text(answer)
    if not any(alias in answer_text for alias in _NET_MARGIN_ALIASES):
        return ["profitability_coverage:net_margin_required_by_objective"]
    if canonical_profitability_metrics(payload) and not _net_margin_has_numeric_result(answer):
        return ["profitability_coverage:net_margin_numeric_result_required"]
    return []


def _asset_turnover_has_numeric_result(answer: Any) -> bool:
    segments = _financial_answer_segments(answer)
    for index, (raw, normalized) in enumerate(segments):
        if not any(alias in normalized for alias in _ASSET_TURNOVER_ALIASES):
            continue
        combined_raw = raw
        combined_normalized = normalized
        if index + 1 < len(segments):
            combined_raw += " " + segments[index + 1][0]
            combined_normalized += " " + segments[index + 1][1]
        if _segment_has_missing_language(combined_normalized):
            continue
        if re.search(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?\s*(?:x|lần|lan)\b", combined_raw, re.I):
            return True
        if re.search(r"(?:=|đạt|dat|từ|tu)\s*[+-]?\d+(?:[.,]\d+)?\b", combined_raw, re.I):
            return True
    return False


def _efficiency_coverage_violations(
    answer: Any,
    payload: Any,
    plan_context: Any,
    user_query: Any,
) -> list[str]:
    if not _hard_comprehensive_profitability_requested(plan_context, user_query):
        return []
    if not canonical_profitability_metrics(payload):
        return []
    answer_text = normalize_financial_text(answer)
    if not any(alias in answer_text for alias in _ASSET_TURNOVER_ALIASES):
        return ["profitability_coverage:asset_turnover_required"]
    if not _asset_turnover_has_numeric_result(answer):
        return ["profitability_coverage:asset_turnover_numeric_result_required"]
    return []


def _asset_turnover_comparative_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    comparative_markers = (
        "cai thien",
        "suy giam",
        "tang",
        "giam",
        "tot hon",
        "xau hon",
        "so voi nam truoc",
        "so voi ky truoc",
    )
    comparative_claim = False
    for _raw, normalized in _financial_answer_segments(answer):
        if not any(alias in normalized for alias in _ASSET_TURNOVER_ALIASES):
            continue
        if any(marker in normalized for marker in comparative_markers):
            comparative_claim = True
            break
    if not comparative_claim:
        return []

    has_prior_turnover_fact = any(
        _fact_is_found(fact)
        and _fact_period_role(fact) == "previous"
        and _fact_value(fact) is not None
        and any(alias in _fact_primary_text(fact) for alias in _ASSET_TURNOVER_ALIASES)
        for fact in facts
    )
    if has_prior_turnover_fact:
        return []
    return ["profitability_coverage:asset_turnover_comparison_unsupported"]


def _answer_has_profitability_comparative_direction(answer: Any) -> bool:
    profitability_aliases = (
        *_NET_MARGIN_ALIASES,
        *_NET_PROFIT_FACT_ALIASES,
        "loi nhuan sau thue",
        *_NET_REVENUE_FACT_ALIASES,
    )
    for raw, normalized in _financial_answer_segments(answer):
        if not any(alias in normalized for alias in profitability_aliases):
            continue
        if not any(marker in normalized for marker in _PROFITABILITY_DIRECTION_MARKERS):
            continue
        has_prior_context = any(
            marker in normalized for marker in _PRIOR_COMPARISON_MARKERS
        ) or len(set(re.findall(r"\b(?:19|20)\d{2}\b", normalized))) >= 2
        if not has_prior_context:
            continue
        if re.search(r"(?<!\w)[+-]?\d[\d.,]*\s*(?:%|diem phan tram|pp)\b", raw, re.I):
            return True
        if len(re.findall(r"(?<!\w)[+-]?\d[\d.,]*", raw)) >= 2:
            return True
    return False


def _reported_percentage_point_values(text: Any) -> list[Decimal]:
    values: list[Decimal] = []
    for match in re.finditer(
        r"(?<!\w)([+-]?\d+(?:[.,]\d+)?)\s*(?:diem phan tram|pp)\b",
        normalize_financial_text(text),
    ):
        value = parse_financial_decimal(match.group(1))
        if value is not None:
            values.append(abs(value))
    return values


def _margin_comparison_is_quantified(
    answer: Any,
    aliases: tuple[str, ...],
    *,
    current_value: Decimal,
    previous_value: Decimal,
    change_value: Decimal,
) -> bool:
    answer_text = normalize_financial_text(answer)
    if not any(alias in answer_text for alias in aliases):
        return False
    percentages = _reported_percentage_values(str(answer))
    point_changes = _reported_percentage_point_values(answer)
    tolerance = Decimal("0.06")
    return (
        any(abs(value - current_value) <= tolerance for value in percentages)
        and any(abs(value - previous_value) <= tolerance for value in percentages)
        and any(abs(value - abs(change_value)) <= tolerance for value in point_changes)
    )


def _profitability_comparative_violations(
    answer: Any,
    payload: Any,
    plan_context: Any,
    user_query: Any,
) -> list[str]:
    if not _hard_comprehensive_profitability_requested(plan_context, user_query):
        return []
    calculation = canonical_profitability_metrics(payload)
    metrics = calculation.get("metrics", {}) or {}
    comparatives = calculation.get("comparatives", {}) or {}
    if "net_margin_change" not in comparatives:
        return []
    violations: list[str] = []
    if not _answer_has_profitability_comparative_direction(answer):
        violations.append("profitability_coverage:comparative_direction_required")

    margin_contracts = (
        ("net_margin", _NET_MARGIN_ALIASES),
        ("gross_margin", _GROSS_MARGIN_ALIASES),
        ("operating_margin", _OPERATING_MARGIN_ALIASES),
    )
    for margin_key, aliases in margin_contracts:
        current = parse_financial_decimal((metrics.get(margin_key, {}) or {}).get("value"))
        previous = parse_financial_decimal(
            (comparatives.get(f"{margin_key}_previous", {}) or {}).get("value")
        )
        change = parse_financial_decimal(
            (comparatives.get(f"{margin_key}_change", {}) or {}).get("value")
        )
        if current is None or previous is None or change is None:
            continue
        if not _margin_comparison_is_quantified(
            answer,
            aliases,
            current_value=current,
            previous_value=previous,
            change_value=change,
        ):
            violations.append(
                f"profitability_coverage:{margin_key}_comparison_quantification_required"
            )
    return violations


def _hard_cfo_pat_comparison_requested(plan_context: Any) -> bool:
    plan = _coerce_plan_context(plan_context)
    if normalize_financial_text(plan.get("difficulty_level", "")) != "hard":
        return False
    surface = _nested_text(plan)
    return (
        "agent_profitability" in surface
        and "agent_cashflow_analysis" in surface
    )


def _answer_has_cfo_pat_comparison(answer: Any) -> bool:
    pat_aliases = (*_NET_PROFIT_FACT_ALIASES, "loi nhuan sau thue", "lai rong", "pat")
    for raw, normalized in _financial_answer_segments(answer):
        has_cfo = any(alias in normalized for alias in _CASHFLOW_ALIASES["cfo"])
        has_pat = any(alias in normalized for alias in pat_aliases)
        if not has_cfo or not has_pat or _segment_has_missing_language(normalized):
            continue
        if not any(marker in normalized for marker in _CFO_PAT_COMPARISON_MARKERS):
            continue
        if len(re.findall(r"(?<!\w)[+-]?\d[\d.,]*", raw)) >= 2:
            return True
    return False


def _cashflow_profit_quality_violations(
    answer: Any,
    payload: Any,
    plan_context: Any,
) -> list[str]:
    if not _hard_cfo_pat_comparison_requested(plan_context):
        return []
    facts = list(iter_financial_facts(payload))
    if "cfo" not in cashflow_fact_values(payload) or not _has_profit_after_tax_fact(facts):
        return []
    if _answer_has_cfo_pat_comparison(answer):
        return []
    return ["profitability_coverage:cfo_pat_comparison_required"]


def _negative_profit_cfo_pat_semantic_violations(
    answer: Any,
    payload: Any,
) -> list[str]:
    """A CFO/PAT ratio with a negative PAT cannot measure conversion quality."""

    canonical = canonical_profitability_metrics(payload)
    metric = (canonical.get("metrics", {}) or {}).get("cfo_to_net_profit", {})
    if not str(metric.get("interpretation_guard", "") or "").startswith(
        "net_profit_non_positive"
    ):
        return []
    text = normalize_financial_text(answer)
    risky_claim = bool(
        "loi nhuan ke toan khong phan anh day du kha nang sinh tien" in text
        or re.search(r"\bdua vao\b.{0,80}\bphi tien mat\b", text)
        or re.search(r"\bphi tien mat\b.{0,80}\bdua vao\b", text)
        or (
            "chat luong loi nhuan kem" in text
            and "cfo" in text
            and ("pat" in text or "loi nhuan sau thue" in text)
        )
    )
    if risky_claim:
        return ["cashflow_quality:cfo_pat_negative_profit_not_conversion_measure"]
    return []


def _complete_cfo_pat_history(payload: Any) -> dict[str, tuple[dict[str, Any], Decimal]]:
    """Return a compatible current/prior CFO+PAT quartet, if available."""

    facts = list(iter_financial_facts(payload))
    selections = {
        "cfo": _select_typed_metric_fact(
            facts,
            _CASHFLOW_ALIASES["cfo"],
            "current",
            preferred_tables=("bao cao luu chuyen tien te",),
        ),
        "cfo_previous": _select_typed_metric_fact(
            facts,
            _CASHFLOW_ALIASES["cfo"],
            "previous",
            preferred_tables=("bao cao luu chuyen tien te",),
        ),
        "net_profit": _select_typed_metric_fact(
            facts,
            _NET_PROFIT_FACT_ALIASES,
            "current",
            excluded_aliases=("chua phan phoi",),
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
        ),
        "net_profit_previous": _select_typed_metric_fact(
            facts,
            _NET_PROFIT_FACT_ALIASES,
            "previous",
            excluded_aliases=("chua phan phoi",),
            preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
        ),
    }
    if any(selection is None for selection in selections.values()):
        return {}
    selected = {
        key: selection
        for key, selection in selections.items()
        if selection is not None
    }
    units = {
        normalize_financial_text(fact.get("unit", ""))
        for fact, _value in selected.values()
        if str(fact.get("unit", "") or "").strip()
    }
    if len(units) != 1 or any(value == 0 for _fact, value in selected.values()):
        return {}
    if not _profitability_facts_are_compatible(selected):
        return {}
    return selected


def _answer_has_cfo_pat_comparative_direction(
    answer: Any,
    history: dict[str, tuple[dict[str, Any], Decimal]],
) -> bool:
    if not history:
        return False
    with localcontext() as context:
        context.prec = 50
        current_ratio = history["cfo"][1] / history["net_profit"][1] * Decimal(100)
        previous_ratio = (
            history["cfo_previous"][1]
            / history["net_profit_previous"][1]
            * Decimal(100)
        )
    direction = "tang" if current_ratio > previous_ratio else "giam" if current_ratio < previous_ratio else "khong doi"
    for _raw, normalized in _financial_answer_segments(answer):
        if "cfo" not in normalized or "pat" not in normalized:
            continue
        if not any(
            marker in normalized
            for marker in (*_PROFITABILITY_DIRECTION_MARKERS, "tu", "xuong", "len")
        ):
            continue
        reported = _reported_percentage_values(normalized)
        has_current = any(abs(value - current_ratio) <= Decimal("0.20") for value in reported)
        has_previous = any(abs(value - previous_ratio) <= Decimal("0.20") for value in reported)
        if has_current and has_previous and direction in normalized:
            return True
    return False


def _cashflow_profit_quality_comparative_violations(
    answer: Any,
    payload: Any,
    plan_context: Any,
    user_query: Any,
) -> list[str]:
    """Require prior/current CFO/PAT direction only for broad hard analysis."""

    if not _hard_comprehensive_profitability_requested(plan_context, user_query):
        return []
    history = _complete_cfo_pat_history(payload)
    if not history:
        return []
    if _answer_has_cfo_pat_comparative_direction(answer, history):
        return []
    return [
        "profitability_coverage:cfo_pat_comparative_direction_required"
    ]


def _cashflow_comparative_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    current = _select_typed_metric_fact(
        facts,
        _CASHFLOW_ALIASES["cfo"],
        "current",
        preferred_tables=("bao cao luu chuyen tien te",),
        reporting_basis="full_period",
    )
    previous = _select_typed_metric_fact(
        facts,
        _CASHFLOW_ALIASES["cfo"],
        "previous",
        preferred_tables=("bao cao luu chuyen tien te",),
        reporting_basis="full_period",
    )
    if current is None or previous is None or previous[1] == 0:
        return []
    units = {
        normalize_financial_text(selection[0].get("unit", ""))
        for selection in (current, previous)
        if str(selection[0].get("unit", "") or "").strip()
    }
    if len(units) > 1:
        return []

    with localcontext() as context:
        context.prec = 50
        change = (current[1] - previous[1]) / abs(previous[1]) * Decimal(100)
    expected_direction = "tang" if change > 0 else "giam" if change < 0 else ""
    sign_crossing = bool(
        (current[1] > 0 > previous[1])
        or (current[1] < 0 < previous[1])
    )
    if not expected_direction:
        return []

    for clause in _answer_clauses(answer):
        if not any(alias in clause for alias in _CASHFLOW_ALIASES["cfo"]):
            continue
        # Do not interpret the ratio transition in ``CFO/PAT giảm từ 95,50%
        # xuống 82,17%`` as a claim that the CFO amount changed by 95,50%.
        # Only a standalone CFO (or the full cash-flow label) is an amount
        # change claim for this validator.
        claim = re.search(
            r"\bcfo\b(?!\s*/\s*pat)[^%]{0,140}?"
            r"\b(tang|giam)\b[^%]{0,140}?([+-]?\d+(?:[.,]\d+)?)\s*%",
            clause,
        )
        if claim is None:
            non_abbreviation_aliases = tuple(
                alias
                for alias in _CASHFLOW_ALIASES["cfo"]
                if alias != "cfo"
            )
            for alias in non_abbreviation_aliases:
                claim = re.search(
                    rf"\b{re.escape(alias)}\b[^%]{{0,140}}?"
                    r"\b(tang|giam)\b[^%]{0,140}?"
                    r"([+-]?\d+(?:[.,]\d+)?)\s*%",
                    clause,
                )
                if claim is not None:
                    break
        if not claim:
            continue
        if sign_crossing:
            return ["cashflow_comparison:cfo_sign_crossing_percent_misleading"]
        if claim.group(1) != expected_direction:
            return ["cashflow_comparison:cfo_change_direction_mismatch"]
        claimed_change = parse_financial_decimal(claim.group(2))
        if (
            claimed_change is not None
            and abs(abs(claimed_change) - abs(change)) > Decimal("0.15")
        ):
            return ["cashflow_comparison:cfo_change_percent_mismatch"]
    return []


def _cashflow_amount_direction_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    """Protect signed CFI/CFF amount changes from magnitude/sign confusion."""

    violations: list[str] = []
    for metric in ("cfi", "cff"):
        current = _select_typed_metric_fact(
            facts,
            _CASHFLOW_ALIASES[metric],
            "current",
            preferred_tables=("bao cao luu chuyen tien te",),
            reporting_basis="full_period",
        )
        previous = _select_typed_metric_fact(
            facts,
            _CASHFLOW_ALIASES[metric],
            "previous",
            preferred_tables=("bao cao luu chuyen tien te",),
            reporting_basis="full_period",
        )
        if current is None or previous is None:
            continue
        delta = current[1] - previous[1]
        expected = "tang" if delta > 0 else "giam" if delta < 0 else ""
        if not expected:
            continue
        for clause in _answer_clauses(answer):
            if not any(alias in clause for alias in _CASHFLOW_ALIASES[metric]):
                continue
            direction_match = re.search(r"\b(tang|giam)\b", clause)
            if direction_match is None:
                continue
            # "mức âm giảm" explicitly discusses absolute negative magnitude,
            # not the signed subtotal itself, and is therefore not a mismatch.
            if "muc am" in clause or "do am" in clause:
                continue
            if direction_match.group(1) != expected:
                violations.append(
                    f"cashflow_comparison:{metric}_amount_expected_{expected}"
                )
                break
    return violations


def _operating_profit_semantic_violations(answer: Any) -> list[str]:
    for _raw, normalized in _financial_answer_segments(answer):
        if "ebit" not in normalized:
            continue
        if any(marker in normalized for marker in _EBIT_NEGATION_MARKERS):
            continue
        if any(alias in normalized for alias in _OPERATING_PROFIT_ALIASES):
            return ["profitability_semantics:operating_profit_is_not_ebit"]
    return []


def _operating_profit_direction_violations(
    answer: Any,
    facts: list[dict[str, Any]],
) -> list[str]:
    current = _select_typed_metric_fact(
        facts,
        _OPERATING_PROFIT_FACT_ALIASES,
        "current",
        preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
    )
    previous = _select_typed_metric_fact(
        facts,
        _OPERATING_PROFIT_FACT_ALIASES,
        "previous",
        preferred_tables=("bao cao ket qua hoat dong kinh doanh",),
    )
    if current is None or previous is None or current[1] == previous[1]:
        return []
    expected_direction = "tang" if current[1] > previous[1] else "giam"
    contradictory_direction = "giam" if expected_direction == "tang" else "tang"

    for clause in _answer_clauses(answer):
        operating_matches = [
            match
            for alias in _OPERATING_PROFIT_FACT_ALIASES
            for match in re.finditer(rf"\b{re.escape(alias)}\b", clause)
        ]
        if not operating_matches:
            continue
        operating_end = min(match.end() for match in operating_matches)
        amount_scope = clause[operating_end:]
        margin_positions = [
            position
            for alias in _OPERATING_MARGIN_ALIASES
            if (position := amount_scope.find(alias)) >= 0
        ]
        if margin_positions:
            amount_scope = amount_scope[: min(margin_positions)]
        direction_match = re.search(r"\b(tang|giam)\b", amount_scope)
        if direction_match and direction_match.group(1) == contradictory_direction:
            return [
                "profitability_trend:operating_profit_amount_expected_"
                + ("increase" if expected_direction == "tang" else "decrease")
            ]
    return []


def _flow_balance_label_roles(prefix: str, suffix: str) -> tuple[bool, bool]:
    ending_label = bool(
        re.search(
            r"(?:\((?:so )?cuoi (?:ky|nam)\)|"
            r"\b(?:so )?cuoi (?:ky|nam))\s*$",
            prefix,
        )
        or re.search(
            r"^\s*(?:[|,:-]\s*)?"
            r"(?:(?:nam\s+)?(?:19|20)\d{2}\s*(?:vnd|dong)?\s*)?"
            r"(?:\((?:so )?cuoi (?:ky|nam)\)|"
            r"(?:so )?cuoi (?:ky|nam)\b)",
            suffix,
        )
    )
    opening_label = bool(
        re.search(
            r"(?:\((?:so )?dau (?:ky|nam)\)|"
            r"\b(?:so )?dau (?:ky|nam))\s*$",
            prefix,
        )
        or re.search(
            r"^\s*(?:[|,:-]\s*)?"
            r"(?:(?:nam\s+)?(?:19|20)\d{2}\s*(?:vnd|dong)?\s*)?"
            r"(?:\((?:so )?dau (?:ky|nam)\)|"
            r"(?:so )?dau (?:ky|nam)\b)",
            suffix,
        )
    )
    return ending_label, opening_label


def _flow_period_label_violations(
    answer: Any,
    aliases_by_metric: Mapping[str, tuple[str, ...]],
    *,
    code_prefix: str,
) -> list[str]:
    violations: list[str] = []
    for _raw, normalized in _financial_answer_segments(answer):
        mentions: list[tuple[str, int, int]] = []
        for candidate, aliases in aliases_by_metric.items():
            for alias in aliases:
                mentions.extend(
                    (candidate, match.start(), match.end())
                    for match in re.finditer(rf"\b{re.escape(alias)}\b", normalized)
                )
        for metric, start, end in mentions:
            # Scope the label to the metric itself.  A later statement such as
            # "CFO làm tăng số dư tiền cuối kỳ" is valid and must not be
            # mistaken for relabeling the annual CFO as an ending balance.
            prefix = normalized[max(0, start - 25) : start]
            suffix = normalized[end : min(len(normalized), end + 55)]
            ending_label, opening_label = _flow_balance_label_roles(prefix, suffix)
            if ending_label:
                violations.append(
                    f"{code_prefix}:{metric}:annual_flow_not_ending_balance"
                )
            if opening_label:
                violations.append(
                    f"{code_prefix}:{metric}:annual_flow_not_opening_balance"
                )
    return list(dict.fromkeys(violations))


def _cashflow_period_label_violations(answer: Any) -> list[str]:
    return _flow_period_label_violations(
        answer,
        {
            metric: _CASHFLOW_ALIASES[metric]
            for metric in ("cfo", "cfi", "cff")
        },
        code_prefix="cashflow_period_label",
    )


def _income_statement_period_label_violations(answer: Any) -> list[str]:
    return _flow_period_label_violations(
        answer,
        _INCOME_STATEMENT_FLOW_ALIASES,
        code_prefix="income_statement_period_label",
    )


def _has_explicit_benchmark(answer_text: str) -> bool:
    if any(marker in answer_text for marker in _EXPLICIT_BENCHMARK_MARKERS):
        return True
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", answer_text))
    return len(years) >= 2


def _profitability_benchmark_violations(answer: Any) -> list[str]:
    for clause in _answer_clauses(answer):
        for level in _LEVEL_MARKERS:
            for match in re.finditer(rf"\b{level}\b", clause):
                # ``cao`` is also the final token in the common phrase
                # ``báo cáo``; it is not an absolute profitability level.
                if level == "cao" and clause[max(0, match.start() - 4) : match.start()] == "bao ":
                    continue
                prefix = clause[: match.start()]
                # A deterministic fallback explicitly says that it is *not*
                # assigning an absolute level without a benchmark (for
                # example, "không gắn nhãn cao/thấp ...").  That caveat is a
                # prohibition, not a profitability claim; do not reject it as
                # though the answer had asserted the level.
                negation_surface = prefix[-48:]
                if re.search(
                    r"(?:khong|chua)\s+(?:gan|goi|xep|danh)\s+nhan"
                    r"(?:\s+\w+)?/?\s*$|"
                    r"(?:khong|chua)\s+(?:ket luan|danh gia)\s*$",
                    negation_surface,
                ):
                    continue
                profitability_subject = max(
                    (prefix.rfind(marker) for marker in _PROFITABILITY_CLAIM_MARKERS),
                    default=-1,
                )
                other_subject = max(
                    (
                        prefix.rfind(marker)
                        for marker in _NON_PROFITABILITY_LEVEL_SUBJECTS
                    ),
                    default=-1,
                )
                if profitability_subject > other_subject:
                    separators = [
                        separator
                        for separator in re.finditer(r",\s+", clause)
                    ]
                    left_matches = [
                        separator
                        for separator in separators
                        if separator.start() < profitability_subject
                    ]
                    right_matches = [
                        separator
                        for separator in separators
                        if separator.start() >= match.end()
                    ]
                    left = left_matches[-1].start() if left_matches else -1
                    left_end = left_matches[-1].end() if left_matches else 0
                    right = right_matches[0].start() if right_matches else -1
                    claim_surface = clause[
                        left_end:
                        len(clause) if right < 0 else right
                    ]
                    if _has_explicit_benchmark(claim_surface):
                        continue
                    if left >= 0:
                        previous_left = clause.rfind(",", 0, left)
                        benchmark_prefix = clause[
                            0 if previous_left < 0 else previous_left + 1 : left
                        ]
                        if _has_explicit_benchmark(benchmark_prefix):
                            continue
                    return ["profitability_level:explicit_benchmark_required"]
    return []


def financial_answer_violations(
    answer: Any,
    payload: Any,
    *,
    user_query: Any = "",
    plan_context: Any = None,
) -> list[str]:
    """Validate high-risk financial claims against the structured fact ledger."""

    answer_text = normalize_financial_text(answer)
    facts = list(iter_financial_facts(payload))
    violations: list[str] = []

    has_vnd_amounts = any(
        _fact_is_found(fact)
        and normalize_financial_text(fact.get("unit", "")) in {"vnd", "dong"}
        and _fact_value(fact) is not None
        for fact in facts
    )
    if (
        has_vnd_amounts
        and not _query_requests_compact_amounts(user_query)
        and _COMPACT_AMOUNT_RE.search(answer_text)
    ):
        violations.append("unexpected_compact_vnd_unit")

    cash_values = cashflow_fact_values(payload)
    for clause in _answer_clauses(answer):
        for metric, claim_segment in _cashflow_claim_segments(clause):
            value = cash_values.get(metric)
            if value is None:
                continue
            negative_claim = any(
                marker in claim_segment for marker in _NEGATIVE_CASHFLOW_MARKERS
            )
            positive_claim = any(
                marker in claim_segment for marker in _POSITIVE_CASHFLOW_MARKERS
            )
            if value > 0 and negative_claim:
                violations.append(f"cashflow_sign:{metric}:expected_positive")
            if value < 0 and positive_claim:
                violations.append(f"cashflow_sign:{metric}:expected_negative")
    violations.extend(_shared_cashflow_sign_violations(answer, cash_values))
    violations.extend(_cashflow_effect_direction_violations(answer, cash_values))
    violations.extend(_balance_total_value_violations(answer, facts))
    violations.extend(_liquidity_ratio_value_violations(answer, payload))
    violations.extend(
        _linked_note_ref_coverage_violations(answer, facts, plan_context)
    )

    available_metrics = _available_metric_keys(facts)
    canonical_profitability = canonical_profitability_metrics(payload)
    for clause in _answer_clauses(answer):
        if not any(marker in clause for marker in _MISSING_MARKERS):
            continue
        for metric in sorted(available_metrics):
            if any(alias in clause for alias in _METRIC_ALIASES[metric]):
                violations.append(f"false_missing:{metric}")
        if (
            "net_revenue" in available_metrics
            and re.search(r"\bdoanh thu\b", clause)
            and any(
                alias in clause
                for alias in _NET_REVENUE_DEPENDENT_CALCULATION_ALIASES
            )
        ):
            violations.append("false_missing:net_revenue")
        if canonical_profitability:
            if re.search(r"\broe\b", clause) or "von chu so huu" in clause:
                violations.extend(("false_missing:equity", "false_missing:roe"))
            if re.search(r"\broa\b", clause) or "tong tai san" in clause:
                violations.extend(("false_missing:total_assets", "false_missing:roa"))

    violations.extend(_net_profit_semantic_violations(answer, facts))
    violations.extend(_annual_ratio_basis_violations(answer, facts))
    violations.extend(
        _profitability_ratio_numerator_violations(answer, payload, facts)
    )
    violations.extend(
        _hard_profitability_coverage_violations(
            answer,
            payload,
            plan_context,
            user_query,
        )
    )
    violations.extend(
        _profitability_comparative_violations(
            answer,
            payload,
            plan_context,
            user_query,
        )
    )
    violations.extend(
        _efficiency_coverage_violations(
            answer,
            payload,
            plan_context,
            user_query,
        )
    )
    violations.extend(_asset_turnover_comparative_violations(answer, facts))
    violations.extend(
        _cashflow_profit_quality_violations(answer, payload, plan_context)
    )
    violations.extend(
        _negative_profit_cfo_pat_semantic_violations(answer, payload)
    )
    violations.extend(
        _cashflow_profit_quality_comparative_violations(
            answer,
            payload,
            plan_context,
            user_query,
        )
    )
    violations.extend(_cashflow_comparative_violations(answer, facts))
    violations.extend(_cashflow_amount_direction_violations(answer, facts))
    violations.extend(_operating_profit_semantic_violations(answer))
    violations.extend(_operating_profit_direction_violations(answer, facts))
    violations.extend(_cashflow_period_label_violations(answer))
    violations.extend(_income_statement_period_label_violations(answer))
    violations.extend(_profitability_benchmark_violations(answer))

    identity_violation = cashflow_identity_violation(payload)
    if identity_violation:
        violations.append(identity_violation)

    return list(dict.fromkeys(violations))
