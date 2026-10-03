"""
CLI demo cho agent_web.

Vì adapter Vietstock thật (agent_web/providers/vietstock_provider.py) còn
là stub (cần code crawler thật để hoàn thiện), CLI này chạy demo bằng một
file fixture JSON (danh sách bài báo mẫu) qua MockSearchProvider/MockFetcher
— đúng tinh thần "không cần API key cho phần search", chỉ ModelClient thật
cần ANTHROPIC_API_KEY.

Dùng:
    python -m agent_web.cli \
        --question "Các tin trong tháng 8 ảnh hưởng tích cực hay tiêu cực đến FPT?" \
        --ticker FPT --company-name "Công ty Cổ phần FPT" \
        --date-from 2026-08-01 --date-to 2026-08-31 \
        --fixtures tests/fixtures/sample_articles.py \
        --format markdown
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import date, datetime

from .clients import AnthropicModelClient
from .config import AgentWebConfig
from .interfaces import WebAgentServices
from .models import WebAnalysisRequest
from .providers.mock_provider import build_mock_services
from .render import render_markdown
from .pipeline import run_agent_web


def _load_fixtures(path: str):
    spec = importlib.util.spec_from_file_location("fixtures_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.FIXTURES  # tests/fixtures/sample_articles.py phải export FIXTURES


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="agent_web demo CLI")
    p.add_argument("--question", required=True)
    p.add_argument("--ticker")
    p.add_argument("--company-name")
    p.add_argument("--topics", nargs="*", default=[])
    p.add_argument("--date-from", type=date.fromisoformat)
    p.add_argument("--date-to", type=date.fromisoformat)
    p.add_argument("--fixtures", required=True, help="Path tới fixture .py chứa FIXTURES")
    p.add_argument("--format", choices=["json", "markdown"], default="markdown")
    return p.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> None:
    fixtures = _load_fixtures(args.fixtures)
    search_provider, fetcher = build_mock_services(fixtures)

    model_client = AnthropicModelClient()  # đọc ANTHROPIC_API_KEY từ env

    services = WebAgentServices(
        search_provider=search_provider,
        fetcher=fetcher,
        model_client=model_client,
        config=AgentWebConfig.from_env(),
    )

    request = WebAnalysisRequest(
        question=args.question,
        ticker=args.ticker,
        company_name=args.company_name,
        topics=args.topics,
        date_from=args.date_from,
        date_to=args.date_to,
    )

    result = await run_agent_web(request, services=services)

    if args.format == "json":
        print(result.model_dump_json(indent=2, exclude_none=False))
    else:
        print(render_markdown(result))


def main() -> None:
    args = _parse_args(sys.argv[1:])
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
