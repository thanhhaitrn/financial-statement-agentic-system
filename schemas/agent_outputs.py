from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Dict, Any, Optional

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

# ---------- Planner (tables only) ---------


class PlannerTablesOnly(BaseModel):
    tables: List[TABLE_NAME] = Field(default_factory=list)
    company: str = ""
    time_hint: str = ""
    need_web: bool = False

    @field_validator("tables", mode="before")
    @classmethod
    def normalize_tables(cls, v):
        if v is None:
            return []
        # LLM có thể trả list[str] hoặc list[dict]
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