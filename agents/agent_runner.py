import json
import time

from agents.agent_tools_list import get_tools_list
from agents.agent_registry import get_default_table, is_analysis_agent
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import build_allowed_keywords_payload
from llm.invoke import extract_usage_metadata, invoke_prompt, merge_usage_metadata
from graph.logger import debug_enabled, make_debug_log, make_log
from schemas.agent_outputs import (
    AnalysisOutput,
    WorkerStructuredOutput,
    parse_analysis_response,
    parse_analysis_response_payload,
    parse_worker_response,
    parse_worker_response_payload,
)


AGENT_DEFAULT_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "agent_web": "WEB",
    "agent_profitability": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_liquidity_solvency": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_cashflow_analysis": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "agent_efficiency": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
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


def _force_json_analysis_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema AnalysisOutput.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- Bat buoc co 2 field: \"answer\" va \"requirements\".\n'
    )


def _plain_analysis_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_analysis_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _allowed_keywords_payload_for_agent(agent_name: str) -> str:
    table_name = AGENT_DEFAULT_TABLE.get(agent_name, "")
    if table_name == "WEB":
        return "{}"
    return build_allowed_keywords_payload([table_name])


def _tool_obs_for_agent(state: dict, agent_name: str) -> str:
    return "\n".join(_tool_obs_items_for_agent(state, agent_name))


def _tool_obs_items_for_agent(state: dict, agent_name: str) -> list[str]:
    items = state.get("tool_observations", []) or []
    current_round = int((state or {}).get("followup_rounds", 0) or 0)
    lines = []

    for item in items:
        if str(item.get("agent", "")).strip() != agent_name:
            continue

        item_round = item.get("round")
        if item_round is None and current_round > 0:
            continue

        if item_round is not None:
            try:
                if int(item_round) != current_round:
                    continue
            except (TypeError, ValueError):
                continue

        lines.append(str(item.get("text", "")))

    return lines


def _has_nonempty_tool_context(state: dict, agent_name: str) -> bool:
    for text in _tool_obs_items_for_agent(state, agent_name):
        stripped = str(text or "").strip()
        if not stripped or "<EMPTY_CONTEXT>" in stripped:
            continue
        if stripped.startswith("[Tool ") or stripped.startswith("[No "):
            continue
        if stripped.startswith("[get_related_info") or stripped.startswith("[web_search"):
            return True
    return False


def _nonempty_tool_context_count(state: dict, agent_name: str) -> int:
    count = 0
    for text in _tool_obs_items_for_agent(state, agent_name):
        stripped = str(text or "").strip()
        if not stripped or "<EMPTY_CONTEXT>" in stripped:
            continue
        if stripped.startswith("[Tool ") or stripped.startswith("[No "):
            continue
        if stripped.startswith("[get_related_info") or stripped.startswith("[web_search"):
            count += 1
    return count


def _tool_call_count_for_round(state: dict, agent_name: str) -> int:
    current_round = int((state or {}).get("followup_rounds", 0) or 0)
    counts = state.get("tool_call_counts", {}) or {}
    value = counts.get(agent_name)

    if isinstance(value, dict):
        try:
            if int(value.get("round", -1)) == current_round:
                return int(value.get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return 0

    if current_round == 0 and isinstance(value, int):
        return value

    return 0


def _assigned_requirements_for_agent(state: dict, agent_name: str) -> list[str]:
    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict) and str(dispatch_target.get("agent", "")).strip() == agent_name:
        return [
            str(item).strip()
            for item in (dispatch_target.get("requirements", []) or [])
            if str(item).strip()
        ]

    worker_plan = state.get("worker_plan", {}) or {}
    requirements = []
    for target in (worker_plan.get("targets", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(
            [
                str(item).strip()
                for item in (target.get("requirements", []) or [])
                if str(item).strip()
            ]
        )

    seen = set()
    normalized = []
    for item in requirements:
        if item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _processed_requirement_count(state: dict, agent_name: str) -> int:
    requirements = _assigned_requirements_for_agent(state, agent_name)
    if not requirements:
        return 0

    processed = max(
        _nonempty_tool_context_count(state, agent_name),
        _tool_call_count_for_round(state, agent_name),
    )
    return min(processed, len(requirements))


def _has_pending_requirement_items(state: dict, agent_name: str) -> bool:
    requirements = _assigned_requirements_for_agent(state, agent_name)
    if not requirements:
        return False
    return _processed_requirement_count(state, agent_name) < len(requirements)


def _next_requirement_item(state: dict, agent_name: str) -> str:
    requirements = _assigned_requirements_for_agent(state, agent_name)
    if not requirements:
        return ""

    current_index = _processed_requirement_count(state, agent_name)
    if 0 <= current_index < len(requirements):
        return str(requirements[current_index] or "").strip()

    return str(requirements[0] or "").strip()


def _parsed_kind(parsed_output) -> str:
    if not isinstance(parsed_output, dict):
        return ""
    return str(parsed_output.get("kind", "")).strip().lower()


def _default_table_for_agent(agent_name: str) -> str:
    return str(AGENT_DEFAULT_TABLE.get(agent_name, "") or get_default_table(agent_name) or "").strip()


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


def _normalize_action_output(agent_name: str, parsed_output, requirement: str = ""):
    if not isinstance(parsed_output, dict):
        return parsed_output, None
    if _parsed_kind(parsed_output) != "action":
        return parsed_output, None

    normalized = dict(parsed_output)
    action_name = str(normalized.get("action", "") or "").strip() or _default_action_name(agent_name)
    arguments = dict(normalized.get("arguments", {}) or {})
    normalized["action"] = action_name
    normalized["arguments"] = arguments

    required_query = str(requirement or "").strip()
    current_query = str(arguments.get("query", "") or "").strip()

    if action_name == "get_related_info" and required_query and current_query != required_query:
        normalized["arguments"] = {**arguments, "query": required_query}
        return normalized, {
            "expected_query": required_query,
            "raw_query": current_query,
        }

    return normalized, None


def _normalize_analysis_output(parsed_output):
    if not isinstance(parsed_output, dict):
        return {
            "answer": "",
            "requirements": [],
        }
    return {
        "answer": str(parsed_output.get("answer", "") or "").strip(),
        "requirements": [
            str(item).strip()
            for item in (parsed_output.get("requirements", []) or [])
            if str(item).strip()
        ],
    }


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


def _run_analysis_once(payload: dict):
    parsed_output = None
    parse_error = ""
    raw_text = ""
    fallback_mode = ""
    usage = {}

    try:
        resp = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=AnalysisOutput,
            plain_payload_factory=_plain_analysis_payload,
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
                parsed_output = parse_analysis_response_payload(parsed_candidate).model_dump()
            elif raw_text:
                parsed_output = parse_analysis_response(raw_text).model_dump()
        except Exception as e:
            parse_error = str(e)

        if not parse_error and (resp or {}).get("parsing_error") is not None:
            parse_error = str((resp or {}).get("parsing_error"))

    response_text = raw_text or _serialize_payload(parsed_output)

    if parsed_output is None and response_text:
        try:
            parsed_output = parse_analysis_response(response_text).model_dump()
            parse_error = ""
        except Exception:
            pass

    return parsed_output, response_text, parse_error, fallback_mode, usage


def _force_answer_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Bạn đã có tool_observations không rỗng.\n"
        "- Mọi requirement item khả dụng của round này đã được xử lý hoặc không còn lượt tool.\n"
        "- KHÔNG được trả kind=\"action\" nữa.\n"
        "- PHẢI trả kind=\"answer\" ngay từ toàn bộ tool_observations hiện có.\n"
        "- Nếu chưa trích được số liệu mới, vẫn trả kind=\"answer\" với facts=[].\n"
    )


def _force_action_instruction(base_instruction: str, requirement: str = "") -> str:
    requirement_line = ""
    if str(requirement or "").strip():
        requirement_line = f"- Requirement item cần xử lý ngay: {str(requirement).strip()}\n"
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Current round chưa có tool_observations không rỗng nhưng vẫn còn requirement chưa xử lý.\n"
        "- kind=\"answer\" với facts=[] trong tình huống này là không hợp lệ.\n"
        "- PHẢI trả kind=\"action\" ngay để gọi tool cho requirement kế tiếp.\n"
        f"{requirement_line}"
        "- Không được trả kind=\"answer\" trước khi truy vấn ít nhất một requirement item khả dụng của round này.\n"
    )


def _default_action_name(agent_name: str) -> str:
    if str(agent_name or "").strip() == "agent_web":
        return "web_search"
    return "get_related_info"


def _synthetic_action_output(agent_name: str, query: str) -> dict:
    return {
        "kind": "action",
        "action": _default_action_name(agent_name),
        "arguments": {
            "query": str(query or "").strip(),
        },
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


def _analysis_input_results(state: dict) -> dict:
    explicit = state.get("analysis_input_results")
    if isinstance(explicit, dict) and explicit:
        return explicit

    results = {}
    for agent_name, payload in (state.get("worker_results", {}) or {}).items():
        if is_analysis_agent(agent_name):
            continue
        results[agent_name] = payload
    return results


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
        "tools_list": get_tools_list(agent_name),
    }

    trace = []
    parsed_output, response_text, parse_error, fallback_mode, usage = _run_worker_once(payload)
    parsed_output = _normalize_answer_output(agent_name, parsed_output)
    next_requirement = _next_requirement_item(state, agent_name)
    parsed_output, pinned_query = _normalize_action_output(
        agent_name,
        parsed_output,
        requirement=next_requirement,
    )
    if pinned_query:
        debug_log = make_debug_log(
            state,
            "agent:action_query_pinned_to_requirement",
            agent_name=agent_name,
            **pinned_query,
        )
        if debug_log:
            trace.append(debug_log)
    if _parsed_kind(parsed_output) == "action":
        response_text = _serialize_payload(parsed_output)
    if _parsed_kind(parsed_output) == "answer":
        response_text = _serialize_payload(parsed_output)
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
        _parsed_kind(parsed_output) == "answer"
        and _answer_facts_n(parsed_output) == 0
        and not _has_nonempty_tool_context(state, agent_name)
        and bool(_assigned_requirements_for_agent(state, agent_name))
    ):
        retry_payload = dict(payload)
        retry_payload["last_agent_response"] = response_text
        retry_payload["system_instruction"] = _force_action_instruction(
            profile["system_instruction"],
            next_requirement,
        )
        debug_log = make_debug_log(
            state,
            "agent:empty_answer_invalid_retry_action",
            agent_name=agent_name,
            next_requirement=next_requirement,
        )
        if debug_log:
            trace.append(debug_log)

        retry_parsed_output, retry_response_text, retry_parse_error, retry_fallback_mode, retry_usage = _run_worker_once(retry_payload)
        retry_parsed_output = _normalize_answer_output(agent_name, retry_parsed_output)
        retry_parsed_output, retry_pinned_query = _normalize_action_output(
            agent_name,
            retry_parsed_output,
            requirement=next_requirement,
        )
        if retry_pinned_query:
            debug_log = make_debug_log(
                state,
                "agent:action_query_pinned_to_requirement",
                agent_name=agent_name,
                **retry_pinned_query,
            )
            if debug_log:
                trace.append(debug_log)
        if _parsed_kind(retry_parsed_output) == "action":
            retry_response_text = _serialize_payload(retry_parsed_output)
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

        parsed_output = retry_parsed_output
        response_text = retry_response_text
        parse_error = retry_parse_error or parse_error

        if _parsed_kind(parsed_output) != "action" and next_requirement:
            parsed_output = _synthetic_action_output(agent_name, next_requirement)
            response_text = _serialize_payload(parsed_output)
            synthetic_log = make_debug_log(
                state,
                "agent:empty_answer_rewritten_to_action",
                agent_name=agent_name,
                query=next_requirement,
            )
            if synthetic_log:
                trace.append(synthetic_log)

    if (
        _parsed_kind(parsed_output) == "answer"
        and _has_pending_requirement_items(state, agent_name)
        and _tool_call_count_for_round(state, agent_name)
        < len(_assigned_requirements_for_agent(state, agent_name))
    ):
        next_requirement = _next_requirement_item(state, agent_name)
        if next_requirement:
            parsed_output = _synthetic_action_output(agent_name, next_requirement)
            response_text = _serialize_payload(parsed_output)
            pending_log = make_debug_log(
                state,
                "agent:premature_answer_rewritten_to_action",
                agent_name=agent_name,
                query=next_requirement,
                processed_requirements_n=_processed_requirement_count(state, agent_name),
                assigned_requirements_n=len(_assigned_requirements_for_agent(state, agent_name)),
            )
            if pending_log:
                trace.append(pending_log)

    if (
        _parsed_kind(parsed_output) == "action"
        and _has_nonempty_tool_context(state, agent_name)
        and not _has_pending_requirement_items(state, agent_name)
    ):
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

        parsed_output = retry_parsed_output
        response_text = retry_response_text
        parse_error = retry_parse_error or parse_error

        if _parsed_kind(parsed_output) != "answer":
            unresolved_log = make_debug_log(
                state,
                "agent:force_answer_retry_unresolved",
                agent_name=agent_name,
                result_kind=_parsed_kind(parsed_output) or "unknown",
            )
            if unresolved_log:
                trace.append(unresolved_log)
    elif (
        _parsed_kind(parsed_output) == "action"
        and _has_nonempty_tool_context(state, agent_name)
        and _has_pending_requirement_items(state, agent_name)
    ):
        debug_log = make_debug_log(
            state,
            "agent:continue_for_pending_requirements",
            agent_name=agent_name,
            fulfilled_requirements_n=_nonempty_tool_context_count(state, agent_name),
            assigned_requirements_n=len(_assigned_requirements_for_agent(state, agent_name)),
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
            error_event = "agent:recovered_after_error"
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


def call_analysis_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    started_at = time.perf_counter()

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": state.get("worker_query", ""),
        "plan_json": json.dumps(state.get("worker_plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(_analysis_input_results(state), ensure_ascii=False),
        "allowed_keywords_json": "{}",
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list(agent_name),
    }

    parsed_output, response_text, parse_error, fallback_mode, usage = _run_analysis_once(payload)
    parsed_output = _normalize_analysis_output(parsed_output)
    response_text = _serialize_payload(parsed_output)
    trace = []

    if fallback_mode:
        fallback_log = make_debug_log(
            state,
            "analysis:structured_output_fallback",
            agent_name=agent_name,
            mode=fallback_mode,
        )
        if fallback_log:
            trace.append(fallback_log)

    if parse_error:
        trace.append(
            make_log(
                state,
                "analysis:error",
                agent=agent_name,
                error=parse_error[:250],
                empty_response=(not str(response_text or "").strip()),
            )
        )

    trace.append(
        make_log(
            state,
            "analysis:done",
            agent=agent_name,
            requirements_n=len(parsed_output.get("requirements", []) or []),
            answer_len=len(str(parsed_output.get("answer", "") or "")),
            result=parsed_output,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            **usage,
        )
    )

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
