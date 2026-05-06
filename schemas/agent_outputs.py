"""Pydantic models and coercion helpers for planner, router, and worker outputs."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from typing import List, Literal, Any, Optional
import re, json
from agents.agent_registry import get_default_table, is_retrieval_agent
from schemas.requirements import normalize_fact_status
from schemas.table_names import normalize_table_heading

TABLE_NAME = Literal[
    "BẢNG CÂN ĐỐI KẾ TOÁN",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
]

TABLE_CANON = {
    "bảng cân đối kế toán": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "báo cáo kết quả hoạt động kinh doanh": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "báo cáo lưu chuyển tiền tệ": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "bcdkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "bcđkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "kqhđkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "kqhdkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "lctt": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "thuyết minh báo cáo tài chính": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh bao cao tai chinh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyết minh bctc": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh bctc": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyết minh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
}

VALID_TABLE_NAMES = set(TABLE_CANON.values())
AGENT_NAME_VALUES = {
    "agent_bs",
    "agent_is",
    "agent_cf",
    "agent_note",
    "agent_web",
    "agent_profitability",
    "agent_liquidity_solvency",
    "agent_cashflow_analysis",
    "agent_efficiency",
}
ANALYSIS_AXIS_ALIASES = {
    "profitability": "agent_profitability",
    "profit": "agent_profitability",
    "earnings": "agent_profitability",
    "agent_profitability": "agent_profitability",
    "liquidity": "agent_liquidity_solvency",
    "solvency": "agent_liquidity_solvency",
    "leverage": "agent_liquidity_solvency",
    "liquidity_solvency": "agent_liquidity_solvency",
    "agent_liquidity_solvency": "agent_liquidity_solvency",
    "cashflow": "agent_cashflow_analysis",
    "cash_flow": "agent_cashflow_analysis",
    "cashflow_analysis": "agent_cashflow_analysis",
    "cash_flow_analysis": "agent_cashflow_analysis",
    "cash_flow_quality": "agent_cashflow_analysis",
    "cashflow_quality": "agent_cashflow_analysis",
    "agent_cashflow_analysis": "agent_cashflow_analysis",
    "efficiency": "agent_efficiency",
    "capital_efficiency": "agent_efficiency",
    "operating_efficiency": "agent_efficiency",
    "asset_efficiency": "agent_efficiency",
    "agent_efficiency": "agent_efficiency",
}


def _normalize_table_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    # Accept common abbreviations from LLM output, but store only canonical
    # statement headings in validated plans and worker answers.
    text = " ".join(value.strip().split())
    if not text:
        return text

    key = text.lower()
    if key in TABLE_CANON:
        return TABLE_CANON[key]

    normalized = normalize_table_heading(text)
    if normalized in VALID_TABLE_NAMES:
        return normalized

    return text


def _is_agent_name(value: Any) -> bool:
    return str(value or "").strip() in AGENT_NAME_VALUES


def _flatten_text_items(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        items: List[str] = []
        for item in value:
            items.extend(_flatten_text_items(item))
        return items

    text = str(value).strip()
    if not text:
        return []
    return [text]


def _coerce_keyword_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return _dedupe_keep_order(_flatten_text_items(value))

    text = str(value).strip()
    if not text:
        return []

    # Router/planner models sometimes return comma-separated strings even when
    # the schema expects a list. Split only obvious list separators.
    if "," in text or ";" in text:
        split_items = re.split(r"[;,]", text)
        normalized = _dedupe_keep_order([item.strip() for item in split_items if item.strip()])
        if normalized:
            return normalized

    return [text]


def _coerce_text_list(value: Any) -> List[str]:
    return _dedupe_keep_order(_flatten_text_items(value))


def _coerce_worker_fact_item(value: Any) -> Optional[dict]:
    if value is None:
        return None

    if isinstance(value, dict):
        return dict(value)

    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped

    # Preserve legacy worker output that returns facts as plain strings.
    text = str(value).strip()
    if not text:
        return None

    normalized = " ".join(text.split())
    item_name = ""
    fact_value = normalized

    colon_parts = re.split(r"\s*:\s*", normalized, maxsplit=1)
    if len(colon_parts) == 2:
        item_name = colon_parts[0].strip(" -")
        fact_value = colon_parts[1].strip()
    else:
        la_match = re.match(r"^(?P<item>.+?)\s+là\s+(?P<value>.+)$", normalized, flags=re.IGNORECASE)
        if la_match:
            item_name = la_match.group("item").strip(" -")
            fact_value = la_match.group("value").strip()
        else:
            digit_match = re.search(r"\d", normalized)
            if digit_match and digit_match.start() > 0:
                prefix = normalized[:digit_match.start()].strip(" ,.;:-")
                if prefix:
                    item_name = prefix

    return {
        "item_name": item_name,
        "time_hint": "",
        "value": fact_value,
        "source": "",
        "status": "found",
    }


def _coerce_worker_facts(value: Any) -> List[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        items = value
    else:
        items = [value]

    normalized: List[Any] = []
    for item in items:
        coerced = _coerce_worker_fact_item(item)
        if coerced is not None:
            normalized.append(coerced)
    return normalized


def _coerce_followup_dict(value: dict) -> dict:
    data = dict(value)

    if "agent" not in data and data.get("agent_name"):
        data["agent"] = data.get("agent_name")

    if "keywords" not in data:
        for key in ("keyword", "kw"):
            if data.get(key):
                data["keywords"] = data.get(key)
                break

    if "keywords" in data:
        data["keywords"] = _coerce_keyword_list(data.get("keywords"))

    if "requirements" not in data:
        for key in ("requirement", "missing_requirement", "missing_requirements", "missing", "needs"):
            if data.get(key):
                data["requirements"] = data.get(key)
                break

    if "requirements" in data:
        data["requirements"] = _coerce_text_list(data.get("requirements"))

    return data


def _coerce_followup_sequence(items: List[Any]) -> Optional[dict]:
    if not items:
        return None

    agent = str(items[0] or "").strip()
    if agent not in AGENT_NAME_VALUES:
        return None

    table = None
    requirements: List[str] = []
    reason = ""
    cursor = 1

    if len(items) > cursor:
        candidate_table = _normalize_table_value(items[cursor])
        if candidate_table in VALID_TABLE_NAMES:
            table = candidate_table
            cursor += 1

    if len(items) > cursor:
        requirements = _coerce_text_list(items[cursor])
        cursor += 1

    if len(items) > cursor:
        reason = str(items[cursor] or "").strip()

    return {
        "agent": agent,
        "table": table,
        "requirements": requirements,
        "reason": reason,
    }


def _coerce_followups_payload(value: Any) -> Any:
    if value is None:
        return []

    if isinstance(value, dict):
        return [_coerce_followup_dict(value)]

    if not isinstance(value, list):
        return value

    if not value:
        return []

    normalized = []
    index = 0

    while index < len(value):
        item = value[index]

        if isinstance(item, dict):
            normalized.append(_coerce_followup_dict(item))
            index += 1
            continue

        if isinstance(item, (list, tuple)):
            seq = _coerce_followup_sequence(list(item))
            if seq is None:
                return value
            normalized.append(seq)
            index += 1
            continue

        if _is_agent_name(item):
            next_index = index + 1
            while next_index < len(value) and not _is_agent_name(value[next_index]):
                next_index += 1

            seq = _coerce_followup_sequence(value[index:next_index])
            if seq is None:
                return value
            normalized.append(seq)
            index = next_index
            continue

        return value

    return normalized


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out

AGENT_NAME = Literal[
    "agent_bs",
    "agent_is",
    "agent_cf",
    "agent_note",
    "agent_web",
    "agent_profitability",
    "agent_liquidity_solvency",
    "agent_cashflow_analysis",
    "agent_efficiency",
]
RETRIEVAL_AGENT = Literal[
    "agent_bs",
    "agent_is",
    "agent_cf",
    "agent_note",
    "agent_web",
]
ANALYSIS_AXIS = Literal[
    "agent_profitability",
    "agent_liquidity_solvency",
    "agent_cashflow_analysis",
    "agent_efficiency",
]

PLANNER_DIFFICULTY_LEVEL = Literal[
    "easy",
    "medium",
    "hard",
]


def _map_question_type_to_difficulty(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"easy", "medium", "hard"}:
        return text
    if text == "calculation":
        return "medium"
    if text in {"evaluation", "risk_assessment"}:
        return "hard"
    return "easy"


# ---------- Planner evidence plan ---------
class PlannerAnalysisAxis(BaseModel):
    axis: ANALYSIS_AXIS
    components: SkipJsonSchema[List[str]] = Field(default_factory=list, exclude=True)
    objective: str = ""

    @field_validator("axis", mode="before")
    @classmethod
    def normalize_axis(cls, v):
        if v is None:
            return ""
        text = str(v).strip()
        if not text:
            return text
        normalized = ANALYSIS_AXIS_ALIASES.get(text.lower())
        return normalized or text

    @field_validator("objective", mode="before")
    @classmethod
    def normalize_objective(cls, v):
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("components", mode="before")
    @classmethod
    def normalize_components(cls, v):
        if v is None:
            return []
        return [str(item).strip() for item in v if str(item).strip()]


class PlannerEvidencePlan(BaseModel):
    difficulty_level: PLANNER_DIFFICULTY_LEVEL = "easy"
    tables: SkipJsonSchema[List[TABLE_NAME]] = Field(default_factory=list, exclude=True)
    analysis_axes: List[PlannerAnalysisAxis] = Field(default_factory=list)
    required_components: SkipJsonSchema[List[str]] = Field(default_factory=list, exclude=True)
    company: Optional[str] = ""
    time_hint: Optional[str] = ""
    need_web: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_question_type(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)
        if "difficulty_level" not in data and "question_type" in data:
            data["difficulty_level"] = _map_question_type_to_difficulty(data.get("question_type"))
        return data

    @field_validator("difficulty_level", mode="before")
    @classmethod
    def normalize_difficulty_level(cls, value):
        return _map_question_type_to_difficulty(value)

    @field_validator("tables", mode="before")
    @classmethod
    def normalize_tables(cls, v):
        if v is None:
            return []
        out = []
        for item in v:
            if isinstance(item, dict) and "table" in item:
                item = item["table"]
            if isinstance(item, str):
                out.append(_normalize_table_value(item))
            else:
                out.append(item)
        return out

    @field_validator("company", "time_hint", mode="before")
    @classmethod
    def normalize_nullable_str(cls, v):
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("required_components", mode="before")
    @classmethod
    def normalize_required_components(cls, v):
        if v is None:
            return []
        return [str(item).strip() for item in v if str(item).strip()]

    @model_validator(mode="after")
    def consolidate_plan(self):
        tables = []
        seen_tables = set()
        normalized_axes = []

        for table in self.tables:
            if table not in seen_tables:
                tables.append(table)
                seen_tables.add(table)

        for axis in self.analysis_axes:
            axis_components = []
            axis_seen_components = set()
            for component in axis.components:
                if component not in axis_seen_components:
                    axis_components.append(component)
                    axis_seen_components.add(component)

            axis.components = axis_components
            normalized_axes.append(axis)

        if not normalized_axes and tables:
            inferred_axes = []
            for table in tables:
                if table == "BẢNG CÂN ĐỐI KẾ TOÁN":
                    inferred_axes.append("agent_liquidity_solvency")
                elif table == "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH":
                    inferred_axes.append("agent_profitability")
                elif table == "BÁO CÁO LƯU CHUYỂN TIỀN TỆ":
                    inferred_axes.append("agent_cashflow_analysis")

            seen_axes = set()
            for axis_name in inferred_axes or ["agent_profitability"]:
                if axis_name in seen_axes:
                    continue
                normalized_axes.append(
                    PlannerAnalysisAxis(
                        axis=axis_name,
                        components=list(self.required_components),
                        objective="",
                    )
                )
                seen_axes.add(axis_name)

        self.tables = tables
        self.analysis_axes = normalized_axes
        self.required_components = _dedupe_keep_order(self.required_components)
        return self


# ---------- Router / Dispatch plan ----------
class Target(BaseModel):
    agent: AGENT_NAME
    table: Optional[TABLE_NAME] = None
    requirements: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_target_fields(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)

        if "requirements" not in data and data.get("keywords"):
            data["requirements"] = data.get("keywords")

        if "agent" not in data and data.get("table"):
            inferred_table = _normalize_table_value(data.get("table"))
            if inferred_table == "BẢNG CÂN ĐỐI KẾ TOÁN":
                data["agent"] = "agent_bs"
            elif inferred_table == "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH":
                data["agent"] = "agent_is"
            elif inferred_table == "BÁO CÁO LƯU CHUYỂN TIỀN TỆ":
                data["agent"] = "agent_cf"
            elif inferred_table == "THUYẾT MINH BÁO CÁO TÀI CHÍNH":
                data["agent"] = "agent_note"

        return data

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_requirements(cls, value):
        return _coerce_text_list(value)

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value):
        return _coerce_keyword_list(value)

    @field_validator("table", mode="before")
    @classmethod
    def normalize_table(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = _normalize_table_value(value)
        if normalized in VALID_TABLE_NAMES:
            return normalized
        return None

    @model_validator(mode="after")
    def finalize_target_defaults(self):
        if self.table is None and is_retrieval_agent(self.agent):
            default_table = get_default_table(self.agent)
            if default_table in VALID_TABLE_NAMES:
                self.table = default_table

        if not self.requirements and self.keywords:
            self.requirements = _coerce_text_list(self.keywords)

        return self


class EvidencePlanItem(BaseModel):
    table: Optional[TABLE_NAME] = None
    query: str = ""
    needby: List[ANALYSIS_AXIS] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_evidence_fields(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)

        if "needby" not in data and "needed_by" in data:
            data["needby"] = data.get("needed_by")

        if not data.get("query"):
            for key in ("requirement", "keyword", "keywords", "requirements"):
                raw = data.get(key)
                items = _coerce_text_list(raw)
                if items:
                    data["query"] = items[0]
                    break

        if not data.get("table") and data.get("agent"):
            default_table = get_default_table(str(data.get("agent", "") or "").strip())
            if default_table in VALID_TABLE_NAMES:
                data["table"] = default_table

        return data

    @field_validator("table", mode="before")
    @classmethod
    def normalize_table(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = _normalize_table_value(value)
        if normalized in VALID_TABLE_NAMES:
            return normalized
        return None

    @field_validator("query", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("needby", mode="before")
    @classmethod
    def normalize_needby(cls, value):
        items = _coerce_text_list(value)
        normalized = []
        for item in items:
            agent = ANALYSIS_AXIS_ALIASES.get(str(item or "").strip().lower(), str(item or "").strip())
            if agent in ANALYSIS_AXIS_ALIASES.values() and agent not in normalized:
                normalized.append(agent)
        return normalized


class AnalysisPlanItem(BaseModel):
    agent: ANALYSIS_AXIS
    objective: str = ""

    @field_validator("agent", mode="before")
    @classmethod
    def normalize_agent(cls, value):
        text = str(value or "").strip()
        return ANALYSIS_AXIS_ALIASES.get(text.lower(), text)

    @field_validator("objective", mode="before")
    @classmethod
    def normalize_objective(cls, value):
        if value is None:
            return ""
        return str(value).strip()


class EvidenceDispatchPlan(BaseModel):
    evidence_plan: List[dict] = Field(default_factory=list)
    analysis_plan: List[dict] = Field(default_factory=list)
    targets: List[dict] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def coerce_current_router_output(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)

        for wrapper_key in (
            "evidence_dispatch_plan",
            "dispatch_plan",
            "router_plan",
            "worker_plan",
            "plan",
            "output",
            "data",
            "result",
        ):
            nested = data.get(wrapper_key)
            if isinstance(nested, dict):
                nested_data = dict(nested)
                nested_data.update({k: v for k, v in data.items() if k not in nested_data})
                data = nested_data
                break

        if "evidence_plan" not in data:
            for key in ("evidence", "evidence_items", "retrieval_plan", "retrieval_queries", "queries", "items"):
                if key in data:
                    data["evidence_plan"] = data.get(key)
                    break

        if "analysis_plan" not in data:
            for key in ("analysis", "analysis_items", "analyses"):
                if key in data:
                    data["analysis_plan"] = data.get(key)
                    break

        if "targets" not in data:
            for key in ("retrieval_targets", "workers", "worker_targets"):
                if key in data:
                    data["targets"] = data.get(key)
                    break

        return data

    @field_validator("evidence_plan", "analysis_plan", "targets", mode="before")
    @classmethod
    def normalize_router_items(cls, value):
        if value is None:
            return []
        if isinstance(value, dict):
            for key in ("items", "queries", "requirements", "targets", "evidence_plan", "analysis_plan"):
                nested = value.get(key)
                if isinstance(nested, list):
                    value = nested
                    break
            else:
                value = [value]
        elif not isinstance(value, list):
            value = [value]

        items = []
        for item in value:
            if isinstance(item, dict):
                items.append(dict(item))
                continue
            text = str(item or "").strip()
            if text:
                items.append({"query": text})
        return items


class WorkerFact(BaseModel):
    content_type: str = ""
    note_number: str = ""
    note_title: str = ""
    subheading: str = ""
    item_name: str = ""
    time_hint: str = ""
    value: str = ""
    interpretation_hint: str = ""
    source: str = ""
    status: Literal["found", "not_found_after_search", "ambiguous"] = "found"

    @field_validator(
        "content_type",
        "note_number",
        "note_title",
        "subheading",
        "item_name",
        "time_hint",
        "value",
        "interpretation_hint",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_fact_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        return normalize_fact_status(value)


class WorkerNarrative(BaseModel):
    content_type: str = "narrative"
    note_number: str = ""
    note_title: str = ""
    subheading: str = ""
    summary: str = ""
    relevance: str = ""
    source: str = ""

    @field_validator(
        "content_type",
        "note_number",
        "note_title",
        "subheading",
        "summary",
        "relevance",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_narrative_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()


class WorkerOutput(BaseModel):
    table: str = ""
    statement: str = ""
    facts: list[WorkerFact] = Field(default_factory=list)
    narratives: list[WorkerNarrative] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)

    @field_validator("table", "statement", mode="before")
    @classmethod
    def normalize_table(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("facts", mode="before")
    @classmethod
    def normalize_facts(cls, value):
        return _coerce_worker_facts(value)

    @field_validator("narratives", mode="before")
    @classmethod
    def normalize_narratives(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @field_validator("missing", mode="before")
    @classmethod
    def normalize_missing(cls, value):
        return _coerce_text_list(value)


class WorkerAnswer(WorkerOutput):
    kind: Literal["answer"] = "answer"


WORKER_RESPONSE_ADAPTER = TypeAdapter(WorkerAnswer)
WORKER_RESPONSE_JSON_SCHEMA = WORKER_RESPONSE_ADAPTER.json_schema()
WORKER_RESPONSE_JSON_SCHEMA.setdefault("title", "WorkerAnswer")
WORKER_RESPONSE_JSON_SCHEMA.setdefault(
    "description",
    "Structured worker answer with extracted facts after native tool calls are complete.",
)


def _analysis_answer_from_facts(value: Any) -> str:
    facts = _coerce_worker_facts(value)
    if not facts:
        return ""

    lines: List[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        item_name = str(fact.get("item_name", "") or "").strip()
        time_hint = str(fact.get("time_hint", "") or "").strip()
        fact_value = str(fact.get("value", "") or "").strip()
        prefix = " - ".join(part for part in (item_name, time_hint) if part)
        if prefix and fact_value:
            lines.append(f"{prefix}: {fact_value}")
        elif fact_value:
            lines.append(fact_value)
        elif prefix:
            lines.append(prefix)

    return "\n".join(lines)


class AnalysisOutput(BaseModel):
    answer: str = ""
    requirements: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_analysis_fields(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)

        if "answer" not in data:
            for key in ("analysis", "summary", "result", "conclusion", "response", "text"):
                if data.get(key):
                    data["answer"] = data.get(key)
                    break

        if ("answer" not in data or not str(data.get("answer", "")).strip()) and data.get("facts"):
            data["answer"] = _analysis_answer_from_facts(data.get("facts"))

        if "requirements" not in data:
            for key in ("requirement", "missing_requirement", "missing_requirements", "missing", "needs"):
                if data.get(key):
                    data["requirements"] = data.get(key)
                    break

        return data

    @field_validator("answer", mode="before")
    @classmethod
    def normalize_answer(cls, value):
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return "\n".join(item for item in _flatten_text_items(value) if item)
        return str(value).strip()

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_requirements(cls, value):
        return _coerce_text_list(value)


ANALYSIS_RESPONSE_JSON_SCHEMA = AnalysisOutput.model_json_schema()
ANALYSIS_RESPONSE_JSON_SCHEMA.setdefault("title", "AnalysisOutput")
ANALYSIS_RESPONSE_JSON_SCHEMA.setdefault(
    "description",
    "Structured analysis response with a synthesized answer and optional missing-data requirements.",
)


def _extract_first_json_object(text: str) -> Optional[str]:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(cleaned)):
        ch = cleaned[idx]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:idx + 1]

    return None


def extract_answer_json(text: str) -> dict:
    answer_match = re.search(r"ANSWER:\s*(\{.*\})\s*$", text, flags=re.DOTALL)
    candidates = []

    if answer_match:
        candidates.append(answer_match.group(1))

    candidates.append(text)
    candidates.append(_extract_first_json_object(text))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Không tìm thấy JSON hợp lệ trong phản hồi worker.")


def parse_worker_response_payload(value: Any):
    if isinstance(value, WorkerAnswer):
        return value

    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)

    if not isinstance(value, dict):
        raise ValueError("Worker payload phải là dict hoặc text hợp lệ.")

    data = dict(value)
    if "kind" not in data:
        data["kind"] = "answer"

    return WORKER_RESPONSE_ADAPTER.validate_python(data)


def parse_worker_response(text: str):
    stripped = str(text or "").strip()
    if not stripped:
        raise ValueError("Worker response rỗng.")

    data = extract_answer_json(stripped)
    if "kind" not in data:
        data["kind"] = "answer"
    return WORKER_RESPONSE_ADAPTER.validate_python(data)


def parse_worker_output(text: str):
    parsed = parse_worker_response(text)
    return WorkerOutput.model_validate(parsed.model_dump(exclude={"kind"}))


def parse_analysis_response_payload(value: Any):
    if isinstance(value, AnalysisOutput):
        return value

    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)

    if not isinstance(value, dict):
        raise ValueError("Analysis payload phải là dict hoặc text hợp lệ.")

    return AnalysisOutput.model_validate(value)


def parse_analysis_response(text: str):
    stripped = str(text or "").strip()
    if not stripped:
        raise ValueError("Analysis response rỗng.")

    data = extract_answer_json(stripped)
    return AnalysisOutput.model_validate(data)


# ---------- Synth ----------
class SynthFollowupRequest(BaseModel):
    agent: Optional[AGENT_NAME] = None
    table: Optional[TABLE_NAME] = None
    requirements: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list, exclude=True)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def coerce_followup_fields(cls, value):
        if not isinstance(value, dict):
            return value
        return _coerce_followup_dict(value)

    @field_validator("agent", mode="before")
    @classmethod
    def normalize_followup_agent(cls, value):
        if value is None:
            return None
        text = str(value or "").strip()
        return text or None

    @field_validator("table", mode="before")
    @classmethod
    def normalize_followup_table(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = _normalize_table_value(value)
        if normalized in VALID_TABLE_NAMES:
            return normalized
        return None

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_followup_requirements(cls, value):
        return _coerce_text_list(value)

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_followup_keywords(cls, value):
        return _coerce_keyword_list(value)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_followup_reason(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def finalize_followup_defaults(self):
        if not self.requirements and self.keywords:
            self.requirements = _coerce_text_list(self.keywords)

        if self.agent and self.table is None:
            default_table = get_default_table(self.agent) or None
            if default_table in VALID_TABLE_NAMES:
                self.table = default_table

        return self

class SynthDecision(BaseModel):
    status: Literal["answer", "need_more"] = "answer"
    answer: str = ""
    followups: List[SynthFollowupRequest] = Field(default_factory=list)

    @field_validator("followups", mode="before")
    @classmethod
    def normalize_followups(cls, v):
        return _coerce_followups_payload(v)
