import json
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from graph.logger import make_debug_log
from schemas.agent_outputs import (
    WORKER_RESPONSE_JSON_SCHEMA,
    parse_worker_response,
    parse_worker_response_payload,
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

    parsed_output = None
    parse_error = ""
    raw_text = ""

    try:
        resp = _invoke_worker_chain(payload)
    except Exception as e:
        raw_text = ""
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

    log_entry = make_debug_log(
        state,
        "agent:done",
        agent_name=agent_name,
        is_worker=True,
        response_preview=response_text[:160],
    )

    if parsed_output is None and response_text:
        try:
            parsed_output = parse_worker_response(response_text).model_dump()
            parse_error = ""
        except Exception:
            pass

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
        "trace": [log_entry] if log_entry else []
    }
