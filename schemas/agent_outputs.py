from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Literal, Dict, Any, Optional
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
    components: List[str] = Field(default_factory=list)
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
    tables: List[TABLE_NAME] = Field(default_factory=list)
    analysis_axes: List[PlannerAnalysisAxis] = Field(default_factory=list)
    required_components: List[str] = Field(default_factory=list)
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

        for table in self.tables:
            if table not in seen_tables:
                tables.append(table)
                seen_tables.add(table)

        required_components = []
        seen_components = set()

        for component in self.required_components:
            if component not in seen_components:
                required_components.append(component)
                seen_components.add(component)

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
                if component not in seen_components:
                    required_components.append(component)
                    seen_components.add(component)

            axis.tables = axis_tables
            axis.components = axis_components
            normalized_axes.append(axis)

        self.tables = tables
        self.analysis_axes = normalized_axes
        self.required_components = required_components
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


# ---------- Tools ----------
class ToolCall(BaseModel):
    action: Literal["get_related_info", "web_search", "calculate_dti"]
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


def parse_worker_output(text: str):
    data = extract_answer_json(text)
    return WorkerOutput.model_validate(data)


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
