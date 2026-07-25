"""Format workflow state into compact API-style response payloads."""
# Code note: Formatter code is the final presentation layer; it should not change computed facts.

import re

from common import dedupe_keep_order as _dedupe_keep_order

ANALYSIS_AGENT_HEADERS = {
    "agent_profitability": "Agent Profitability",
    "agent_liquidity_solvency": "Agent Liquidity Solvency",
    "agent_cashflow_analysis": "Agent Cashflow Analysis",
    "agent_efficiency": "Agent Efficiency",
}

ANALYSIS_ASPECT_HEADINGS = (
    "**1. Khả năng sinh lời**",
    "**2. Thanh khoản và an toàn tài chính**",
    "**3. Dòng tiền**",
    "**4. Hiệu quả hoạt động**",
)

ANALYSIS_ASPECT_CONCLUSION_RE = re.compile(
    r"(?ims)^\s*(?:#{1,6}\s*)?\*{0,2}\s*Kết luận khía cạnh\s*\*{0,2}\s*:?.*?"
    r"(?=^\s*(?:#{1,6}\s+|\*\*[^*\n]+\*\*)|\Z)"
)


def _strip_answer_prefix(answer: str) -> str:
    text = str(answer or "").strip()
    if text.lower().startswith("answer:"):
        return text.split(":", 1)[1].strip()
    return text


def _format_answer_prefix(answer: str) -> str:
    text = _strip_answer_prefix(answer)
    if not text:
        text = "Chưa đủ dữ liệu để trả lời."
    return "ANSWER: " + text


def _strip_analysis_aspect_heading(answer: str) -> str:
    text = _strip_answer_prefix(answer)
    for heading in ANALYSIS_ASPECT_HEADINGS:
        if text.startswith(heading):
            return text[len(heading):].lstrip()
    return text


def _strip_analysis_aspect_conclusion(answer: str) -> str:
    text = str(answer or "").strip()
    if not text:
        return text
    return ANALYSIS_ASPECT_CONCLUSION_RE.sub("", text).strip()


def _analysis_agent_order(state: dict) -> list[str]:
    ordered = []

    worker_plan = state.get("worker_plan", {}) or {}
    for target in worker_plan.get("targets", []) or []:
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if agent in ANALYSIS_AGENT_HEADERS and agent not in ordered:
            ordered.append(agent)

    for agent in (state.get("worker_results", {}) or {}).keys():
        if agent in ANALYSIS_AGENT_HEADERS and agent not in ordered:
            ordered.append(agent)

    for item in state.get("worker_messages", []) or []:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if agent in ANALYSIS_AGENT_HEADERS and agent not in ordered:
            ordered.append(agent)

    return ordered


def _latest_analysis_payloads_from_messages(state: dict) -> dict:
    latest = {}

    for item in state.get("worker_messages", []) or []:
        if not isinstance(item, dict):
            continue

        agent = str(item.get("agent", "") or "").strip()
        if agent not in ANALYSIS_AGENT_HEADERS:
            continue
        if str(item.get("kind", "") or "").strip() != "agent_response":
            continue

        parsed = item.get("parsed_output")
        if not isinstance(parsed, dict):
            continue
        if str(parsed.get("kind", "") or "").strip().lower() == "tool_calls":
            continue
        if "answer" not in parsed and "requirements" not in parsed:
            continue

        try:
            round_n = int(item.get("round", 0) or 0)
        except (TypeError, ValueError):
            round_n = 0

        previous = latest.get(agent)
        if previous is not None and int(previous.get("round", -1)) > round_n:
            continue

        latest[agent] = {
            "answer": str(parsed.get("answer", "") or "").strip(),
            "requirements": parsed.get("requirements", []) or [],
            "round": round_n,
        }

    return latest


def _analysis_payloads_for_final_round(state: dict) -> dict:
    payloads = {}
    for agent, payload in (state.get("worker_results", {}) or {}).items():
        if agent not in ANALYSIS_AGENT_HEADERS or not isinstance(payload, dict):
            continue
        payloads[agent] = payload
    payloads.update(_latest_analysis_payloads_from_messages(state))
    return payloads


def _format_analysis_sections(state: dict) -> list[str]:
    analysis_payloads = _analysis_payloads_for_final_round(state)
    sections = []

    for agent in _analysis_agent_order(state):
        payload = analysis_payloads.get(agent, {})
        if not isinstance(payload, dict):
            continue

        answer = _strip_analysis_aspect_conclusion(
            _strip_analysis_aspect_heading(payload.get("answer", ""))
        )
        if not answer:
            continue

        label = ANALYSIS_AGENT_HEADERS.get(agent, agent)
        sections.append(f"=== {label} ===\n{_format_answer_prefix(answer)}")

    return sections


def _not_found_messages(state: dict) -> list[str]:
    messages = []
    for payload in (state.get("worker_results", {}) or {}).values():
        if not isinstance(payload, dict):
            continue
        for fact in payload.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            if str(fact.get("status", "") or "").strip() != "not_found_after_search":
                continue
            # ``message`` is canonical.  ``interpretation_hint`` remains a
            # read-only compatibility field for v1 reports.
            message = str(
                fact.get("message", "")
                or fact.get("interpretation_hint", "")
                or ""
            ).strip()
            if message:
                messages.append(message)
    return _dedupe_keep_order(messages)


def format_final_answer(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    status = d.get("status", "answer")
    answer = _strip_answer_prefix(d.get("answer", ""))
    if not answer:
        answer = "Chưa đủ dữ liệu để trả lời."

    not_found_messages = [
        message
        for message in _not_found_messages(state)
        if message not in answer
    ]
    if not_found_messages:
        answer = "\n".join([answer, *not_found_messages])

    # The synthesizer already incorporates successful analysis outputs.  Printing
    # the worker sections again duplicates the answer and can expose stale rounds.
    lines = [f"=== FINAL ANSWER ===\n{_format_answer_prefix(answer)}"]

    if status == "need_more":
        missing = list(d.get("missing", []) or [])
        for followup in (d.get("followups", []) or []):
            if not isinstance(followup, dict):
                continue
            missing.extend(followup.get("requirements", []) or [])

        missing = _dedupe_keep_order(missing)
        if missing:
            lines.append("Còn thiếu dữ liệu:")
            lines.extend([f"- {x}" for x in missing])
        return "\n\n".join(lines)

    return "\n\n".join(lines)
