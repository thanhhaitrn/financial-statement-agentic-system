"""
Cấu hình agent_web. Mọi tham số vận hành phải cấu hình được (mục 8) — không
hardcode. API key luôn lấy từ env, không bao giờ ghi log giá trị key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


@dataclass
class AgentWebConfig:
    # Cửa sổ thời gian mặc định khi request thiếu date_from/date_to (ngày)
    default_time_window_days: int = 30

    # Giới hạn tài nguyên (mục 4, 8)
    max_search_queries: int = 6
    max_articles: int = 25
    max_followup_search_rounds: int = 1

    # Timeout / retry
    request_timeout_s: float = 20.0
    total_deadline_s: float = 120.0
    max_concurrency: int = 5
    max_retries: int = 2

    # Model
    model: str = "claude-sonnet-4-6"

    @classmethod
    def from_env(cls) -> "AgentWebConfig":
        return cls(
            default_time_window_days=_env_int("AGENT_WEB_DEFAULT_WINDOW_DAYS", 30),
            max_search_queries=_env_int("AGENT_WEB_MAX_SEARCH_QUERIES", 6),
            max_articles=_env_int("AGENT_WEB_MAX_ARTICLES", 25),
            max_followup_search_rounds=_env_int("AGENT_WEB_MAX_FOLLOWUP_ROUNDS", 1),
            request_timeout_s=_env_float("AGENT_WEB_REQUEST_TIMEOUT_S", 20.0),
            total_deadline_s=_env_float("AGENT_WEB_TOTAL_DEADLINE_S", 120.0),
            max_concurrency=_env_int("AGENT_WEB_MAX_CONCURRENCY", 5),
            max_retries=_env_int("AGENT_WEB_MAX_RETRIES", 2),
            model=os.environ.get("AGENT_WEB_MODEL", "claude-sonnet-4-6"),
        )
