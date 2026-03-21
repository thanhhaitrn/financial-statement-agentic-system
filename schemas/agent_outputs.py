from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from typing import Annotated, List, Literal, Dict, Any, Optional, Union
import re, json

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

PLANNER_QUESTION_TYPE = Literal[
    "lookup",
    "calculation",
    "comparison",
    "evaluation",
    "risk_assessment",
]


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
                key = item.strip().lower()
                out.append(TABLE_CANON.get(key, item))
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
    question_type: PLANNER_QUESTION_TYPE = "lookup"
    tables: List[TABLE_NAME] = Field(default_factory=list, exclude=True)
    analysis_axes: List[PlannerAnalysisAxis] = Field(default_factory=list)
    required_components: List[str] = Field(default_factory=list, exclude=True)
    company: Optional[str] = ""
    time_hint: Optional[str] = ""
    need_web: bool = False

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
                key = item.strip().lower()
                out.append(TABLE_CANON.get(key, item))
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
        key = v.strip().lower()
        return TABLE_CANON.get(key, v)

class KeywordPlan(BaseModel):
    targets: List[Target] = Field(default_factory=list)


WORKER_TOOL_ACTION = Literal["get_related_info", "web_search"]


# ---------- Tools ----------
class ToolCall(BaseModel):
    action: WORKER_TOOL_ACTION
    arguments: Dict[str, Any] = Field(default_factory=dict)

class WorkerFact(BaseModel):
    item_name: str
    time_hint: str = ""
    value: str
    source: str

class WorkerOutput(BaseModel):
    table: str
    facts: list[WorkerFact] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    notes: str = ""


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

WORKER_RESPONSE_ADAPTER = TypeAdapter(WorkerResponse)
WORKER_RESPONSE_JSON_SCHEMA = WORKER_RESPONSE_ADAPTER.json_schema()


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
    keywords: List[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("table", mode="before")
    @classmethod
    def normalize_followup_table(cls, v):
        if v is None or not isinstance(v, str):
            return v
        key = v.strip().lower()
        return TABLE_CANON.get(key, v)

class SynthDecision(BaseModel):
    status: Literal["answer", "need_more"] = "answer"
    answer: str = ""
    followups: List[FollowupRequest] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
