"""Company-news analysis over articles the evidence stage already selected.

Two batched model calls, each retried once inside the same budget:

1. select the articles about the company and the question, group them into
   events (what happened, and when if the article says);
2. back each event with verbatim quotes, a short explanation and what is
   still uncertain (plus a secondary sentiment label).

A reply missing a required field, or with a label outside the list, is
rejected and retried. Everything else is code: overlapping groups merge
before step 2 (an article counts once); quotes must be verbatim in an article
of their own event; the title and explanation must be supported by those
quotes or are replaced / dropped; dates, related metrics and the
confirmation level come from fixed rules. "No news" is only concluded when the
news list was read for the whole window. A model failure is an error, never
"no news".
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentfinx.core import experiment
from agentfinx.core.schemas.web_analysis import (SENTIMENT_SCORES, Collection, NewsArticle,
                                  NewsEvent, Quote, SentimentLabel,
                                  WebAnalysis, WebContext)
from agentfinx.news.analysis import prompts, support
from agentfinx.news.analysis.model import ModelInvoker
from agentfinx.news.analysis.quotes import clean_quote, quote_matches
from agentfinx.news.analysis.scoring import aggregate

# A call is not started with less time than this left in the budget.
MIN_CALL_SECONDS = 3.0
TONE_NOTE = ("Sắc thái chỉ là thông tin phụ về giọng điệu của tin; không phải dự báo giá cổ phiếu "
             "và không chứng minh tin nào là nguyên nhân của số liệu trong báo cáo.")


# Model replies. No defaults: a missing field rejects the whole reply.
class _GroupOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=3)
    article_ids: list[str] = Field(min_length=1)
    event_date: str


class _SelectOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    relevant_article_ids: list[str]
    events: list[_GroupOut]


class _QuoteOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    article_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class _EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str = Field(min_length=1)
    quotes: list[_QuoteOut]
    explanation: str
    uncertainties: list[str]
    label: SentimentLabel


class _EvidenceOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    events: list[_EvidenceItem]


class _BudgetExhausted(RuntimeError):
    pass


def _json_object(text: str) -> dict:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model reply holds no JSON object")
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model reply is not a JSON object")
    return value


class _Session:
    """Budget and call accounting for one analysis."""

    def __init__(self, invoke: ModelInvoker, *, budget_seconds: float, max_calls: int,
                 clock: Callable[[], float]):
        self.invoke = invoke
        self.max_calls = max_calls
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + budget_seconds
        self.calls = 0
        self.errors: list[str] = []

    def call(self, step: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        """One attempt plus one retry, both inside the budget and call cap."""

        last: Exception | None = None
        for _attempt in range(2):
            remaining = self.deadline - self.clock()
            if self.calls >= self.max_calls or remaining < MIN_CALL_SECONDS:
                self.errors.append(f"{step}:budget_exhausted")
                raise _BudgetExhausted(step)
            self.calls += 1
            try:
                with experiment.step(f"news.{step}"):
                    reply = self.invoke(system, user, remaining)
                return schema.model_validate(_json_object(reply))
            except ValidationError as exc:
                self.errors.append(f"{step}:missing_or_invalid_fields")
                last = exc
            except ValueError as exc:
                self.errors.append(f"{step}:invalid_output")
                last = exc
            except Exception as exc:  # transport, timeout, provider errors
                self.errors.append(f"{step}:{type(exc).__name__}")
                last = exc
        raise RuntimeError(f"{step} failed") from last

    def elapsed_ms(self) -> int:
        return int((self.clock() - self.started) * 1000)


def _merge_groups(groups: list[_GroupOut], relevant: list[str], known: dict[str, NewsArticle]) -> list[dict]:
    """Union groups that share an article, then add relevant articles no
    group took as their own events: no article is ever counted twice."""

    clean = []
    for group in groups:
        ids = [item for item in dict.fromkeys(group.article_ids) if item in known and item in relevant]
        if ids:
            clean.append({"title": " ".join(group.title.split()), "ids": ids,
                          "dates": {group.event_date.strip()} - {""}})
    merged: list[dict] = []
    for group in clean:
        for item in [entry for entry in merged if set(entry["ids"]) & set(group["ids"])]:
            merged.remove(item)
            group = {"title": item["title"] or group["title"],
                     "ids": list(dict.fromkeys(item["ids"] + group["ids"])),
                     "dates": item["dates"] | group["dates"]}
        merged.append(group)
    used = {item for group in merged for item in group["ids"]}
    for article_id in relevant:
        if article_id not in used and article_id in known:
            merged.append({"title": known[article_id].title, "ids": [article_id], "dates": set()})
    return [
        {"event_id": f"e{index}", "title": group["title"] or known[group["ids"][0]].title,
         "article_ids": group["ids"],
         # Two different dates for one event: neither is stated reliably.
         "event_date": next(iter(group["dates"])) if len(group["dates"]) == 1 else ""}
        for index, group in enumerate(merged, start=1)
    ]


def _checked_event(group: dict, item: _EvidenceItem | None, known: dict[str, NewsArticle],
                   context: WebContext, diagnostics: dict) -> NewsEvent:
    articles = [known[article_id] for article_id in group["article_ids"]]
    titles = [article.title for article in articles]
    quotes: list[Quote] = []
    for quote in (item.quotes if item else []):
        # Verbatim, and from an article of THIS event.
        if quote.article_id in group["article_ids"] and quote_matches(quote.text, known[quote.article_id].content):
            quotes.append(Quote(article_id=quote.article_id, text=clean_quote(quote.text)))
        else:
            diagnostics["quotes_rejected"] += 1
    evidence = [quote.text for quote in quotes] + titles

    title, title_source = group["title"], "model_checked"
    if not support.supported(title, evidence):
        title, title_source = titles[0], "source_title"
        diagnostics["titles_replaced"] += 1
    explanation, dropped = support.supported_sentences(item.explanation if item else "", evidence)
    diagnostics["explanation_sentences_dropped"] += dropped
    uncertainties = []
    for note in (item.uncertainties if item else []):
        note = " ".join(str(note or "").split())
        # A stated uncertainty may name only what the sources name.
        if note and support.supported(note, evidence + [note]) and set(support._names(note)) <= set(
                support._names(" ".join(evidence)) | support._names(context.company)):
            uncertainties.append(note)
    level, basis = support.confirmation(context.ticker, titles, [quote.text for quote in quotes])
    if level in {"unattributed", "unconfirmed", "forecast"}:
        uncertainties.append(basis)
    if any(article.content_status != "full" for article in articles):
        uncertainties.append("Một phần nguồn chỉ đọc được phần tóm tắt, không đọc được toàn văn.")

    label = item.label if item else "insufficient"
    if SENTIMENT_SCORES.get(label) is not None and not quotes:
        diagnostics["events_downgraded"] += 1
        label = "insufficient"
    return NewsEvent(
        event_id=group["event_id"],
        title=title,
        title_source=title_source,
        article_ids=group["article_ids"],
        event_date=support.stated_date(group["event_date"], [article.content for article in articles]),
        reported_at=support.reported_at(article.published_at for article in articles),
        related_metrics=support.related_metrics([quote.text for quote in quotes], context.question),
        explanation=explanation,
        quotes=quotes,
        confirmation=level,
        confirmation_basis=basis,
        uncertainties=list(dict.fromkeys(uncertainties)),
        label=label,
        score=SENTIMENT_SCORES.get(label),
    )


def _sources(events: list[NewsEvent], known: dict[str, NewsArticle]) -> list[dict]:
    cited = list(dict.fromkeys(item for event in events for item in event.article_ids))
    return [
        {"article_id": item, "title": known[item].title, "url": known[item].url,
         "publisher": known[item].publisher, "published_at": known[item].published_at,
         "summary": known[item].summary[:400], "content_status": known[item].content_status}
        for item in cited if item in known
    ]


def _collection(batch: dict, articles: list[NewsArticle]) -> Collection:
    coverage = batch.get("coverage") or {}
    stats = batch.get("stats") or {}
    return Collection(
        list_read_for_window=bool(coverage.get("complete")) and not batch.get("pending"),
        listed_in_window=int(stats.get("listed_in_window", len(articles)) or 0),
        full_text_in_window=int(stats.get("full_text_in_window", 0) or 0),
        selected=len(articles),
        analyzed_full_text=sum(1 for article in articles if article.content_status == "full"),
        analyzed_summary_only=sum(1 for article in articles if article.content_status != "full"),
        ranked_by_topic=bool(stats.get("ranked_by_topic")),
        covered_from=str(coverage.get("covered_from", "") or coverage.get("listed_from", "") or ""),
        pending=bool(batch.get("pending")),
    )


def _coverage_limitations(batch: dict) -> list[str]:
    notes = []
    coverage = batch.get("coverage") or {}
    if coverage and not coverage.get("complete", True):
        covered_from = support.vn_day(str(coverage.get("covered_from", "") or ""))
        if covered_from:
            notes.append(f"Danh sách tin mới được đọc từ {covered_from}; tin trước đó trong khoảng hỏi chưa được thu thập.")
        else:
            notes.append("Chưa đọc hết danh sách tin cho cả khoảng thời gian hỏi.")
    if batch.get("pending"):
        notes.append("Hệ thống vẫn đang đọc danh sách tin; hỏi lại sau ít phút có thể có thêm bài.")
    return notes


def analyze_company_news(
    context: WebContext,
    articles: list[NewsArticle],
    batch: dict,
    *,
    invoke: ModelInvoker,
    budget_seconds: float = 60,
    max_calls: int = 4,
    max_articles: int = 10,
    prompt_article_chars: int = 2_500,
    clock: Callable[[], float] = time.monotonic,
) -> WebAnalysis:
    base = {"context": context, "coverage": dict(batch.get("coverage") or {})}
    if context.status != "ready":
        return WebAnalysis(status=context.status, limitations=[context.message], **base)

    articles = articles[:max_articles]
    known = {article.article_id: article for article in articles}
    collection = _collection(batch, articles)
    diagnostics = {"articles_in": len(articles), "model_calls": 0, "duration_ms": 0, "quotes_rejected": 0,
                   "titles_replaced": 0, "explanation_sentences_dropped": 0, "events_downgraded": 0,
                   "errors": [], "budget_seconds": budget_seconds, "provider_status": batch.get("status", "")}
    limitations = _coverage_limitations(batch)
    period = f"{support.vn_day(context.date_from)} đến {support.vn_day(context.date_to)}"
    base["collection"] = collection

    if not articles:
        if batch.get("status") == "error":
            return WebAnalysis(status="error", diagnostics=diagnostics, **base, limitations=[
                "Không lấy được tin từ Vietstock lúc này (lỗi nguồn), nên chưa có phần tin tức."])
        if batch.get("pending"):
            return WebAnalysis(status="pending", diagnostics=diagnostics, **base, limitations=[
                f"Hệ thống đang đọc danh sách tin của {context.ticker} trên Vietstock; hãy hỏi lại sau ít phút."])
        if not collection.list_read_for_window:
            # The list was not read back that far: this is not "no news".
            listed_from = support.vn_day(str((batch.get("coverage") or {}).get("listed_from", "") or ""))
            reach = f"; hiện mới đọc được danh sách tin từ {listed_from} trở về sau" if listed_from else ""
            return WebAnalysis(status="partial", diagnostics=diagnostics, **base, limitations=[
                f"Chưa đọc được tin của {context.ticker} trong khoảng {period}{reach}. "
                "Nguồn chỉ cho xem lại một phần lịch sử tin."])
        return WebAnalysis(status="insufficient_evidence", diagnostics=diagnostics, **base, limitations=[
            f"Đã đọc hết danh sách tin của {context.ticker} trên Vietstock từ {period}: không có bài nào."])

    session = _Session(invoke, budget_seconds=budget_seconds, max_calls=max_calls, clock=clock)

    def finish(status: str, events: list[NewsEvent], notes: list[str]) -> WebAnalysis:
        diagnostics.update(model_calls=session.calls, duration_ms=session.elapsed_ms(),
                           errors=list(session.errors), events=len(events))
        return WebAnalysis(status=status, events=events, aggregate=aggregate(events),
                           sources=_sources(events, known), limitations=notes, diagnostics=diagnostics,
                           **base)

    # 1) Which articles are about the company and the question, as events.
    try:
        selected = session.call(
            "select", prompts.SELECT_AND_GROUP_SYSTEM,
            prompts.select_and_group_user(context, articles, prompt_article_chars), _SelectOut)
    except (RuntimeError, _BudgetExhausted):
        return finish("error", [], [
            f"Đã chọn {len(articles)} bài về {context.ticker} nhưng không phân tích được do lỗi mô hình "
            "hoặc hết thời gian; đây không có nghĩa là không có tin.", *limitations])
    relevant = [item for item in dict.fromkeys(selected.relevant_article_ids) if item in known]
    collection.relevant = len(relevant)
    diagnostics["articles_relevant"] = len(relevant)
    if not relevant:
        status = "insufficient_evidence" if collection.list_read_for_window else "partial"
        return finish(status, [], [
            f"Trong {len(articles)} bài đã phân tích (từ {period}) không bài nào nói thực chất về "
            f"{context.company or context.ticker} liên quan tới câu hỏi.", *limitations])
    grouped = _merge_groups(selected.events, relevant, known)

    # 2) Evidence for each event.
    evidence: dict[str, _EvidenceItem] = {}
    evidence_failed = False
    try:
        result = session.call("evidence", prompts.SCORE_SYSTEM,
                               prompts.score_user(context, grouped, known, prompt_article_chars), _EvidenceOut)
        evidence = {item.event_id: item for item in result.events}
    except (RuntimeError, _BudgetExhausted):
        evidence_failed = True

    events = [_checked_event(group, evidence.get(group["event_id"]), known, context, diagnostics)
              for group in grouped]
    notes = list(limitations)
    if collection.analyzed_summary_only:
        notes.append(f"{collection.analyzed_summary_only} bài chỉ đọc được phần tóm tắt, không đọc được toàn văn.")
    if diagnostics["explanation_sentences_dropped"]:
        notes.append(f"{diagnostics['explanation_sentences_dropped']} câu giải thích của mô hình không được trích "
                     "dẫn chứng minh nên đã bỏ.")
    if diagnostics["events_downgraded"]:
        notes.append(f"{diagnostics['events_downgraded']} sự kiện không có trích dẫn khớp nguyên văn với bài gốc "
                     "nên không được nhận định.")
    if evidence_failed:
        notes.append("Chưa lấy được trích dẫn cho các sự kiện do lỗi mô hình hoặc hết thời gian.")
    notes.append(TONE_NOTE)
    partial = evidence_failed or bool(limitations)
    return finish("partial" if partial else "ok", events, notes)


def articles_from_evidence(evidence: list[dict]) -> list[NewsArticle]:
    """Number the selected evidence a1, a2, … in the provider's order (most
    about the question first), dropping duplicate URLs and content."""

    output: list[NewsArticle] = []
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    for item in evidence:
        url = str(item.get("source_url", "") or "")
        content_hash = str(item.get("content_hash", "") or "")
        if not url or url in seen_urls or (content_hash and content_hash in seen_hashes):
            continue
        seen_urls.add(url)
        if content_hash:
            seen_hashes.add(content_hash)
        status = str(item.get("content_status") or "full")
        output.append(NewsArticle(
            article_id=f"a{len(output) + 1}",
            evidence_id=str(item.get("evidence_id", "") or ""),
            title=str(item.get("title", "") or ""),
            url=url,
            publisher=str(item.get("publisher", "") or ""),
            published_at=str(item.get("published_at", "") or ""),
            retrieved_at=str(item.get("retrieved_at", "") or ""),
            content=str(item.get("content", "") or ""),
            summary=str(item.get("summary", "") or ""),
            content_status=status if status in {"full", "summary_only", "listing_only"} else "full",
        ))
    return output
