import json
import re
import time
from agents.planner_hints import infer_table_keywords
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import build_allowed_keywords_payload
from llm.invoke import extract_usage_metadata, invoke_prompt, merge_usage_metadata
from graph.logger import debug_enabled, make_debug_log, make_log
from schemas.agent_outputs import (
    WorkerStructuredOutput,
    parse_worker_response,
    parse_worker_response_payload,
)


AGENT_DEFAULT_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "agent_web": "WEB",
}

AGENT_DEFAULT_TOOL = {
    "agent_bs": "get_related_info",
    "agent_is": "get_related_info",
    "agent_cf": "get_related_info",
    "agent_web": "web_search",
}

_DOC_FACT_RE = re.compile(
    r"(?:Công ty\s+.+?\.\s*)?"
    r"(?:Bảng\s+(?P<table>.+?)\.\s*)?"
    r"(?P<item>.+?)\.\s*Giá trị\s+(?P<value>.+?)\.?$",
    flags=re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)
_ABBREV_REPLACEMENTS = {
    "tndn": "thu nhập doanh nghiệp",
    "tscd": "tài sản cố định",
    "tscđ": "tài sản cố định",
    "hdkd": "hoạt động kinh doanh",
    "hđkd": "hoạt động kinh doanh",
    "lctt": "lưu chuyển tiền tệ",
    "lnst": "lợi nhuận sau thuế",
    "qldn": "quản lý doanh nghiệp",
}

def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le, khong giai thich them.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- Neu can goi tool, tra: {"kind":"action","action":"get_related_info|web_search","arguments":{"query":"..."}}.\n'
        '- Neu da du du lieu, tra: {"kind":"answer","facts":[]}.\n'
    )


def extract_text(resp):
    if isinstance(resp, str):
        return resp
    if hasattr(resp, "content"):
        content = getattr(resp, "content", "")
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content or "")
    return str(resp or "")


def _serialize_payload(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)

def _plain_worker_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_output_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _allowed_keywords_payload_for_agent(agent_name: str) -> str:
    table_name = AGENT_DEFAULT_TABLE.get(agent_name, "")
    return build_allowed_keywords_payload([table_name])


def _tool_obs_for_agent(state: dict, agent_name: str) -> str:
    items = state.get("tool_observations", []) or []
    lines = [
        str(x.get("text", ""))
        for x in items
        if str(x.get("agent", "")).strip() == agent_name
    ]
    return "\n".join(lines)


def _tool_obs_items_for_agent(state: dict, agent_name: str) -> list[str]:
    items = state.get("tool_observations", []) or []
    return [
        str(x.get("text", ""))
        for x in items
        if str(x.get("agent", "")).strip() == agent_name
    ]


def _has_nonempty_tool_context(state: dict, agent_name: str) -> bool:
    for text in _tool_obs_items_for_agent(state, agent_name):
        stripped = str(text or "").strip()
        if not stripped or "<EMPTY_CONTEXT>" in stripped:
            continue
        if stripped.startswith("[Tool ") or stripped.startswith("[No "):
            continue
        if stripped.startswith("[get_related_info") or stripped.startswith("[AUTO_FOLLOWUP") or stripped.startswith("[web_search"):
            return True
    return False


def _parsed_kind(parsed_output) -> str:
    if not isinstance(parsed_output, dict):
        return ""
    return str(parsed_output.get("kind", "")).strip().lower()


def _default_table_for_agent(agent_name: str) -> str:
    return str(AGENT_DEFAULT_TABLE.get(agent_name, "") or "").strip()


def _normalize_answer_output(agent_name: str, parsed_output):
    if not isinstance(parsed_output, dict):
        return parsed_output
    if _parsed_kind(parsed_output) != "answer":
        return parsed_output

    normalized = dict(parsed_output)
    facts = normalized.get("facts", [])
    normalized_facts = facts if isinstance(facts, list) else []
    inferred_table = (
        str(normalized.get("table", "") or "").strip()
        or next(
            (
                str((fact or {}).get("table", "") or "").strip()
                for fact in normalized_facts
                if isinstance(fact, dict) and str((fact or {}).get("table", "") or "").strip()
            ),
            "",
        )
        or _default_table_for_agent(agent_name)
    )

    normalized["table"] = inferred_table

    rewritten_facts = []
    for fact in normalized_facts:
        if not isinstance(fact, dict):
            rewritten_facts.append(fact)
            continue
        fact_table = str(fact.get("table", "") or "").strip() or inferred_table
        rewritten_facts.append({**fact, "table": fact_table})

    normalized["facts"] = rewritten_facts
    return normalized


def _run_worker_once(payload: dict):
    parsed_output = None
    parse_error = ""
    raw_text = ""
    fallback_mode = ""
    usage = {}

    try:
        resp = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=WorkerStructuredOutput,
            plain_payload_factory=_plain_worker_payload,
        )
        if resp.get("mode") != "structured":
            fallback_mode = str(resp.get("mode", "") or "")
    except Exception as e:
        parse_error = str(e)
        resp = None

    if resp is not None:
        raw_text = extract_text((resp or {}).get("raw", ""))
        usage = extract_usage_metadata((resp or {}).get("raw"))

        try:
            parsed_candidate = (resp or {}).get("parsed")
            if parsed_candidate is not None:
                parsed_output = parse_worker_response_payload(parsed_candidate).model_dump()
            elif raw_text:
                parsed_output = parse_worker_response(raw_text).model_dump()
        except Exception as e:
            parse_error = str(e)

        if not parse_error and (resp or {}).get("parsing_error") is not None:
            parse_error = str((resp or {}).get("parsing_error"))

    response_text = raw_text or _serialize_payload(parsed_output)

    if parsed_output is None and response_text:
        try:
            parsed_output = parse_worker_response(response_text).model_dump()
            parse_error = ""
        except Exception:
            pass

    return parsed_output, response_text, parse_error, fallback_mode, usage


def _force_answer_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Bạn đã có tool_observations không rỗng.\n"
        "- KHÔNG được trả kind=\"action\" nữa.\n"
        "- PHẢI trả kind=\"answer\" ngay từ toàn bộ tool_observations hiện có.\n"
        "- Nếu chưa trích được số liệu mới, vẫn trả kind=\"answer\" với facts=[].\n"
    )


def _split_item_and_time_hint(item_text: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(item_text or "").split("|") if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " | ".join(parts[1:])


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    for short, expanded in _ABBREV_REPLACEMENTS.items():
        text = re.sub(rf"\b{re.escape(short)}\b", expanded, text)
    return " ".join(text.split())


def _text_tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalize_text(value)))


def _fact_match_score(query: str, fact: dict) -> float:
    query_norm = _normalize_text(query)
    item_name = str((fact or {}).get("item_name", "") or "").strip()
    item_norm = _normalize_text(item_name)
    if not query_norm or not item_norm:
        return 0.0

    query_tokens = _text_tokens(query)
    item_tokens = _text_tokens(item_name)
    score = 0.0

    if item_norm == query_norm:
        score += 100.0
    if query_norm and query_norm in item_norm:
        score += 40.0

    if query_tokens and item_tokens:
        overlap = len(query_tokens & item_tokens)
        score += (overlap / len(query_tokens)) * 25.0
        score += (overlap / len(item_tokens)) * 10.0

    return score


def _extract_fact_from_context_line(
    line: str,
    *,
    fallback_table: str = "",
    fallback_source: str = "",
) -> dict | None:
    text = " ".join(str(line or "").split())
    if not text or text == "<EMPTY_CONTEXT>":
        return None

    match = _DOC_FACT_RE.search(text)
    if not match:
        return None

    item_name, time_hint = _split_item_and_time_hint(match.group("item") or "")
    value = str(match.group("value") or "").strip(" .")
    table = str(match.group("table") or fallback_table).strip() or fallback_table

    if not item_name and not value:
        return None

    return {
        "item_name": item_name,
        "time_hint": time_hint,
        "value": value,
        "source": fallback_source,
        "table": table,
    }


def _dedupe_facts(facts: list[dict]) -> list[dict]:
    deduped = []
    seen = set()

    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        key = (
            str(fact.get("item_name", "")).strip(),
            str(fact.get("time_hint", "")).strip(),
            str(fact.get("value", "")).strip(),
            str(fact.get("source", "")).strip(),
            str(fact.get("table", "")).strip(),
        )
        if key in seen:
            continue
        deduped.append(fact)
        seen.add(key)

    return deduped


def _facts_from_tool_results(state: dict, agent_name: str) -> list[dict]:
    current_round = int((state or {}).get("followup_rounds", 0) or 0)
    scored_facts = []
    order = 0

    for item in state.get("tool_results", []) or []:
        if str(item.get("agent", "")).strip() != agent_name:
            continue
        if str(item.get("tool", "")).strip() != "get_related_info":
            continue

        item_round = item.get("round")
        if item_round is not None and int(item_round) != current_round:
            continue

        args = item.get("args", {}) or {}
        results = item.get("results", {}) or {}
        table = str(args.get("table", "") or AGENT_DEFAULT_TABLE.get(agent_name, "")).strip()
        query = str(args.get("query", "") or "").strip()
        source = str(results.get("source", "") or "").strip()
        context = str(results.get("context", "") or "")

        for line in context.splitlines():
            fact = _extract_fact_from_context_line(
                line,
                fallback_table=table,
                fallback_source=source,
            )
            if fact is not None:
                scored_facts.append((_fact_match_score(query, fact), order, fact))
                order += 1

    scored_facts.sort(key=lambda item: (-item[0], item[1]))
    if not scored_facts:
        return []

    best_score = scored_facts[0][0]
    min_score = max(10.0, best_score * 0.6) if best_score > 0 else 0.0
    filtered = [
        fact
        for score, _order, fact in scored_facts
        if score >= min_score
    ]
    if not filtered:
        filtered = [fact for _score, _order, fact in scored_facts[:2]]

    return _dedupe_facts(filtered[:3])


def _coerce_tool_context_to_answer(state: dict, agent_name: str) -> dict | None:
    previous = (state.get("worker_results", {}) or {}).get(agent_name, {}) or {}
    previous_facts = previous.get("facts", []) if isinstance(previous.get("facts"), list) else []
    extracted_facts = _facts_from_tool_results(state, agent_name)
    facts = _dedupe_facts(list(previous_facts) + extracted_facts)

    if not facts:
        return None

    table = (
        str(previous.get("table", "")).strip()
        or next((str(fact.get("table", "")).strip() for fact in facts if str(fact.get("table", "")).strip()), "")
        or _default_table_for_agent(agent_name)
    )

    return {
        "kind": "answer",
        "table": table,
        "facts": facts,
    }


def _coerce_repeat_action_to_answer(state: dict, agent_name: str) -> dict:
    previous = (state.get("worker_results", {}) or {}).get(agent_name, {}) or {}
    table = str(previous.get("table", "")).strip() or _default_table_for_agent(agent_name)
    facts = previous.get("facts", []) if isinstance(previous.get("facts"), list) else []

    return {
        "kind": "answer",
        "table": table,
        "facts": facts,
    }


def _answer_facts_n(parsed_output) -> int:
    if not isinstance(parsed_output, dict):
        return 0
    if _parsed_kind(parsed_output) != "answer":
        return 0
    facts = parsed_output.get("facts", [])
    if not isinstance(facts, list):
        return 0
    return len(facts)


def _worker_trace_summary(agent_name: str, parsed_output, response_text: str) -> dict:
    summary = {
        "agent": agent_name,
        "result_kind": _parsed_kind(parsed_output) or "unknown",
    }

    if isinstance(parsed_output, dict):
        if summary["result_kind"] == "answer":
            summary["table"] = str(parsed_output.get("table", "") or "").strip()
            summary["facts_n"] = _answer_facts_n(parsed_output)
        elif summary["result_kind"] == "action":
            args = parsed_output.get("arguments", {}) or {}
            summary["action"] = str(parsed_output.get("action", "") or "").strip()
            summary["query"] = str(args.get("query", "") or "").strip()

    if not isinstance(parsed_output, dict) and str(response_text or "").strip():
        summary["response_preview"] = str(response_text or "")[:120]

    return summary


def _target_keywords_for_agent(state: dict, agent_name: str) -> list[str]:
    table = AGENT_DEFAULT_TABLE.get(agent_name, "")
    worker_plan = state.get("worker_plan", {}) or {}

    for target in (worker_plan.get("targets", []) or []):
        if str(target.get("table", "")).strip() != table:
            continue
        return [
            str(keyword).strip()
            for keyword in (target.get("keywords", []) or [])
            if str(keyword).strip()
        ]

    return []


def _planner_hint_keywords_for_agent(state: dict, agent_name: str) -> list[str]:
    table = AGENT_DEFAULT_TABLE.get(agent_name, "")
    if not table:
        return []

    planner_plan = state.get("planner_plan", {}) or {}
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    return [
        str(keyword).strip()
        for keyword in infer_table_keywords(
            table,
            str(state.get("user_query", "") or ""),
            analysis_axes,
        )
        if str(keyword).strip()
    ]


def _build_auto_action_fallback(state: dict, agent_name: str) -> dict | None:
    tool_name = AGENT_DEFAULT_TOOL.get(agent_name, "")
    if not tool_name:
        return None

    query = ""
    keywords = _target_keywords_for_agent(state, agent_name)
    if keywords:
        query = keywords[0]

    if not query:
        planner_keywords = _planner_hint_keywords_for_agent(state, agent_name)
        if planner_keywords:
            query = planner_keywords[0]

    if not query:
        query = str(state.get("worker_query", "") or "").strip()
    if not query:
        query = str(state.get("user_query", "") or "").strip()
    if not query:
        return None

    return {
        "kind": "action",
        "action": tool_name,
        "arguments": {"query": query},
    }


def call_worker_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    started_at = time.perf_counter()

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": state.get("worker_query", ""),
        "plan_json": json.dumps(state.get("worker_plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "allowed_keywords_json": _allowed_keywords_payload_for_agent(agent_name),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": _tool_obs_for_agent(state, agent_name),
        "tools_list": profile.get("tool_list", ""),
    }

    parsed_output, response_text, parse_error, fallback_mode, usage = _run_worker_once(payload)
    parsed_output = _normalize_answer_output(agent_name, parsed_output)
    if _parsed_kind(parsed_output) == "answer":
        response_text = _serialize_payload(parsed_output)
    trace = []
    llm_usage = merge_usage_metadata(usage)
    llm_calls = 1

    if fallback_mode:
        fallback_log = make_debug_log(
            state,
            "agent:structured_output_fallback",
            agent_name=agent_name,
            mode=fallback_mode,
        )
        if fallback_log:
            trace.append(fallback_log)

    if (
        parsed_output is None
        and not str(response_text or "").strip()
        and not _has_nonempty_tool_context(state, agent_name)
    ):
        auto_action = _build_auto_action_fallback(state, agent_name)
        if auto_action is not None:
            parsed_output = auto_action
            response_text = _serialize_payload(auto_action)
            debug_log = make_debug_log(
                state,
                "agent:auto_action_fallback",
                agent_name=agent_name,
                action=auto_action.get("action", ""),
                query=((auto_action.get("arguments") or {}).get("query", "")),
            )
            if debug_log:
                trace.append(debug_log)

    if _parsed_kind(parsed_output) == "action" and _has_nonempty_tool_context(state, agent_name):
        retry_payload = dict(payload)
        retry_payload["last_agent_response"] = response_text
        retry_payload["system_instruction"] = _force_answer_instruction(profile["system_instruction"])
        debug_log = make_debug_log(
            state,
            "agent:force_answer_retry",
            agent_name=agent_name,
            previous_response=response_text[:160],
        )
        if debug_log:
            trace.append(debug_log)

        retry_parsed_output, retry_response_text, retry_parse_error, retry_fallback_mode, retry_usage = _run_worker_once(retry_payload)
        retry_parsed_output = _normalize_answer_output(agent_name, retry_parsed_output)
        if _parsed_kind(retry_parsed_output) == "answer":
            retry_response_text = _serialize_payload(retry_parsed_output)
        llm_usage = merge_usage_metadata(llm_usage, retry_usage)
        llm_calls += 1
        if retry_fallback_mode:
            fallback_log = make_debug_log(
                state,
                "agent:structured_output_fallback",
                agent_name=agent_name,
                mode=retry_fallback_mode,
            )
            if fallback_log:
                trace.append(fallback_log)
        if _parsed_kind(retry_parsed_output) == "answer":
            parsed_output = retry_parsed_output
            response_text = retry_response_text
            parse_error = retry_parse_error
        else:
            inferred_answer = _coerce_tool_context_to_answer(state, agent_name)
            if inferred_answer is not None:
                parsed_output = inferred_answer
                response_text = _serialize_payload(parsed_output)
                parse_error = retry_parse_error or parse_error
                debug_log = make_debug_log(
                    state,
                    "agent:force_answer_from_tool_context",
                    agent_name=agent_name,
                    table=inferred_answer.get("table", ""),
                    facts_n=len(inferred_answer.get("facts", []) or []),
                )
                if debug_log:
                    trace.append(debug_log)
            else:
                parsed_output = _coerce_repeat_action_to_answer(state, agent_name)
                response_text = _serialize_payload(parsed_output)
                parse_error = retry_parse_error or parse_error
                debug_log = make_debug_log(
                    state,
                    "agent:force_answer_fallback",
                    agent_name=agent_name,
                    kept_facts_n=len(parsed_output.get("facts", []) or []),
                )
                if debug_log:
                    trace.append(debug_log)

    if (
        (_parsed_kind(parsed_output) != "answer" or _answer_facts_n(parsed_output) == 0)
        and _has_nonempty_tool_context(state, agent_name)
    ):
        inferred_answer = _coerce_tool_context_to_answer(state, agent_name)
        if inferred_answer is not None:
            parsed_output = inferred_answer
            response_text = _serialize_payload(parsed_output)
            debug_log = make_debug_log(
                state,
                "agent:tool_context_fallback",
                agent_name=agent_name,
                table=inferred_answer.get("table", ""),
                facts_n=len(inferred_answer.get("facts", []) or []),
            )
            if debug_log:
                trace.append(debug_log)

    if parse_error:
        error_event = "agent:error"
        error_payload = {
            "agent_name": agent_name,
            "error": parse_error[:250],
            "empty_response": (not str(response_text or "").strip()),
        }
        parsed_kind = _parsed_kind(parsed_output)
        if parsed_kind in {"action", "answer"}:
            error_event = "agent:fallback_after_error"
            error_payload["recovered_kind"] = parsed_kind
        trace.append(
            make_log(
                state,
                error_event,
                **error_payload,
            )
        )

    done_log = make_log(
        state,
        "agent:done",
        **_worker_trace_summary(agent_name, parsed_output, response_text),
        llm_calls=llm_calls,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        **llm_usage,
    )
    if debug_enabled(state):
        done_log["response_preview"] = response_text[:160]
    done_log = {key: value for key, value in done_log.items() if value is not None}
    trace.append(done_log)

    return {
        "worker_messages": [
            {
                "agent": agent_name,
                "kind": "agent_response",
                "round": state.get("followup_rounds", 0),
                "response": response_text,
                "parsed_output": parsed_output,
                "parse_error": parse_error,
            }
        ],
        "trace": trace,
    }
