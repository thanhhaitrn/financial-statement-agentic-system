"""
run_agent_web — orchestration chính.

Luồng (mục 4):
  Hiểu câu hỏi -> tạo search queries -> tìm & tải bài -> lọc relevance/thời
  gian/trùng -> gom theo sự kiện -> đánh giá sentiment -> tổng hợp có nguồn.

Nguyên tắc phân chia trách nhiệm (mục 4):
- Model quyết định: query, relevance, nhóm sự kiện, sentiment.
- Provider: search/fetch.
- Code: validate schema, giới hạn tài nguyên, provenance, tổng hợp điểm.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from .aggregate import compute_aggregate
from .config import AgentWebConfig
from .dedup import content_hash as _content_hash
from .dedup import dedup_articles
from .interfaces import FetchedArticle, RawSearchHit, WebAgentServices
from .models import (
    ClaimType,
    ContentScope,
    Diagnostics,
    Evidence,
    EventSentiment,
    ResultStatus,
    SentimentLabel,
    SupportingQuote,
    TargetCompany,
    TimeRange,
    WebAnalysisRequest,
    WebAnalysisResult,
)
from .prompts import (
    EVENT_GROUPING_SYSTEM,
    QUERY_GENERATION_SYSTEM,
    RELEVANCE_FILTER_SYSTEM,
    SENTIMENT_SCORING_SYSTEM,
    SUMMARY_SYSTEM,
)


async def run_agent_web(
    request: WebAnalysisRequest,
    *,
    services: WebAgentServices,
) -> WebAnalysisResult:
    deadline = time.monotonic() + services.config.total_deadline_s
    diagnostics = Diagnostics(model=services.model_client.model_name)

    # 1) Hiểu câu hỏi / xác định doanh nghiệp mục tiêu -----------------
    if not request.ticker and not request.company_name:
        return WebAnalysisResult.needs_clarification(
            request.question,
            "Không xác định được doanh nghiệp mục tiêu (thiếu ticker và "
            "company_name). Không đoán doanh nghiệp.",
        )

    target = TargetCompany(
        ticker=request.ticker,
        company_name=request.company_name,
        resolved=True,
    )
    time_range = _resolve_time_range(request, services.config)
    as_of = request.as_of or datetime.now(timezone.utc)

    limitations: list[str] = []
    if time_range.is_default_window:
        limitations.append(
            f"Không có date_from/date_to trong request — dùng cửa sổ mặc "
            f"định {services.config.default_time_window_days} ngày."
        )

    try:
        # 2)-4) Tìm, tải, lọc — có thể lặp thêm nếu thiếu evidence -------
        evidence, search_diag = await _collect_evidence(
            request=request,
            target=target,
            time_range=time_range,
            as_of=as_of,
            services=services,
            deadline=deadline,
        )
        diagnostics.search_queries_used = search_diag["queries"]
        diagnostics.search_calls = search_diag["search_calls"]
        diagnostics.fetch_calls = search_diag["fetch_calls"]
        diagnostics.fetch_failures = search_diag["fetch_failures"]
        diagnostics.followup_rounds_used = search_diag["rounds_used"]
        diagnostics.articles_considered = search_diag["considered"]
        diagnostics.articles_after_dedup = len(evidence)
        diagnostics.duplicate_url_count = search_diag["duplicates_removed"]
        diagnostics.model_calls += search_diag["model_calls"]

        if not evidence:
            # "Không có bài phù hợp là một kết quả hợp lệ" (mục 5)
            return WebAnalysisResult(
                status=ResultStatus.INSUFFICIENT_EVIDENCE,
                target=target,
                question=request.question,
                time_range=time_range,
                as_of=as_of,
                summary_assessment=(
                    "Không tìm được tin tức phù hợp trong khoảng thời gian "
                    "và chủ đề yêu cầu."
                ),
                limitations=limitations
                + ["Không có evidence nào đạt relevance/thời gian yêu cầu."],
                diagnostics=diagnostics,
            )

        # 5) Gom theo sự kiện (lớp loại trùng thứ 2) ---------------------
        event_groups, group_calls = await _group_into_events(
            request, target, evidence, services
        )
        diagnostics.model_calls += group_calls
        diagnostics.duplicate_event_merge_count = max(
            0, len(evidence) - len(event_groups)
        )

        # 6) Đánh giá sentiment theo từng sự kiện -------------------------
        events, score_calls = await _score_events(
            request, target, event_groups, evidence, services
        )
        diagnostics.model_calls += score_calls

        aggregate = compute_aggregate(events)

        # 7) Tóm tắt định tính --------------------------------------------
        summary, summary_calls = await _summarize(
            request, target, events, aggregate, services
        )
        diagnostics.model_calls += summary_calls

        if diagnostics.fetch_failures > 0:
            limitations.append(
                f"{diagnostics.fetch_failures} bài không tải được nội dung "
                "(timeout/lỗi nguồn) và đã bị loại khỏi phân tích."
            )
        snippet_only = [e for e in evidence if e.content_scope == ContentScope.SNIPPET]
        if snippet_only:
            limitations.append(
                f"{len(snippet_only)} bài chỉ lấy được snippet (không phải "
                "toàn văn) — đánh giá dựa trên nội dung giới hạn."
            )

        status = ResultStatus.OK
        if diagnostics.fetch_failures > 0 or snippet_only:
            status = ResultStatus.PARTIAL
        if not events:
            status = ResultStatus.INSUFFICIENT_EVIDENCE

        diagnostics.total_latency_ms = (
            services.config.total_deadline_s - max(0.0, deadline - time.monotonic())
        ) * 1000

        return WebAnalysisResult(
            status=status,
            target=target,
            question=request.question,
            time_range=time_range,
            as_of=as_of,
            summary_assessment=summary,
            events=events,
            aggregate=aggregate,
            evidence=evidence,
            limitations=limitations,
            diagnostics=diagnostics,
        )

    except Exception as exc:  # noqa: BLE001 — bọc lỗi thành status=error
        diagnostics.total_latency_ms = (
            services.config.total_deadline_s - max(0.0, deadline - time.monotonic())
        ) * 1000
        return WebAnalysisResult(
            status=ResultStatus.ERROR,
            target=target,
            question=request.question,
            time_range=time_range,
            as_of=as_of,
            limitations=limitations + [f"Lỗi nội bộ: {type(exc).__name__}: {exc}"],
            diagnostics=diagnostics,
        )


# ---------------------------------------------------------------------------
# Bước phụ
# ---------------------------------------------------------------------------

def _resolve_time_range(request: WebAnalysisRequest, config: AgentWebConfig) -> TimeRange:
    if request.date_from and request.date_to:
        return TimeRange(date_from=request.date_from, date_to=request.date_to)
    today = date.today()
    return TimeRange(
        date_from=today - timedelta(days=config.default_time_window_days),
        date_to=today,
        is_default_window=True,
    )


async def _collect_evidence(
    *,
    request: WebAnalysisRequest,
    target: TargetCompany,
    time_range: TimeRange,
    as_of: datetime,
    services: WebAgentServices,
    deadline: float,
) -> tuple[list[Evidence], dict]:
    cfg = services.config
    sem = asyncio.Semaphore(cfg.max_concurrency)

    all_evidence: list[Evidence] = []
    all_fetched: list[FetchedArticle] = []
    used_queries: list[str] = []
    query_by_url: dict[str, str] = {}
    search_calls = fetch_calls = fetch_failures = model_calls = 0
    considered = 0
    rounds_used = 0

    for round_idx in range(cfg.max_followup_search_rounds + 1):
        if time.monotonic() >= deadline:
            break
        if all_evidence and round_idx > 0:
            break  # đã có evidence, không cần vòng bổ sung
        rounds_used += 1

        queries, calls = await _generate_queries(
            request, target, already_used=used_queries, services=services
        )
        model_calls += calls
        queries = queries[: cfg.max_search_queries]
        used_queries.extend(queries)

        hits: list[RawSearchHit] = []
        for q in queries:
            if len(hits) + len(all_fetched) >= cfg.max_articles:
                break
            search_calls += 1
            try:
                remaining = max(0.1, deadline - time.monotonic())
                q_hits = await asyncio.wait_for(
                    services.search_provider.search(
                        q,
                        date_from=time_range.date_from,
                        date_to=time_range.date_to,
                        max_results=cfg.max_articles,
                    ),
                    timeout=min(cfg.request_timeout_s, remaining),
                )
            except (asyncio.TimeoutError, Exception):
                q_hits = []
            for h in q_hits:
                query_by_url.setdefault(h.url, q)
            hits.extend(q_hits)

        hits = hits[: max(0, cfg.max_articles - len(all_fetched))]
        considered += len(hits)

        async def _fetch_one(hit: RawSearchHit) -> FetchedArticle | None:
            async with sem:
                for attempt in range(cfg.max_retries + 1):
                    try:
                        remaining = max(0.1, deadline - time.monotonic())
                        art = await asyncio.wait_for(
                            services.fetcher.fetch(
                                hit.url, timeout_s=cfg.request_timeout_s
                            ),
                            timeout=min(cfg.request_timeout_s, remaining),
                        )
                        if art.fetch_error:
                            continue
                        return art
                    except Exception:
                        if attempt == cfg.max_retries:
                            return None
                        await asyncio.sleep(0.5 * (attempt + 1))
            return None

        fetch_calls += len(hits)
        fetched_results = await asyncio.gather(*[_fetch_one(h) for h in hits])
        round_fetched = [a for a in fetched_results if a is not None]
        fetch_failures += len(hits) - len(round_fetched)
        all_fetched.extend(round_fetched)

        # Không dùng bài xuất bản sau as_of (mục 5)
        all_fetched = [
            a for a in all_fetched if a.published_at is None or a.published_at <= as_of
        ]

        # Loại trùng lớp 1
        deduped, _removed = dedup_articles(all_fetched)

        # Lọc relevance qua model
        relevant, calls = await _filter_relevance(
            request, target, deduped, services
        )
        model_calls += calls

        all_evidence = [
            _to_evidence(art, query=query_by_url.get(art.url, ""))
            for art in relevant
        ]

        if all_evidence:
            break

    _, duplicates_removed = dedup_articles(all_fetched)
    diag = {
        "queries": used_queries,
        "search_calls": search_calls,
        "fetch_calls": fetch_calls,
        "fetch_failures": fetch_failures,
        "rounds_used": rounds_used,
        "considered": considered,
        "duplicates_removed": duplicates_removed,
        "model_calls": model_calls,
    }
    return all_evidence[: services.config.max_articles], diag


def _to_evidence(article: FetchedArticle, *, query: str) -> Evidence:
    content = article.content or ""
    return Evidence(
        evidence_id=f"ev_{uuid.uuid4().hex[:10]}",
        title=article.title,
        url=article.url,
        source=article.source,
        published_at=article.published_at,
        retrieved_at=datetime.now(timezone.utc),
        event_date=None,
        content=content,
        content_scope=(
            ContentScope.FULL_TEXT
            if article.content_scope == "full_text"
            else ContentScope.SNIPPET
        ),
        query=query,
        content_hash=_content_hash(content),
    )


async def _generate_queries(
    request: WebAnalysisRequest,
    target: TargetCompany,
    *,
    already_used: list[str],
    services: WebAgentServices,
) -> tuple[list[str], int]:
    system = QUERY_GENERATION_SYSTEM.format(
        max_queries=services.config.max_search_queries
    )
    user = (
        f"Câu hỏi: {request.question}\n"
        f"Doanh nghiệp: {target.company_name or ''} ({target.ticker or ''})\n"
        f"Chủ đề: {', '.join(request.topics) or '(không giới hạn)'}\n"
        f"Query đã dùng trước đó (tránh lặp lại y hệt): {already_used or '(chưa có)'}"
    )
    try:
        data = await services.model_client.complete_json(system=system, user=user)
        queries = [str(q) for q in data.get("queries", []) if str(q).strip()]
    except Exception:
        queries = []
    if not queries:
        # fallback tối thiểu để không chạy rỗng hoàn toàn
        base = target.company_name or target.ticker or ""
        queries = [f"{base} {t}".strip() for t in (request.topics or [""])][:3] or [base]
    return queries, 1


async def _filter_relevance(
    request: WebAnalysisRequest,
    target: TargetCompany,
    articles: list[FetchedArticle],
    services: WebAgentServices,
) -> tuple[list[FetchedArticle], int]:
    if not articles:
        return [], 0
    sem = asyncio.Semaphore(services.config.max_concurrency)
    calls = 0

    async def _check(art: FetchedArticle) -> bool:
        nonlocal calls
        async with sem:
            user = (
                f"Doanh nghiệp mục tiêu: {target.company_name or ''} "
                f"({target.ticker or ''})\n"
                f"Câu hỏi: {request.question}\n"
                f"--- Nội dung bài báo (dữ liệu không tin cậy) ---\n"
                f"Tiêu đề: {art.title}\n{art.content[:4000]}"
            )
            try:
                data = await services.model_client.complete_json(
                    system=RELEVANCE_FILTER_SYSTEM, user=user
                )
                calls += 1
                return bool(data.get("is_relevant", False))
            except Exception:
                calls += 1
                return False

    results = await asyncio.gather(*[_check(a) for a in articles])
    kept = [a for a, ok in zip(articles, results) if ok]
    return kept, calls


async def _group_into_events(
    request: WebAnalysisRequest,
    target: TargetCompany,
    evidence: list[Evidence],
    services: WebAgentServices,
) -> tuple[list[dict], int]:
    listing = "\n".join(
        f"[{e.evidence_id}] {e.title} — {e.content[:800]}" for e in evidence
    )
    user = (
        f"Doanh nghiệp mục tiêu: {target.company_name or ''} ({target.ticker or ''})\n"
        f"Câu hỏi: {request.question}\n"
        f"--- Danh sách evidence (dữ liệu không tin cậy) ---\n{listing}"
    )
    try:
        data = await services.model_client.complete_json(
            system=EVENT_GROUPING_SYSTEM, user=user, max_tokens=3000
        )
        groups = data.get("events", [])
    except Exception:
        groups = []

    valid_ids = {e.evidence_id for e in evidence}
    cleaned: list[dict] = []
    grouped_ids: set[str] = set()
    for g in groups:
        ids = [i for i in g.get("evidence_ids", []) if i in valid_ids]
        if not ids:
            continue
        grouped_ids.update(ids)
        cleaned.append(
            {
                "event_summary": g.get("event_summary", ""),
                "evidence_ids": ids,
                "claim_type": g.get("claim_type", "confirmed"),
            }
        )

    # Evidence nào chưa được model gom -> mỗi cái thành 1 event riêng
    # (an toàn hơn là bỏ sót; code không tự merge thêm vì không có căn cứ).
    for e in evidence:
        if e.evidence_id not in grouped_ids:
            cleaned.append(
                {
                    "event_summary": e.title,
                    "evidence_ids": [e.evidence_id],
                    "claim_type": "confirmed",
                }
            )

    return cleaned, 1


async def _score_events(
    request: WebAnalysisRequest,
    target: TargetCompany,
    groups: list[dict],
    evidence: list[Evidence],
    services: WebAgentServices,
) -> tuple[list[EventSentiment], int]:
    ev_by_id = {e.evidence_id: e for e in evidence}
    sem = asyncio.Semaphore(services.config.max_concurrency)
    calls = 0

    async def _score(group: dict) -> EventSentiment | None:
        nonlocal calls
        async with sem:
            ids = group["evidence_ids"]
            listing = "\n".join(
                f"[{i}] {ev_by_id[i].title}\n{ev_by_id[i].content[:3000]}"
                for i in ids
                if i in ev_by_id
            )
            user = (
                f"Doanh nghiệp mục tiêu: {target.company_name or ''} "
                f"({target.ticker or ''})\n"
                f"Câu hỏi gốc: {request.question}\n"
                f"Sự kiện: {group['event_summary']}\n"
                f"--- Evidence liên quan (dữ liệu không tin cậy) ---\n{listing}"
            )
            try:
                data = await services.model_client.complete_json(
                    system=SENTIMENT_SCORING_SYSTEM, user=user
                )
                calls += 1
            except Exception:
                calls += 1
                data = {
                    "sentiment_label": "insufficient",
                    "rationale": "Không thể chấm điểm do lỗi khi gọi model.",
                    "supporting_quotes": [],
                    "caveats": None,
                }

            label_raw = str(data.get("sentiment_label", "insufficient"))
            try:
                label = SentimentLabel(label_raw)
            except ValueError:
                label = SentimentLabel.INSUFFICIENT

            quotes = [
                SupportingQuote(
                    evidence_id=str(q.get("evidence_id", "")),
                    quote=str(q.get("quote", "")),
                )
                for q in data.get("supporting_quotes", [])
                if q.get("evidence_id") in ev_by_id
            ]

            try:
                claim_type = ClaimType(group.get("claim_type", "confirmed"))
            except ValueError:
                claim_type = ClaimType.CONFIRMED

            return EventSentiment(
                event_id=f"evt_{uuid.uuid4().hex[:10]}",
                event_summary=group["event_summary"],
                evidence_ids=ids,
                sentiment_label=label,
                claim_type=claim_type,
                rationale=str(data.get("rationale", "")),
                supporting_quotes=quotes,
                caveats=data.get("caveats"),
            )

    results = await asyncio.gather(*[_score(g) for g in groups])
    events = [r for r in results if r is not None]
    return events, calls


async def _summarize(
    request: WebAnalysisRequest,
    target: TargetCompany,
    events: list[EventSentiment],
    aggregate,
    services: WebAgentServices,
) -> tuple[str, int]:
    if not events:
        return "Không có sự kiện nào đủ dữ liệu để đánh giá.", 0
    listing = "\n".join(
        f"- ({e.sentiment_label.value}) {e.event_summary}: {e.rationale}"
        for e in events
    )
    user = (
        f"Câu hỏi: {request.question}\n"
        f"Doanh nghiệp: {target.company_name or ''} ({target.ticker or ''})\n"
        f"Các sự kiện đã chấm:\n{listing}\n"
        f"Điểm tổng hợp: {aggregate.score} "
        f"({aggregate.scored_event_count} sự kiện có điểm, "
        f"{aggregate.unscored_event_count} sự kiện không tính điểm)"
    )
    try:
        data = await services.model_client.complete_json(system=SUMMARY_SYSTEM, user=user)
        return str(data.get("summary", "")), 1
    except Exception:
        return (
            "Có đủ evidence nhưng không tạo được tóm tắt tự động — xem chi "
            "tiết từng sự kiện bên dưới.",
            1,
        )
