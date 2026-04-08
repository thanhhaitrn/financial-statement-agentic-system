from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from typing import Annotated, List, Literal, Dict, Any, Optional, Union
import re, json
from schemas.table_names import normalize_table_heading

TABLE_NAME = Literal[
    "BẢNG CÂN ĐỐI KẾ TOÁN",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
]

TABLE_CANON = {
    "bảng cân đối kế toán": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "báo cáo kết quả hoạt động kinh doanh": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "báo cáo lưu chuyển tiền tệ": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "bcdkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "bcđkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "kqhđkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "kqhdkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "lctt": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
}

VALID_TABLE_NAMES = set(TABLE_CANON.values())
AGENT_NAME_VALUES = {"agent_bs", "agent_is", "agent_cf", "agent_web"}
FOLLOWUP_AGENT_DEFAULT_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
}


def _normalize_table_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

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

AGENT_NAME = Literal["agent_bs", "agent_is", "agent_cf", "agent_web"]

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
    axis: str
    tables: List[TABLE_NAME] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list, exclude=True)
    objective: str = ""

    @field_validator("tables", mode="before")
    @classmethod
    def normalize_axis_tables(cls, v):
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

    @field_validator("axis", "objective", mode="before")
    @classmethod
    def normalize_axis_strings(cls, v):
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
    tables: List[TABLE_NAME] = Field(default_factory=list, exclude=True)
    analysis_axes: List[PlannerAnalysisAxis] = Field(default_factory=list)
    required_components: List[str] = Field(default_factory=list, exclude=True)
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

        for axis in self.analysis_axes:
            axis_tables = []
            axis_seen_tables = set()
            for table in axis.tables:
                if table not in axis_seen_tables:
                    axis_tables.append(table)
                    axis_seen_tables.add(table)
                if table not in seen_tables:
                    tables.append(table)
                    seen_tables.add(table)

            axis_components = []
            axis_seen_components = set()
            for component in axis.components:
                if component not in axis_seen_components:
                    axis_components.append(component)
                    axis_seen_components.add(component)

            axis.tables = axis_tables
            axis.components = axis_components
            normalized_axes.append(axis)

        if not normalized_axes and self.tables:
            normalized_axes.append(
                PlannerAnalysisAxis(
                    axis="core",
                    tables=tables or list(self.tables),
                    components=list(self.required_components),
                    objective="",
                )
            )
        elif self.tables:
            for table in self.tables:
                if table not in seen_tables:
                    tables.append(table)
                    seen_tables.add(table)

        self.tables = tables
        self.analysis_axes = normalized_axes
        self.required_components = _dedupe_keep_order(self.required_components)
        return self


# ---------- Keyworder / Detailed plan (optional next step) ----------
class Target(BaseModel):
    table: Literal[
        "BẢNG CÂN ĐỐI KẾ TOÁN",
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    ]
    keywords: List[str] = Field(default_factory=list)

    @field_validator("table", mode="before")
    @classmethod
    def normalize_table(cls, v):
        if not isinstance(v, str):
            return v
        return _normalize_table_value(v)

class KeywordPlan(BaseModel):
    targets: List[Target] = Field(default_factory=list)


WORKER_TOOL_ACTION = Literal["get_related_info", "web_search"]


# ---------- Tools ----------
class ToolCall(BaseModel):
    action: WORKER_TOOL_ACTION
    arguments: Dict[str, Any] = Field(default_factory=dict)

class WorkerFact(BaseModel):
    item_name: str = ""
    time_hint: str = ""
    value: str = ""
    source: str = ""

    @field_validator("item_name", "time_hint", "value", "source", mode="before")
    @classmethod
    def normalize_fact_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()

class WorkerOutput(BaseModel):
    table: str
    facts: list[WorkerFact] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def normalize_facts(cls, value):
        return _coerce_worker_facts(value)


class WorkerAction(BaseModel):
    kind: Literal["action"] = "action"
    action: WORKER_TOOL_ACTION
    arguments: Dict[str, Any] = Field(default_factory=dict)


class WorkerAnswer(WorkerOutput):
    kind: Literal["answer"] = "answer"


WorkerResponse = Annotated[
    Union[WorkerAction, WorkerAnswer],
    Field(discriminator="kind"),
]


class WorkerStructuredOutput(BaseModel):
    kind: Literal["action", "answer"]
    action: Optional[WORKER_TOOL_ACTION] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    table: str = ""
    facts: list[WorkerFact] = Field(default_factory=list)

    @field_validator("table", mode="before")
    @classmethod
    def normalize_output_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("facts", mode="before")
    @classmethod
    def normalize_facts(cls, value):
        return _coerce_worker_facts(value)

WORKER_RESPONSE_ADAPTER = TypeAdapter(WorkerResponse)
WORKER_RESPONSE_JSON_SCHEMA = WORKER_RESPONSE_ADAPTER.json_schema()
WORKER_RESPONSE_JSON_SCHEMA.setdefault("title", "WorkerResponse")
WORKER_RESPONSE_JSON_SCHEMA.setdefault(
    "description",
    "Structured worker response that is either a tool action request or a final extracted answer.",
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


def extract_action_json(text: str) -> dict:
    action_match = re.search(r"(?mi)^\s*ACTION:\s*([^\n]+?)\s*$", text)
    if not action_match:
        raise ValueError("Không tìm thấy ACTION hợp lệ trong phản hồi worker.")

    action = action_match.group(1).strip()
    arguments: Dict[str, Any] = {}

    args_match = re.search(r"(?mis)^\s*ARGUMENTS:\s*(\{.*\})\s*$", text)
    if args_match:
        args_text = args_match.group(1).strip()
        try:
            parsed = json.loads(args_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ARGUMENTS không phải JSON hợp lệ: {exc}") from exc

        if not isinstance(parsed, dict):
            raise ValueError("ARGUMENTS phải là JSON object.")
        arguments = parsed

    return {
        "kind": "action",
        "action": action,
        "arguments": arguments,
    }


def parse_worker_response_payload(value: Any):
    if isinstance(value, (WorkerAction, WorkerAnswer)):
        return value

    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)

    if not isinstance(value, dict):
        raise ValueError("Worker payload phải là dict hoặc text hợp lệ.")

    data = dict(value)
    if "kind" not in data:
        if data.get("action"):
            data["kind"] = "action"
        else:
            data["kind"] = "answer"

    return WORKER_RESPONSE_ADAPTER.validate_python(data)


def parse_worker_response(text: str):
    stripped = str(text or "").strip()
    if not stripped:
        raise ValueError("Worker response rỗng.")

    if re.search(r"(?mi)^\s*ACTION\s*:", stripped):
        return WORKER_RESPONSE_ADAPTER.validate_python(extract_action_json(stripped))

    data = extract_answer_json(stripped)
    if "kind" not in data:
        data["kind"] = "answer"
    return WORKER_RESPONSE_ADAPTER.validate_python(data)


def parse_worker_output(text: str):
    parsed = parse_worker_response(text)
    if isinstance(parsed, WorkerAction):
        raise ValueError("Worker đang trả action, chưa có answer để collect.")
    return WorkerOutput.model_validate(parsed.model_dump(exclude={"kind"}))


# ---------- Synth ----------
class FollowupRequest(BaseModel):
    agent: AGENT_NAME
    table: Optional[TABLE_NAME] = None
    requirements: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list, exclude=True)
    reason: str = ""

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_followup_requirements(cls, v):
        return _coerce_text_list(v)

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_followup_keywords(cls, v):
        return _coerce_keyword_list(v)

    @field_validator("table", mode="before")
    @classmethod
    def normalize_followup_table(cls, v):
        if v is None or not isinstance(v, str):
            return v
        normalized = _normalize_table_value(v)
        if normalized in VALID_TABLE_NAMES:
            return normalized
        return None

    @model_validator(mode="after")
    def infer_followup_table_from_agent(self):
        if self.table is None:
            self.table = FOLLOWUP_AGENT_DEFAULT_TABLE.get(self.agent)
        return self

class SynthDecision(BaseModel):
    status: Literal["answer", "need_more"] = "answer"
    answer: str = ""
    followups: List[FollowupRequest] = Field(default_factory=list)

    @field_validator("followups", mode="before")
    @classmethod
    def normalize_followups(cls, v):
        return _coerce_followups_payload(v)
