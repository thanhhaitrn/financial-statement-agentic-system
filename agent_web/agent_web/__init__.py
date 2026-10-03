"""
agent_web
=========

Agent độc lập: nhận câu hỏi về một doanh nghiệp -> tìm tin liên quan ->
đánh giá sentiment theo sự kiện -> trả kết quả có nguồn (evidence-backed).

Entry point chính: `agent_web.pipeline.run_agent_web`
"""

from .pipeline import run_agent_web
from .models import WebAnalysisRequest, WebAnalysisResult
from .interfaces import WebAgentServices

__all__ = [
    "run_agent_web",
    "WebAnalysisRequest",
    "WebAnalysisResult",
    "WebAgentServices",
]
