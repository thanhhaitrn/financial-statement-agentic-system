from __future__ import annotations

import pytest

from agent_web.config import AgentWebConfig
from agent_web.interfaces import WebAgentServices
from agent_web.providers.mock_provider import build_mock_services
from .fixtures.sample_articles import FIXTURES


@pytest.fixture
def fast_config() -> AgentWebConfig:
    return AgentWebConfig(
        default_time_window_days=30,
        max_search_queries=3,
        max_articles=10,
        max_followup_search_rounds=1,
        request_timeout_s=5.0,
        total_deadline_s=10.0,
        max_concurrency=5,
        max_retries=0,
        model="mock-model",
    )


@pytest.fixture
def mock_services_factory(fast_config):
    """Trả một factory(model_client) -> WebAgentServices, dùng fixture bài
    báo mẫu chung cho các test."""

    def _make(model_client) -> WebAgentServices:
        provider, fetcher = build_mock_services(FIXTURES)
        return WebAgentServices(
            search_provider=provider,
            fetcher=fetcher,
            model_client=model_client,
            config=fast_config,
        )

    return _make
