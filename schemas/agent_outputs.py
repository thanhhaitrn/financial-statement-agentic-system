from pydantic import BaseModel, Field
from typing import List, Literal, Dict, Any

TABLE_NAME = Literal[
    "BẢNG CÂN ĐỐI KẾ TOÁN",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
]

#METRIC_TYPE = Literal["value", "difference", "ratio", "growth"]

# ---------- Planner (tables only) ---------

class PlannerTablesOnly(BaseModel):
    tables: List[TABLE_NAME] = Field(default_factory=list)
    company: str = ""
    time_hint: str = ""
    need_web: bool = False


# ---------- Keyworder / Detailed plan (optional next step) ----------
class Target(BaseModel):
    table: TABLE_NAME
    keywords: List[str] = Field(default_factory=list)

"""class Metric(BaseModel):
    name: str
    type: METRIC_TYPE
    components: List[str] = Field(default_factory=list)"""

class KeywordPlan(BaseModel):
    targets: List[Target] = Field(default_factory=list)


# ---------- Tools ----------
class ToolCall(BaseModel):
    action: Literal["get_related_info", "web_search", "calculate_dti"]
    arguments: Dict[str, Any] = Field(default_factory=dict)


# ---------- Synth ----------
class FollowupRequest(BaseModel):
    table: Literal[
        "BẢNG CÂN ĐỐI KẾ TOÁN",
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    ]
    query: str

"""class SynthDecision(BaseModel):
    status: Literal["answer", "need_more"]
    answer: str = ""
    followups: List[FollowupRequest] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)"""