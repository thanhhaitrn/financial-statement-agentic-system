"""Company news that was not ready when a chat run answered.

A run answers with its report figures without waiting for news that is still
being collected (the source's list is still being read) or analysed (the
web branch ran out of its budget). The run then leaves a ``news_updates``
row; the chat worker, between questions, finishes the news from the shared
cache and adds it to the conversation as its own ``news_update`` message,
which the chat page shows by itself. After ``EXPIRES_AFTER`` it stops and says
so: only then is the user asked to ask again.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from agentfinx.api import db as webdb

logger = logging.getLogger("agentfinx.web.news")

RETRY_AFTER = timedelta(seconds=20)
EXPIRES_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 12


def needs_update(final_state: dict) -> Optional[dict]:
    """The news context to finish later, or None when the answer is complete."""

    analysis = (final_state or {}).get("web_analysis") or {}
    context = analysis.get("context") or {}
    if context.get("status") != "ready":
        return None
    incomplete = (
        analysis.get("status") == "pending"
        or bool((analysis.get("collection") or {}).get("pending"))
        or bool((analysis.get("diagnostics") or {}).get("unfinished"))
    )
    return dict(context) if incomplete else None


def schedule(conn, *, run: Any, context: dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return webdb.create_news_update(
        conn, run_id=run["id"], session_id=run["session_id"], owner_id=run["owner_id"], context=context,
        due_at=webdb.iso(now + RETRY_AFTER), expires_at=webdb.iso(now + EXPIRES_AFTER),
    )


def _default_provider():
    from agentfinx.core.config.runtime_policy import DEFAULT_POLICY
    from agentfinx.news.collect import build_default_web_provider

    return build_default_web_provider(DEFAULT_POLICY)


def _analyze(context: dict, provider, invoke) -> Any:
    from agentfinx.core.config.runtime_policy import DEFAULT_POLICY
    from agentfinx.core.schemas.web_analysis import WebContext
    from agentfinx.core.schemas.web_evidence import WebEvidenceRequest
    from agentfinx.news import service as news_service

    policy = DEFAULT_POLICY.web
    web_context = WebContext.model_validate(context)
    batch = provider.search(WebEvidenceRequest(
        query=web_context.question, intent="company_news", ticker=web_context.ticker,
        company=web_context.company, as_of=web_context.as_of, date_from=web_context.date_from,
        date_to=web_context.date_to, topic=web_context.question, limit=policy.analysis_max_articles,
    ))
    batch_meta = {"status": batch.status, "message": batch.message, "pending": bool(batch.pending),
                  "coverage": dict(batch.coverage or {}), "stats": dict(batch.stats or {})}
    if batch.pending:
        return None  # the list is still being read: try again later
    return news_service.analyze(
        web_context,
        [item.model_dump(mode="json") for item in batch.evidence],
        batch_meta,
        invoke=invoke,
        budget_seconds=policy.analysis_budget_seconds,
    )


def _message(analysis, question: str) -> tuple[str, dict]:
    from agentfinx.news.analysis.facts import structured_news
    from agentfinx.news.analysis.render import render_news_section

    content = f"**Cập nhật tin tức** cho câu hỏi “{question}”:\n\n{render_news_section(analysis.model_dump(mode='json'))}"
    return content, {"news": structured_news(analysis.model_dump(mode="json"))}


def run_news_update_once(
    database: webdb.WebDB,
    worker_id: str,
    *,
    provider_factory: Optional[Callable[[], Any]] = None,
    invoke: Optional[Callable[[str, str, float], str]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> bool:
    """Finish one due news update. Returns whether there was one."""

    clock = now or (lambda: datetime.now(timezone.utc))
    with database.transaction() as conn:
        row = webdb.claim_due_news_update(conn, worker_id, now=webdb.iso(clock()))
    if row is None:
        return False

    context = json.loads(row["context_json"])
    question = str(context.get("question", "") or "")
    expired = webdb.iso(clock()) >= row["expires_at"]
    analysis = None
    if not expired:
        try:
            provider = (provider_factory or _default_provider)()
            if provider is None:
                expired = True  # web evidence was switched off since
            else:
                analysis = _analyze(context, provider, invoke)  # invoke=None: the configured model
        except Exception:
            logger.exception("news update %s failed", row["id"])
            if row["attempts"] >= MAX_ATTEMPTS:
                expired = True

    still_waiting = analysis is None or analysis.status == "pending"
    with database.transaction() as conn:
        if webdb.get_session(conn, row["session_id"]) is None:
            return True  # the conversation was deleted meanwhile
        if still_waiting and not expired:
            webdb.reschedule_news_update(conn, row["id"], due_at=webdb.iso(clock() + RETRY_AFTER))
            return True
        if still_waiting:
            content = (f"Chưa cập nhật được phần tin tức cho câu hỏi “{question}”: nguồn tin chưa sẵn sàng sau "
                       f"{int(EXPIRES_AFTER.total_seconds() // 60)} phút. Bạn có thể hỏi lại sau.")
            payload, status = {"news": {}}, "expired"
        else:
            content, payload = _message(analysis, question)
            status = "done"
        message_id = webdb.add_message(
            conn, session_id=row["session_id"], role="assistant", content=content, run_id=row["run_id"],
            kind="news_update", payload=json.dumps(payload, ensure_ascii=False),
        )
        webdb.finish_news_update(conn, row["id"], status=status, message_id=message_id)
        webdb.touch_session(conn, row["session_id"])
    return True
