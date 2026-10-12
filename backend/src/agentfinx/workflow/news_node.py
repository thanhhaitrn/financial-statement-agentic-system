"""The company-news branch of the graph (WEB_ANALYSIS_ENABLED).

After ``build_evidence`` leaves a news bundle (need_web, web_intent =
company_news), the analysis runs in one of two ways:

- **parallel** (default): ``start_web`` starts it outside the graph steps
  (``workflow.news_branch``) and the report branch goes on at once; ``join_web``
  waits for it before Synth. Only Synth receives the news.
- **sequential**: when an analysis agent itself needs the news (the question
  ties the news to that agent's figures, see ``web_analysis.facts``), the
  agent must wait for it, so ``agent_web`` runs before the report agents.

One time budget covers the whole branch, fetching and analysis together
(``WEB_BRANCH_BUDGET_SECONDS``): the analysis gets what the fetch left. If it
is not done when the budget ends, the answer goes out without it, marked
unfinished, and the chat adds the news when it is ready.

The branch never searches by itself, reuses its result in a follow-up round
with the same question and articles, and runs at most
``MAX_RUNS_PER_QUESTION`` times for a question.
"""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from __future__ import annotations

import json
from hashlib import sha256

from agentfinx.core.config.runtime_policy import active_policy
from agentfinx.core.schemas.web_analysis import Collection, WebAnalysis, WebContext
from agentfinx.news import service as news_service
from agentfinx.news.analysis.facts import links_news_to_analysis, news_agents
from agentfinx.workflow.logger import make_log
from agentfinx.workflow.router import route_after_evidence

MAX_RUNS_PER_QUESTION = 2


def bundle_key(bundle: dict) -> str:
    context = dict(bundle.get("context") or {})
    batch = dict(bundle.get("batch") or {})
    identity = {
        "context": [context.get(name, "") for name in
                    ("status", "ticker", "question", "date_from", "date_to")],
        "batch": [batch.get("status", ""), bool(batch.get("pending"))],
        "evidence": sorted(
            (str(item.get("evidence_id", "")), str(item.get("content_hash", "")))
            for item in (bundle.get("evidence") or [])
        ),
    }
    return sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def needs_web_analysis(state: dict) -> bool:
    bundle = state.get("web_bundle") or {}
    if not bundle or not active_policy().web.analysis_active:
        return False
    key = bundle_key(bundle)
    if (state.get("web_analysis") or {}).get("memo_key") == key or state.get("web_job") == key:
        return False
    return int(state.get("web_analysis_runs", 0) or 0) < MAX_RUNS_PER_QUESTION


def web_mode(state: dict, *, parallel_available: bool) -> str:
    """"none", "sequential" or "parallel" for the bundle in ``state``."""

    if not needs_web_analysis(state):
        return "none"
    bundle = state.get("web_bundle") or {}
    agent_needs_news = links_news_to_analysis(str(state.get("user_query", "") or "")) and bool(
        set(bundle.get("needby") or []) & set(state.get("expected_workers") or []))
    if agent_needs_news or not parallel_available:
        return "sequential"
    return "parallel"


def make_route_after_build_evidence(*, parallel_available: bool):
    def route(state: dict):
        mode = web_mode(state, parallel_available=parallel_available)
        if mode == "sequential":
            return "agent_web"
        if mode == "parallel":
            return "start_web"
        return route_after_evidence(state)

    return route


# Without a runner (a graph built without services) the branch is sequential.
route_after_build_evidence = make_route_after_build_evidence(parallel_available=False)


def analysis_budget(bundle: dict) -> float:
    """What the branch budget leaves for the analysis after the fetch."""

    policy = active_policy().web
    fetched = float(bundle.get("fetch_seconds", 0.0) or 0.0)
    return max(0.0, min(float(policy.analysis_budget_seconds), float(policy.branch_budget_seconds) - fetched))


def analyze_bundle(bundle: dict, *, web_model=None, budget_seconds: float) -> WebAnalysis:
    try:
        context = WebContext.model_validate(bundle.get("context") or {})
    except ValueError:
        context = WebContext(status="unsupported", message="Thiếu thông tin để tìm tin doanh nghiệp.")
    try:
        analysis = news_service.analyze(context, bundle.get("evidence") or [], bundle.get("batch") or {},
                                        invoke=web_model, budget_seconds=budget_seconds)
    except Exception as exc:
        analysis = WebAnalysis(
            status="error",
            context=context,
            limitations=["Không phân tích được tin tức do lỗi hệ thống; phần báo cáo tài chính vẫn được trả lời."],
            diagnostics={"errors": [f"agent_web:{type(exc).__name__}"]},
        )
    analysis.memo_key = bundle_key(bundle)
    return analysis


def unfinished_analysis(bundle: dict) -> WebAnalysis:
    """The answer goes out without the news; the chat adds it when ready."""

    try:
        context = WebContext.model_validate(bundle.get("context") or {})
    except ValueError:
        context = WebContext(status="unsupported")
    batch = dict(bundle.get("batch") or {})
    return WebAnalysis(
        status="pending",
        context=context,
        coverage=dict(batch.get("coverage") or {}),
        collection=Collection(pending=True),
        limitations=["Phần tin tức chưa phân tích xong trong thời gian cho phép; hệ thống sẽ tự bổ sung khi có kết quả."],
        diagnostics={"unfinished": True},
        memo_key=bundle_key(bundle),
    )


def _web_log(state: dict, analysis: WebAnalysis, *, mode: str, agents: list[str]) -> dict:
    diagnostics = analysis.diagnostics or {}
    # A web problem is supplementary: the run goes on with the report answer.
    event = "agent_web:failed" if analysis.status == "error" else "agent_web:done"
    return make_log(
        state,
        event,
        agent="agent_web",
        scope="web",
        mode=mode,
        status=analysis.status,
        model_calls=int(diagnostics.get("model_calls", 0) or 0),
        duration_ms=int(diagnostics.get("duration_ms", 0) or 0),
        articles_n=int(diagnostics.get("articles_in", 0) or 0),
        events_n=len(analysis.events),
        quotes_rejected_n=int(diagnostics.get("quotes_rejected", 0) or 0),
        unfinished=bool(diagnostics.get("unfinished", False)),
        news_to_agents=agents,
        errors=list(diagnostics.get("errors", []) or []),
    )


def agent_web_node(state: dict, *, web_model=None) -> dict:
    """Sequential: the analysis an analysis agent is about to read."""

    bundle = state.get("web_bundle") or {}
    analysis = analyze_bundle(bundle, web_model=web_model, budget_seconds=analysis_budget(bundle))
    data = analysis.model_dump(mode="json")
    agents = news_agents(
        data,
        question=str(state.get("user_query", "") or ""),
        needby=list(bundle.get("needby") or []),
        dispatched=list(state.get("expected_workers") or []),
    )
    return {
        "web_analysis": data,
        "web_news_agents": agents,
        "web_analysis_runs": int(state.get("web_analysis_runs", 0) or 0) + 1,
        "trace": [_web_log(state, analysis, mode="sequential", agents=agents)],
    }


def start_web_node(state: dict, *, web_runner=None, web_model=None) -> dict:
    """Parallel: start the analysis beside the report branch and move on."""

    bundle = dict(state.get("web_bundle") or {})
    key = bundle_key(bundle)
    budget = analysis_budget(bundle)
    if web_runner is not None:
        web_runner.start(
            key,
            lambda: analyze_bundle(bundle, web_model=web_model, budget_seconds=budget).model_dump(mode="json"),
            budget_seconds=budget,
        )
    return {"web_job": key, "web_news_agents": []}


def join_web_node(state: dict, *, web_runner=None) -> dict:
    """Before Synth: the parallel analysis, or a note that it is unfinished."""

    key = str(state.get("web_job", "") or "")
    if not key or (state.get("web_analysis") or {}).get("memo_key") == key:
        return {}
    bundle = state.get("web_bundle") or {}
    result, finished = (web_runner.join(key) if web_runner is not None else (None, False))
    if finished and result and "status" in result:
        analysis = WebAnalysis.model_validate(result)
    elif finished:
        analysis = analyze_bundle({}, budget_seconds=0)  # the job itself failed
        analysis.memo_key = key
    else:
        analysis = unfinished_analysis(bundle)
    return {
        "web_analysis": analysis.model_dump(mode="json"),
        "web_analysis_runs": int(state.get("web_analysis_runs", 0) or 0) + 1,
        "trace": [_web_log(state, analysis, mode="parallel", agents=[])],
    }
