"""The one entry point for analysing company news.

``news.collect`` fetches and caches articles; ``news.analysis`` reads only
the articles it is handed and asks the model about them. Both callers that
need an analysis go through ``analyze`` here, so they use the same limits of
the active runtime policy: the graph's news branch
(``workflow.news_node``) and the chat worker finishing news that was
not ready when a run answered (``api.news_updates``).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from agentfinx.core.config.runtime_policy import active_policy
from agentfinx.core.schemas.web_analysis import WebAnalysis, WebContext
from agentfinx.news.analysis.pipeline import analyze_company_news, articles_from_evidence


def analyze(
    context: WebContext,
    evidence: Iterable[dict],
    batch: dict,
    *,
    budget_seconds: float,
    invoke: Optional[Callable[[str, str, float], str]] = None,
) -> WebAnalysis:
    """Analyse collected evidence rows; ``batch`` describes how they were collected."""

    policy = active_policy().web
    if invoke is None:
        from agentfinx.news.analysis.model import ollama_json_invoker

        invoke = ollama_json_invoker()
    return analyze_company_news(
        context,
        articles_from_evidence(list(evidence)),
        dict(batch or {}),
        invoke=invoke,
        budget_seconds=budget_seconds,
        max_calls=policy.analysis_max_model_calls,
        max_articles=policy.analysis_max_articles,
        prompt_article_chars=policy.analysis_prompt_article_chars,
    )
