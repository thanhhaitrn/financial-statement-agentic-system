import pytest

from agent_web.interfaces import WebAgentServices
from agent_web.models import ResultStatus, WebAnalysisRequest
from agent_web.pipeline import run_agent_web
from agent_web.clients.mock_model_client import MockModelClient
from agent_web.config import AgentWebConfig


@pytest.mark.asyncio
async def test_missing_target_returns_needs_clarification_without_calling_model():
    request = WebAnalysisRequest(question="Tin tức gần đây có gì đáng chú ý?")
    model_client = MockModelClient()

    # search_provider/fetcher cố tình None-ish: nếu pipeline lỡ gọi tới sẽ
    # raise AttributeError ngay, chứng minh pipeline KHÔNG được đoán doanh
    # nghiệp rồi vẫn chạy tiếp.
    services = WebAgentServices(
        search_provider=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        model_client=model_client,
        config=AgentWebConfig(),
    )

    result = await run_agent_web(request, services=services)

    assert result.status == ResultStatus.NEEDS_CLARIFICATION
    assert result.target.resolved is False
    assert model_client.calls == []
