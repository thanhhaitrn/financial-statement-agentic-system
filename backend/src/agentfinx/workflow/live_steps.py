"""Graph nodes reported as live steps (:mod:`agentfinx.core.progress`).

Each tracked node opens a step when it starts and closes it when it returns.
An analysis agent is one step across its loop: the agent node opens it, its
tool node notes each lookup, its finalize node closes it. The news branch that
runs beside the report agents opens its step in ``start_web`` and closes it in
``join_web``. Details are counts and aspect names; a closed step also carries
what the node produced (plan, lookups, facts, the agent's analysis, news events)
as short Markdown for the user to open.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from agentfinx.agents.agent_registry import ANALYSIS_ASPECT_LABELS
from agentfinx.core import progress

logger = logging.getLogger(__name__)

NEWS_LABEL = "Đọc và phân tích tin tức"


def _route_detail(out: dict) -> str:
    plan = (out or {}).get("worker_plan") or {}
    aspects = []
    for item in plan.get("analysis_plan") or []:
        label = ANALYSIS_ASPECT_LABELS.get(str((item or {}).get("agent") or ""))
        if label and label not in aspects:
            aspects.append(label)
    parts = [", ".join(aspects)] if aspects else []
    if plan.get("need_web"):
        parts.append("có đọc tin tức")
    return " · ".join(parts)


def _evidence_detail(out: dict) -> str:
    items = ((out or {}).get("evidence_pack") or {}).get("items") or []
    return f"{len(items)} số liệu" if items else ""


# --- what a finished step shows when opened (Markdown, built from the node's own output) ---

_LEVELS = {"easy": "Đơn giản", "medium": "Trung bình", "hard": "Phức tạp"}
_FACTS_SHOWN = 30


def _aspect(name: str) -> str:
    return ANALYSIS_ASPECT_LABELS.get(str(name or ""), str(name or ""))


def plan_output(out: dict) -> str:
    plan = (out or {}).get("planner_plan") or {}
    if not plan:
        return ""
    lines = []
    if plan.get("company"):
        lines.append(f"- **Công ty:** {plan['company']}")
    if plan.get("time_hint"):
        lines.append(f"- **Kỳ:** {plan['time_hint']}")
    if plan.get("difficulty_level"):
        lines.append(f"- **Mức độ câu hỏi:** {_LEVELS.get(plan['difficulty_level'], plan['difficulty_level'])}")
    axes = [_aspect(a.get("axis") if isinstance(a, dict) else a) for a in plan.get("analysis_axes") or [] if a]
    if axes:
        lines.append(f"- **Trục phân tích:** {', '.join(axes)}")
    lines.append(f"- **Đọc tin tức:** {'Có' if plan.get('need_web') else 'Không'}")
    return "\n".join(lines)


def route_output(out: dict) -> str:
    plan = (out or {}).get("worker_plan") or {}
    lines = []
    analyses = plan.get("analysis_plan") or []
    if analyses:
        lines.append("**Phân tích sẽ chạy**")
        for item in analyses:
            item = item or {}
            goal = item.get("objective") or "; ".join(str(r) for r in item.get("requirements") or [] if r)
            lines.append(f"- {_aspect(item.get('agent'))}" + (f": {goal}" if goal else ""))
    lookups = plan.get("evidence_plan") or []
    if lookups:
        if lines:
            lines.append("")
        lines.append("**Cần tra cứu**")
        for item in lookups:
            item = item or {}
            where = item.get("table") or ("Tin tức doanh nghiệp" if item.get("web_intent") else "Báo cáo")
            what = ", ".join(str(q) for q in item.get("queries") or [] if q) or str(item.get("query") or "")
            lines.append(f"- {where}" + (f": {what}" if what else ""))
    return "\n".join(line for line in lines).strip()


def _fact_line(fact: dict) -> str:
    name = fact.get("metric_label") or fact.get("row_label") or fact.get("item_name") or "Số liệu"
    period = fact.get("period_label") or ""
    value = str(fact.get("value") or "").strip()
    unit = str(fact.get("unit") or "").strip()
    head = f"{name}" + (f" · {period}" if period else "")
    return f"- {head}: **{value}{(' ' + unit) if unit and unit not in value else ''}**"


def evidence_output(out: dict) -> str:
    groups = (out or {}).get("worker_results") or {}
    lines, total = [], 0
    for table, result in groups.items():
        facts = (result or {}).get("facts") if isinstance(result, dict) else None
        if not facts:
            continue
        lines.append(f"**{table}**")
        for fact in facts:
            if not str((fact or {}).get("value") or "").strip():
                continue
            total += 1
            if total <= _FACTS_SHOWN:
                lines.append(_fact_line(fact or {}))
    if total > _FACTS_SHOWN:
        lines.append(f"\n… và {total - _FACTS_SHOWN} số liệu khác")
    return "\n".join(lines)


_CONFIRMATION = {"official_disclosure": "công bố chính thức", "attributed_report": "có nguồn", "forecast": "dự báo",
                 "unconfirmed": "chưa xác nhận", "unattributed": "không rõ nguồn"}


def news_output(out: dict) -> str:
    analysis = (out or {}).get("web_analysis") or {}
    events = analysis.get("events") or []
    if not events:
        status = analysis.get("status")
        return "Không tìm thấy sự kiện liên quan." if status == "ok" else ""
    lines = []
    for event in events:
        event = event or {}
        meta = [_CONFIRMATION.get(event.get("confirmation"), event.get("confirmation") or "")]
        if event.get("reported_at"):
            meta.append(str(event["reported_at"]))
        metrics = ", ".join(str(m) for m in event.get("related_metrics") or [] if m)
        lines.append(f"- **{event.get('title') or 'Sự kiện'}** · {' · '.join(m for m in meta if m)}"
                     + (f"\n  Liên quan: {metrics}" if metrics else ""))
        quote = next((q.get("text") for q in event.get("quotes") or [] if isinstance(q, dict) and q.get("text")), "")
        if quote:
            quote = " ".join(str(quote).split())
            lines.append(f"  > {quote[:240]}{'…' if len(quote) > 240 else ''}")
    return "\n".join(lines)


def _wrap(node: Callable, before: Callable[[], None], after: Callable[[dict], None], fail_key: str | None) -> Callable:
    @wraps(node)
    def run(state: dict):
        if not progress.active():
            return node(state)
        before()
        try:
            out = node(state)
        except BaseException:
            if fail_key:
                progress.end(fail_key, status="failed")
            raise
        try:
            after(out)
        except Exception:  # describing a step must never fail the answer
            logger.warning("live step after %s failed", getattr(node, "__name__", "node"), exc_info=True)
        return out

    return run


def _simple(key: str, label: str, detail: Callable[[dict], str] | None = None,
            output: Callable[[dict], str] | None = None):
    def factory(node: Callable) -> Callable:
        return _wrap(node, lambda: progress.begin(key, label),
                     lambda out: progress.end(key, detail(out) if detail else None,
                                              output=output(out) if output else None), key)

    return factory


SIMPLE = {
    "agent_main": _simple("plan", "Hiểu câu hỏi và lập kế hoạch", output=plan_output),
    "agent_router": _simple("route", "Chọn hướng phân tích", _route_detail, route_output),
    "build_evidence": _simple("evidence", "Tìm số liệu trong báo cáo", _evidence_detail, evidence_output),
    "agent_web": _simple("news", NEWS_LABEL, output=news_output),
    "agent_synth": _simple("synth", "Tổng hợp câu trả lời"),
}


def track(name: str, node: Callable) -> Callable:
    """``node`` reporting its step, for the nodes that have one."""

    if name in SIMPLE:
        return SIMPLE[name](node)
    if name == "start_web":
        return _wrap(node, lambda: progress.begin("news", NEWS_LABEL, "Chạy song song với phân tích báo cáo"),
                     lambda out: None, "news")
    if name == "join_web":
        return _wrap(node, lambda: None, lambda out: progress.end("news", "", output=news_output(out)), "news")
    return node


def track_analysis(agent_name: str, agent_node: Callable, tool_node: Callable, finalize_node: Callable
                   ) -> tuple[Callable, Callable, Callable]:
    """One step for an analysis agent's loop (agent ↔ tool, then finalize)."""

    key = agent_name
    label = f"Phân tích {ANALYSIS_ASPECT_LABELS.get(agent_name, agent_name).lower()}"
    lookups = {"n": 0}
    answer = {"text": ""}

    def before_agent():
        progress.begin(key, label, f"Đã tra cứu {lookups['n']} lần" if lookups["n"] else "")

    def before_tool():
        lookups["n"] += 1
        progress.update(key, f"Đang tra cứu số liệu (lần {lookups['n']})")

    def after_agent(out):
        result = ((out or {}).get("worker_results") or {}).get(agent_name) or {}
        text = str(result.get("answer") or "").strip() if isinstance(result, dict) else ""
        if text:
            answer["text"] = text

    def after_finalize(out):
        progress.end(key, f"Đã tra cứu {lookups['n']} lần" if lookups["n"] else "", output=answer["text"] or None)

    return (_wrap(agent_node, before_agent, after_agent, key),
            _wrap(tool_node, before_tool, lambda out: None, key),
            _wrap(finalize_node, lambda: None, after_finalize, key))
