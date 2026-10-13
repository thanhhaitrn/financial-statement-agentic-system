"""The checked news analysis as structured data, for Synth and, when the
question needs it, for one analysis agent.

``structured_news`` is the one form both receive: per event what happened,
when, which figures it relates to, the checked explanation, the quotes with
their sources, the confirmation level and its basis, what is uncertain, and
(secondary) the sentiment; plus how far collection got and its limits. No
URLs and no article text beyond verified quotes.

By default the analysis goes to Synth alone. An analysis agent also gets it
when all three hold:

- the router listed that agent as needing the web evidence (``needby``);
- the agent is dispatched for this question;
- the question links the news to the figures ("tin ... ảnh hưởng thế nào tới
  lợi nhuận", "vì sao ... có liên quan tới tin ..."), not merely asks for both.

The agent receives it as web facts, so the existing guards keep it out of
calculations and report grounding.
"""

from __future__ import annotations

import re

from agentfinx.core.schemas.web_analysis import WebAnalysis
from agentfinx.news.analysis.render import CONFIRMATIONS
from agentfinx.news.analysis.support import vn_day
from agentfinx.news.analysis.window import fold

USABLE_STATUSES = {"ok", "partial"}
_LINK_CUES = re.compile(
    r"\b(?:anh huong|tac dong|lien quan|lien he|giai thich|nguyen nhan|vi sao|tai sao|ly do|"
    r"do dau|phan anh|gan voi|dong gop|hau qua|he qua)\b"
)


def links_news_to_analysis(question: str) -> bool:
    return bool(_LINK_CUES.search(fold(question)))


def news_agents(analysis: dict, *, question: str, needby: list[str], dispatched: list[str]) -> list[str]:
    if (analysis or {}).get("status") not in USABLE_STATUSES or not (analysis or {}).get("events"):
        return []
    if not links_news_to_analysis(question):
        return []
    return [agent for agent in dict.fromkeys(needby) if agent in set(dispatched)]


def structured_news(analysis: dict) -> dict:
    """The checked analysis as data. Empty when there is none."""

    if not isinstance(analysis, dict) or not analysis.get("status"):
        return {}
    try:
        result = WebAnalysis.model_validate(analysis)
    except ValueError:
        return {}
    sources = {item["article_id"]: item for item in result.sources}

    def source(article_id: str) -> dict:
        item = sources.get(article_id, {})
        # The source's own summary (sapo) gives the who and what in the
        # source's words, so a reader of this data need not fill gaps itself.
        return {"title": item.get("title", ""), "summary": item.get("summary", ""),
                "publisher": item.get("publisher", "") or "Vietstock",
                "published": vn_day(item.get("published_at", "")) or "",
                "text": "full" if item.get("content_status") == "full" else "summary_only"}

    context = result.context
    return {
        "status": result.status,
        "company": context.company,
        "ticker": context.ticker,
        "period": {"from": vn_day(context.date_from) or "", "to": vn_day(context.date_to) or "",
                   "window": context.window_source},
        "events": [
            {
                "event": event.title,
                "event_title_from": event.title_source,
                # Date the article states for the event (may be a period end).
                "date_in_article": event.event_date,
                "reported_on": event.reported_at,
                "related_metrics": event.related_metrics,
                "explanation": event.explanation,
                "evidence": [{"quote": quote.text, "source": source(quote.article_id)} for quote in event.quotes],
                "sources": [source(item) for item in event.article_ids],
                "confirmation": event.confirmation,
                "confirmation_text": CONFIRMATIONS.get(event.confirmation, event.confirmation),
                "confirmation_basis": event.confirmation_basis,
                "uncertainties": event.uncertainties,
                "sentiment_secondary": {"label": event.label, "score": event.score},
            }
            for event in result.events
        ],
        "collection": result.collection.model_dump(),
        "limitations": list(result.limitations),
        "aggregate_sentiment_secondary": {"score": result.aggregate.score,
                                          "scored_events": result.aggregate.scored_events,
                                          "rubric_version": result.aggregate.rubric_version},
    }


def news_facts_for_agent(analysis: dict, agent: str) -> list[dict]:
    """The structured events as web facts visible to ``agent`` only."""

    data = structured_news(analysis)
    facts = []
    for index, event in enumerate(data.get("events", []), start=1):
        first = (event["sources"] or [{}])[0]
        when = event["date_in_article"] or event["reported_on"]
        quotes = " / ".join(f"“{item['quote']}”" for item in event["evidence"])
        value = (f"Sự kiện: {event['event']}" + (f" ({when})" if when else "") + ". "
                 + (f"Liên quan: {', '.join(event['related_metrics'])}. " if event["related_metrics"] else "")
                 + (f"{event['explanation']} " if event["explanation"] else "")
                 + f"Mức xác nhận: {event['confirmation_text']}."
                 + (f" Trích dẫn đã kiểm tra: {quotes}" if quotes else ""))
        facts.append({
            "source_kind": "web",
            "content_type": "web_fact",
            "fact_id": f"news-{(analysis or {}).get('memo_key', '')}-e{index}",
            "item_name": event["event"],
            "value": value,
            "evidence_text": value,
            "ticker": data.get("ticker", ""),
            "source": first.get("publisher") or "Vietstock",
            "publisher": first.get("publisher") or "Vietstock",
            "published_at": first.get("published", ""),
            "time_hint": when,
            "status": "found",
            "retrieval_status": "found",
            "news_event": event,
            "needby": [agent],
        })
    return facts
