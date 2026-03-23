import json
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import (
    WORKER_RESPONSE_JSON_SCHEMA,
    parse_worker_response,
    parse_worker_response_payload,
)


AGENT_DEFAULT_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "agent_web": "WEB",
}

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


def _invoke_worker_chain(payload: dict):
    chain = PROMPT_TEMPLATE | llm.with_structured_output(
        WORKER_RESPONSE_JSON_SCHEMA,
        include_raw=True,
    )
    return chain.invoke(payload)


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


def _run_worker_once(payload: dict):
    parsed_output = None
    parse_error = ""
    raw_text = ""

    try:
        resp = _invoke_worker_chain(payload)
    except Exception as e:
        parse_error = str(e)
        resp = None
    else:
        raw_text = extract_text((resp or {}).get("raw", ""))

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

    return parsed_output, response_text, parse_error


def _force_answer_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Bạn đã có tool_observations không rỗng.\n"
        "- KHÔNG được trả kind=\"action\" nữa.\n"
        "- PHẢI trả kind=\"answer\" ngay từ toàn bộ tool_observations hiện có.\n"
        "- Nếu chưa trích được số liệu mới, vẫn trả kind=\"answer\" với facts=[] và notes ngắn gọn.\n"
    )


def _coerce_repeat_action_to_answer(state: dict, agent_name: str) -> dict:
    previous = (state.get("worker_results", {}) or {}).get(agent_name, {}) or {}
    table = str(previous.get("table", "")).strip() or AGENT_DEFAULT_TABLE.get(agent_name, "")
    facts = previous.get("facts", []) if isinstance(previous.get("facts"), list) else []
    notes = []

    previous_notes = str(previous.get("notes", "") or "").strip()
    if previous_notes:
        notes.append(previous_notes)
    notes.append("Buoc tra loi duoc ep chot sau khi worker lap lai tool action du da co ket qua tool.")

    return {
        "kind": "answer",
        "table": table,
        "facts": facts,
        "missing": [],
        "notes": " | ".join(notes),
    }


def call_worker_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": state.get("worker_query", ""),
        "plan_json": json.dumps(state.get("worker_plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "allowed_keywords_json": "{}",
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": _tool_obs_for_agent(state, agent_name),
        "tools_list": profile.get("tool_list", ""),
    }

    parsed_output, response_text, parse_error = _run_worker_once(payload)
    trace = []

    if _parsed_kind(parsed_output) == "action" and _has_nonempty_tool_context(state, agent_name):
        retry_payload = dict(payload)
        retry_payload["last_agent_response"] = response_text
        retry_payload["system_instruction"] = _force_answer_instruction(profile["system_instruction"])
        trace.append(
            make_log(
                state,
                "agent:force_answer_retry",
                agent_name=agent_name,
                previous_response=response_text[:160],
            )
        )

        retry_parsed_output, retry_response_text, retry_parse_error = _run_worker_once(retry_payload)
        if _parsed_kind(retry_parsed_output) == "answer":
            parsed_output = retry_parsed_output
            response_text = retry_response_text
            parse_error = retry_parse_error
        else:
            parsed_output = _coerce_repeat_action_to_answer(state, agent_name)
            response_text = _serialize_payload(parsed_output)
            parse_error = retry_parse_error or parse_error
            trace.append(
                make_log(
                    state,
                    "agent:force_answer_fallback",
                    agent_name=agent_name,
                    kept_facts_n=len(parsed_output.get("facts", []) or []),
                )
            )

    log_entry = make_debug_log(
        state,
        "agent:done",
        agent_name=agent_name,
        is_worker=True,
        response_preview=response_text[:160],
    )
    if log_entry:
        trace.append(log_entry)

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
