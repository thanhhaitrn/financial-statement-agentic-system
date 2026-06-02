"""Run worker and analysis agents, normalize their outputs, and log execution."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

import json
import time

from agents.agent_tools_list import get_tools_for_bind, get_tools_list
from agents.agent_registry import is_analysis_agent
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import build_allowed_keywords_payload
from llm.client import llm
from llm.invoke import extract_usage_metadata, invoke_prompt
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import (
    AnalysisOutput,
    parse_analysis_response,
    parse_analysis_response_payload,
)
from schemas.requirements import (
    FACT_STATUS_NOT_FOUND,
    extract_financial_statement_keywords,
    normalize_fact_status,
    normalize_requirement_text,
    requirement_name_matches_fact,
    requirement_matches_fact,
)
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from tools.tool_calls import invalid_tool_calls, response_tool_calls, synthetic_tool_call
from tools.evidence import merge_worker_fact_payload, scoped_tool_name_for_query, scoped_tool_name_for_table


DEFAULT_MAX_ANALYSIS_TOOL_CALLS_PER_ROUND = 2
ANALYSIS_ALLOWED_KEYWORD_TABLES = {
    "agent_profitability": {TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_liquidity_solvency": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_cashflow_analysis": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_efficiency": {TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION},
}


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


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


def _force_json_analysis_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema AnalysisOutput.\n'
        '- Khong boc JSON bang markdown/code fence; field "answer" duoc phep dung Markdown tieng Viet theo profile.\n'
        '- Khong them van ban ngoai JSON.\n'
        '- Bat buoc co 2 field: \"answer\" va \"requirements\".\n'
    )


def _plain_analysis_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_analysis_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


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
        if (
            stripped.startswith("[get_balance_sheet_info")
            or stripped.startswith("[get_income_statement_info")
            or stripped.startswith("[get_cashflow_info")
            or stripped.startswith("[get_note_info")
            or stripped.startswith("[get_report_section_info")
        ):
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
        if (
            stripped.startswith("[get_balance_sheet_info")
            or stripped.startswith("[get_income_statement_info")
            or stripped.startswith("[get_cashflow_info")
            or stripped.startswith("[get_note_info")
            or stripped.startswith("[get_report_section_info")
        ):
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


def _is_forced_collect(state: dict, agent_name: str) -> bool:
    current_round = int((state or {}).get("followup_rounds", 0) or 0)
    value = (state.get("force_collect_agents", {}) or {}).get(agent_name)
    try:
        return int(value) == current_round
    except (TypeError, ValueError):
        return False


def _assigned_requirements_for_agent(state: dict, agent_name: str) -> list[str]:
    def evidence_queries_to_requirements(target: dict) -> list[str]:
        return [
            str(item.get("query", "") or "").strip()
            for item in (target.get("evidence_queries", []) or [])
            if isinstance(item, dict) and str(item.get("query", "") or "").strip()
        ]

    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict) and str(dispatch_target.get("agent", "")).strip() == agent_name:
        return _dedupe_keep_order(
            [
                str(item).strip()
                for item in (dispatch_target.get("requirements", []) or [])
                if str(item).strip()
            ]
            + evidence_queries_to_requirements(dispatch_target)
        )

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
        requirements.extend(evidence_queries_to_requirements(target))

    for target in (worker_plan.get("analysis_plan", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(
            [
                str(item).strip()
                for item in (target.get("requirements", []) or [])
                if str(item).strip()
            ]
        )
        requirements.extend(evidence_queries_to_requirements(target))

    seen = set()
    normalized = []
    for item in requirements:
        if item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _max_tool_calls_for_agent(state: dict, agent_name: str) -> int:
    requirements_n = len(_assigned_requirements_for_agent(state, agent_name))
    if requirements_n > 0:
        return min(requirements_n, DEFAULT_MAX_ANALYSIS_TOOL_CALLS_PER_ROUND)
    return DEFAULT_MAX_ANALYSIS_TOOL_CALLS_PER_ROUND


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


def _dispatch_target_for_agent(state: dict, agent_name: str) -> dict:
    agent = str(agent_name or "").strip()
    dispatch_target = state.get("dispatch_target")
    if (
        isinstance(dispatch_target, dict)
        and str(dispatch_target.get("agent", "") or "").strip() == agent
    ):
        return dispatch_target

    current_round = int((state or {}).get("followup_rounds", 0) or 0)
    for item in reversed(state.get("worker_messages", []) or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("agent", "") or "").strip() != agent:
            continue
        if str(item.get("kind", "") or "").strip() != "agent_response":
            continue
        try:
            if int(item.get("round", 0) or 0) != current_round:
                continue
        except (TypeError, ValueError):
            continue
        message_target = item.get("dispatch_target")
        if (
            isinstance(message_target, dict)
            and str(message_target.get("agent", "") or "").strip() == agent
        ):
            return message_target

    return {}


def _scoped_analysis_input_results(state: dict, agent_name: str) -> dict:
    dispatch_target = _dispatch_target_for_agent(state, agent_name)
    target_results = (
        dispatch_target.get("analysis_input_results")
        if isinstance(dispatch_target, dict)
        else None
    )
    if isinstance(target_results, dict) and target_results:
        return target_results

    explicit = state.get("analysis_input_results")
    if isinstance(explicit, dict) and explicit:
        return explicit

    return {}


def _evidence_pack_payload_for_prompt(state: dict, agent_name: str = "") -> dict:
    pack = state.get("evidence_pack", {}) or {}
    if not isinstance(pack, dict):
        return {}
    items = []
    for item in pack.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        payload = {}
        for key in (
            "scope",
            "table",
            "query",
            "note_ref",
            "source_table",
            "source_item",
            "facts_n",
            "cache_hit",
        ):
            value = item.get(key)
            if value in ("", None, [], {}):
                continue
            payload[key] = value
        if payload:
            items.append(payload)

    payload = {
        "items": items,
        "stats": pack.get("stats", {}) or {},
    }
    if not _scoped_analysis_input_results(state, agent_name):
        payload["facts_by_table"] = _compact_analysis_results_for_prompt(
            pack.get("facts_by_table", {}) or pack.get("facts_by_agent", {}) or {}
        )
    return payload


def _compact_analysis_fact_for_prompt(fact: dict) -> dict:
    if not isinstance(fact, dict):
        return {}

    payload = {}
    for key in (
        "content_type",
        "item_name",
        "time_hint",
        "value",
        "interpretation_hint",
        "note_ref",
        "note_number",
        "note_title",
        "subheading",
    ):
        value = fact.get(key)
        if value in ("", None, [], {}):
            continue
        payload[key] = value

    status = normalize_fact_status(fact.get("status"))
    if status and status != "found":
        payload["status"] = status

    return payload


def _compact_analysis_results_for_prompt(results: dict) -> dict:
    output = {}
    for result_key, payload in (results or {}).items():
        if not isinstance(payload, dict):
            continue
        table = normalize_table_heading(str(payload.get("table", "") or result_key).strip())
        facts = [
            compact
            for compact in (
                _compact_analysis_fact_for_prompt(fact)
                for fact in (payload.get("facts", []) or [])
            )
            if compact
        ]
        item = {}
        if table:
            item["table"] = table
        if facts:
            item["facts"] = facts
        if item:
            output[str(result_key)] = item
    return output


def _analysis_input_results_payload_for_prompt(state: dict, agent_name: str = "") -> dict:
    return _compact_analysis_results_for_prompt(
        _analysis_input_results_with_tool_facts(state, agent_name)
    )


def _compact_evidence_queries_for_prompt(items: list[dict]) -> list[dict]:
    output = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue
        key = (table, query)
        if key in seen:
            continue
        seen.add(key)
        payload = {"query": query}
        if table:
            payload["table"] = table
        output.append(payload)
    return output


def _analysis_plan_payload_for_prompt(state: dict, agent_name: str) -> dict:
    worker_plan = state.get("worker_plan", {}) or {}
    if not isinstance(worker_plan, dict):
        worker_plan = {}

    dispatch_target = state.get("dispatch_target")
    if not isinstance(dispatch_target, dict):
        dispatch_target = {}

    objective = str(dispatch_target.get("objective", "") or "").strip()
    if not objective:
        for item in worker_plan.get("analysis_plan", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("agent", "") or "").strip() != agent_name:
                continue
            objective = str(item.get("objective", "") or "").strip()
            break

    evidence_queries = _compact_evidence_queries_for_prompt(
        list(dispatch_target.get("evidence_queries", []) or [])
        or list(state.get("evidence_queries", []) or [])
    )
    requirements = _dedupe_keep_order(
        str(item).strip()
        for item in (dispatch_target.get("requirements", []) or [])
        if str(item).strip()
    )

    plan_item = {
        "agent": agent_name,
        "objective": objective,
        "evidence_queries": evidence_queries,
        "requirements": requirements,
    }
    compact_plan = {
        "analysis_plan": [
            {
                key: value
                for key, value in plan_item.items()
                if value not in ("", None, [], {})
            }
        ]
    }
    difficulty_level = str(worker_plan.get("difficulty_level", "") or "").strip()
    if difficulty_level:
        compact_plan["difficulty_level"] = difficulty_level
    return compact_plan


def _evidence_pack_facts(state: dict) -> list[dict]:
    pack = state.get("evidence_pack", {}) or {}
    if not isinstance(pack, dict):
        return []

    facts: list[dict] = []
    for section_name in ("facts_by_table", "facts_by_agent"):
        section = pack.get(section_name, {}) or {}
        if not isinstance(section, dict):
            continue
        for payload in section.values():
            if not isinstance(payload, dict):
                continue
            table = str(payload.get("table", "") or "").strip()
            for fact in payload.get("facts", []) or []:
                if not isinstance(fact, dict):
                    continue
                fact_payload = dict(fact)
                if table and not str(fact_payload.get("table", "") or "").strip():
                    fact_payload["table"] = table
                facts.append(fact_payload)

    for item in pack.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table", "") or "").strip()
        for fact in item.get("facts_preview", []) or []:
            if not isinstance(fact, dict):
                continue
            fact_payload = dict(fact)
            if table and not str(fact_payload.get("table", "") or "").strip():
                fact_payload["table"] = table
            facts.append(fact_payload)

    return facts


def _dedupe_fact_payloads(facts: list[dict]) -> list[dict]:
    output = []
    seen = set()
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        key = (
            str(fact.get("table", "") or "").strip(),
            str(fact.get("item_name", "") or "").strip(),
            str(fact.get("time_hint", "") or "").strip(),
            str(fact.get("value", "") or "").strip(),
            str(fact.get("source", "") or "").strip(),
            str(fact.get("status", "") or "").strip(),
        )
        if key in seen:
            continue
        output.append(fact)
        seen.add(key)
    return output


def _analysis_evidence_facts(state: dict, agent_name: str) -> list[dict]:
    facts = list(_analysis_input_facts(state, agent_name))
    if not _scoped_analysis_input_results(state, agent_name):
        facts.extend(_evidence_pack_facts(state))
    facts.extend(_tool_result_facts_for_agent(state, agent_name))
    return _dedupe_fact_payloads(facts)


def _tool_result_facts_for_agent(state: dict, agent_name: str) -> list[dict]:
    facts = []
    current_round = int((state or {}).get("followup_rounds", 0) or 0)

    for item in state.get("tool_results", []) or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("agent", "") or "").strip() != agent_name:
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

        results = item.get("results", {}) or {}
        if not isinstance(results, dict):
            continue
        args = item.get("args", {}) or {}
        if not isinstance(args, dict):
            args = {}
        table = normalize_table_heading(
            str(
                results.get("table", "")
                or args.get("table", "")
                or ""
            ).strip()
        )
        for fact in results.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            fact_payload = dict(fact)
            if table and not str(fact_payload.get("table", "") or "").strip():
                fact_payload["table"] = table
            facts.append(fact_payload)

    return facts


def _requirement_satisfied_by_evidence(
    state: dict,
    agent_name: str,
    requirement: str,
    facts: list[dict],
) -> bool:
    requirement_text = str(requirement or "").strip()
    if not requirement_text:
        return True

    expected_table = normalize_table_heading(_table_for_requirement(state, requirement_text))
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        fact_table = normalize_table_heading(str(fact.get("table", "") or "").strip())
        if expected_table and fact_table and fact_table != expected_table:
            continue
        if requirement_matches_fact(requirement_text, fact, table=expected_table or fact_table):
            return True
        if (
            normalize_fact_status(fact.get("status")) == FACT_STATUS_NOT_FOUND
            and requirement_name_matches_fact(requirement_text, fact, table=expected_table or fact_table)
        ):
            return True
    return False


def _missing_requirements_after_evidence_check(state: dict, agent_name: str) -> list[str]:
    requirements = _assigned_requirements_for_agent(state, agent_name)
    if not requirements:
        return []

    facts = _analysis_evidence_facts(state, agent_name)
    return [
        requirement
        for requirement in requirements
        if not _requirement_satisfied_by_evidence(state, agent_name, requirement, facts)
    ]


def _attempted_tool_queries_for_agent(state: dict, agent_name: str) -> list[tuple[str, str]]:
    attempted = []
    current_round = int((state or {}).get("followup_rounds", 0) or 0)

    for item in state.get("tool_results", []) or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("agent", "") or "").strip() != agent_name:
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

        args = item.get("args", {}) or {}
        if not isinstance(args, dict):
            continue
        query = str(args.get("query", "") or "").strip()
        table = normalize_table_heading(str(args.get("table", "") or "").strip())
        if query:
            attempted.append((table, query))

    return attempted


def _requirement_attempted_by_tool(state: dict, agent_name: str, requirement: str) -> bool:
    requirement_text = str(requirement or "").strip()
    if not requirement_text:
        return True

    expected_table = normalize_table_heading(_table_for_requirement(state, requirement_text))
    normalized_requirement = normalize_requirement_text(requirement_text, table=expected_table)
    for table, query in _attempted_tool_queries_for_agent(state, agent_name):
        query_table = normalize_table_heading(table)
        if expected_table and query_table and query_table != expected_table:
            continue
        normalized_query = normalize_requirement_text(query, table=expected_table or query_table)
        if normalized_requirement and normalized_query and (
            normalized_requirement == normalized_query
            or normalized_requirement in normalized_query
            or normalized_query in normalized_requirement
        ):
            return True

    return False


def _processed_missing_requirement_count(state: dict, agent_name: str, missing_requirements: list[str]) -> int:
    if not missing_requirements:
        return 0
    for index, requirement in enumerate(missing_requirements):
        if not _requirement_attempted_by_tool(state, agent_name, requirement):
            return index
    return len(missing_requirements)


def _next_missing_requirement_item(state: dict, agent_name: str, missing_requirements: list[str]) -> str:
    if not missing_requirements:
        return ""
    current_index = _processed_missing_requirement_count(state, agent_name, missing_requirements)
    if 0 <= current_index < len(missing_requirements):
        return str(missing_requirements[current_index] or "").strip()
    return ""


def _parsed_kind(parsed_output) -> str:
    if not isinstance(parsed_output, dict):
        return ""
    return str(parsed_output.get("kind", "")).strip().lower()


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


def _tool_call_payload(tool_calls: list[dict]) -> dict:
    return {
        "kind": "tool_calls",
        "tool_calls": tool_calls,
    }


def _run_analysis_tool_call_once(payload: dict, agent_name: str):
    parsed_output = None
    parse_error = ""
    usage = {}
    response_text = ""

    tools = get_tools_for_bind(agent_name)
    if not tools:
        return None, "", f"No bound tools configured for {agent_name}.", "", usage

    try:
        resolved_prompt_template = PROMPT_TEMPLATE(payload)
        chain = resolved_prompt_template | llm.bind_tools(tools)
        raw = chain.invoke(payload)
        usage = extract_usage_metadata(raw)
        tool_calls = response_tool_calls(raw)
        invalid_calls = invalid_tool_calls(raw)
        if invalid_calls:
            parse_error = f"Invalid native tool calls: {invalid_calls[:2]}"

        if tool_calls:
            parsed_output = _tool_call_payload(tool_calls)
            response_text = _serialize_payload(parsed_output)
        else:
            response_text = extract_text(raw)
            if response_text:
                try:
                    parsed_output = parse_analysis_response(response_text).model_dump()
                    parse_error = ""
                except Exception as exc:
                    parse_error = str(exc)
    except Exception as exc:
        parse_error = str(exc)

    return parsed_output, response_text, parse_error, "native_tool_call", usage


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


def _force_analysis_answer_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Không còn requirement phân tích khả dụng cần gọi tool trong round này, hoặc không còn lượt tool.\n"
        "- KHÔNG được gọi tool nữa.\n"
        "- PHẢI trả đúng schema AnalysisOutput ngay từ worker_results_json và tool_observations hiện có.\n"
        "- Nếu còn thiếu dữ liệu, nêu rõ trong answer và điền requirements bằng các dữ liệu cần truy xuất thêm.\n"
    )


def _force_analysis_tool_call_instruction(base_instruction: str, requirement: str = "") -> str:
    requirement_line = ""
    if str(requirement or "").strip():
        requirement_line = f"- Keyword cần kiểm tra/truy xuất ngay: {str(requirement).strip()}\n"
    return (
        f"{base_instruction}\n\n"
        "BẮT BUỘC BỔ SUNG:\n"
        "- Trước hết phải đọc input_facts trong worker_results_json; evidence_pack_json chỉ là metadata truy xuất/tóm tắt.\n"
        "- Nếu worker_results_json đã có fact phù hợp cho keyword cần kiểm tra, trả AnalysisOutput trực tiếp và requirements=[].\n"
        "- Chỉ khi input_facts thiếu, mơ hồ hoặc có status not_found_after_search cho dữ liệu cần thiết, hãy gọi đúng bound tool theo phạm vi bảng; không viết JSON action thủ công.\n"
        f"{requirement_line}"
        "- Objective phân tích nằm trong plan_json.analysis_plan[].objective; KHÔNG dùng objective dài làm query.\n"
        "- Query tool phải lấy từ evidence_queries được giao hoặc keyword phù hợp trong allowed_keywords_json theo đúng table tương ứng.\n"
        "- Dùng get_balance_sheet_info cho bảng cân đối kế toán, get_income_statement_info cho báo cáo kết quả kinh doanh, get_cashflow_info cho lưu chuyển tiền tệ, get_note_info cho thuyết minh, get_report_section_info cho phần đầu báo cáo như báo cáo Ban Tổng Giám đốc/kiểm toán/soát xét.\n"
        "- Evidence ban đầu chỉ gửi tối đa 2 facts cho mỗi phần thuyết minh; nếu cần thêm dòng/chi tiết trong đúng phần thuyết minh đó, dùng get_note_info với query là số thuyết minh, tiêu đề note, hoặc chủ đề note ngắn.\n"
        "- query của tool PHẢI là 1 khoản mục/line-item báo cáo tài chính ngắn bằng tiếng Việt, ví dụ: \"lợi nhuận sau thuế thu nhập doanh nghiệp\", \"tổng cộng tài sản\", \"lưu chuyển tiền thuần từ hoạt động kinh doanh\"; riêng get_note_info dùng chủ đề/số thuyết minh ngắn, get_report_section_info dùng chủ đề phần đầu báo cáo ngắn.\n"
        "- Không dùng objective phân tích dài làm query, không dùng câu như \"đánh giá khả năng sinh lời\" làm query, và không ghép nhiều khoản mục vào cùng một query.\n"
        "- Nếu không gọi tool nhưng vẫn thiếu dữ liệu, requirements phải dùng cùng kiểu khoản mục/line-item hoặc chủ đề thuyết minh ngắn để hệ thống có thể truy xuất tiếp.\n"
    )


def _analysis_input_results(state: dict, agent_name: str = "") -> dict:
    scoped = _scoped_analysis_input_results(state, agent_name)
    if scoped:
        return scoped

    explicit = state.get("analysis_input_results")
    if isinstance(explicit, dict):
        return explicit

    results = {}
    for source_agent, payload in (state.get("worker_results", {}) or {}).items():
        if is_analysis_agent(source_agent):
            continue
        results[source_agent] = payload
    return results


def _analysis_input_results_with_tool_facts(state: dict, agent_name: str) -> dict:
    results = {}
    for result_key, payload in (_analysis_input_results(state, agent_name) or {}).items():
        if not isinstance(payload, dict):
            results[result_key] = payload
            continue

        facts = [
            dict(fact)
            for fact in (payload.get("facts", []) or [])
            if isinstance(fact, dict)
        ]
        results[result_key] = {
            "table": str(payload.get("table", "") or "").strip(),
            "facts": facts,
        }

    for fact in _tool_result_facts_for_agent(state, agent_name):
        if not isinstance(fact, dict):
            continue
        table = normalize_table_heading(str(fact.get("table", "") or "").strip())
        result_key = table or "TOOL"
        results[result_key] = merge_worker_fact_payload(
            results.get(result_key, {}),
            {
                "table": table,
                "facts": [fact],
            },
        )

    return results


def _analysis_input_facts(state: dict, agent_name: str) -> list[dict]:
    facts = []
    for _source_agent, payload in (_analysis_input_results(state, agent_name) or {}).items():
        if not isinstance(payload, dict):
            continue
        table = str(payload.get("table", "") or "").strip()
        for fact in payload.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            fact_payload = dict(fact)
            if table and not str(fact_payload.get("table", "") or "").strip():
                fact_payload["table"] = table
            facts.append(fact_payload)
    return facts


def _analysis_output_requirements(parsed_output) -> list[str]:
    if not isinstance(parsed_output, dict):
        return []
    return [
        str(item).strip()
        for item in (parsed_output.get("requirements", []) or [])
        if str(item).strip()
    ]


def _looks_like_statement_line_item(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    blocked_terms = (
        "đánh giá",
        "phân tích",
        "khả năng",
        "hiệu quả",
        "rủi ro",
        "xu hướng",
        "chất lượng",
        "kết luận",
    )
    if any(term in text for term in blocked_terms):
        return False
    return len(text.split()) <= 12


def _table_for_requirement(state: dict, requirement: str) -> str:
    requirement_text = str(requirement or "").strip()
    if not requirement_text:
        return ""

    candidates = []
    explicit = state.get("evidence_queries")
    if isinstance(explicit, list):
        candidates.extend(explicit)

    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict):
        candidates.extend(dispatch_target.get("evidence_queries", []) or [])

    worker_plan = state.get("worker_plan", {}) or {}
    agent_name = str((dispatch_target or {}).get("agent", "") or "").strip() if isinstance(dispatch_target, dict) else ""
    for item in (worker_plan.get("analysis_plan", []) or []):
        if not isinstance(item, dict):
            continue
        if agent_name and str(item.get("agent", "") or "").strip() != agent_name:
            continue
        candidates.extend(item.get("evidence_queries", []) or [])

    for item in candidates:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table", "") or "").strip()
        query = str(item.get("query", "") or "").strip()
        if not table or not query:
            continue
        normalized_requirement = normalize_requirement_text(requirement_text, table=table)
        normalized_query = normalize_requirement_text(query, table=table)
        if normalized_requirement and normalized_query and normalized_requirement == normalized_query:
            return table
        if normalized_requirement and normalized_query and (
            normalized_requirement in normalized_query or normalized_query in normalized_requirement
        ):
            return table

    return ""


def _statement_query_from_requirement(value: str, *, table: str = "") -> str:
    raw_text = " ".join(str(value or "").strip().split())
    if _looks_like_statement_line_item(raw_text):
        return raw_text

    keywords = extract_financial_statement_keywords(value, table=table, limit=1)
    if keywords:
        return keywords[0]

    normalized = normalize_requirement_text(value, table=table)
    if _looks_like_statement_line_item(normalized):
        return normalized
    return ""


def _analysis_report_query_from_output(state: dict, parsed_output, fallback_requirement: str = "") -> tuple[str, str]:
    for requirement in _analysis_output_requirements(parsed_output):
        table = _table_for_requirement(state, requirement)
        query = _statement_query_from_requirement(requirement, table=table)
        if query:
            return query, table

    table = _table_for_requirement(state, fallback_requirement)
    query = _statement_query_from_requirement(fallback_requirement, table=table)
    return query, table


def _analysis_allowed_keyword_tables(state: dict, agent_name: str, requirement: str = "") -> list[str]:
    tables = set()
    for table in ANALYSIS_ALLOWED_KEYWORD_TABLES.get(agent_name, set()):
        if table != TABLE_NOTE:
            tables.add(table)

    for candidate in (requirement,):
        table = normalize_table_heading(_table_for_requirement(state, candidate))
        if table and table != TABLE_NOTE:
            tables.add(table)

    for item in (state.get("evidence_queries", []) or []):
        if isinstance(item, dict):
            table = normalize_table_heading(str(item.get("table", "") or "").strip())
            if table and table != TABLE_NOTE:
                tables.add(table)

    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict):
        for item in dispatch_target.get("evidence_queries", []) or []:
            if not isinstance(item, dict):
                continue
            table = normalize_table_heading(str(item.get("table", "") or "").strip())
            if table and table != TABLE_NOTE:
                tables.add(table)

    return [table for table in (TABLE_BS, TABLE_IS, TABLE_CF, TABLE_REPORT_SECTION) if table in tables]


def _allowed_keywords_payload_for_analysis(state: dict, agent_name: str, requirement: str = "") -> str:
    selected_tables = _analysis_allowed_keyword_tables(state, agent_name, requirement=requirement)
    return build_allowed_keywords_payload(selected_tables=selected_tables or None)


def _deterministic_tool_call_for_missing_requirement(
    state: dict,
    agent_name: str,
    requirement: str,
) -> dict:
    table = _table_for_requirement(state, requirement)
    raw_query = " ".join(str(requirement or "").strip().split())
    normalized_table = normalize_table_heading(table)
    if normalized_table in {TABLE_NOTE, TABLE_REPORT_SECTION} and raw_query:
        return _tool_call_payload(
            [
                synthetic_tool_call(
                    scoped_tool_name_for_table(normalized_table),
                    {"query": raw_query},
                )
            ]
        )

    query = _statement_query_from_requirement(requirement, table=table)
    if not query:
        query = raw_query
    if not query or not _looks_like_statement_line_item(query):
        return {}

    tool_name = scoped_tool_name_for_table(table) or scoped_tool_name_for_query(query, agent_name=agent_name)
    if not tool_name:
        return {}

    return _tool_call_payload(
        [
            synthetic_tool_call(
                tool_name,
                {"query": query},
            )
        ]
    )


def _analysis_output_requests_more_data(parsed_output) -> bool:
    return bool(_analysis_output_requirements(parsed_output))


def _analysis_fallback_output(state: dict, agent_name: str, parsed_output) -> dict:
    normalized = _normalize_analysis_output(parsed_output)
    if str(normalized.get("answer", "") or "").strip():
        return normalized

    requirements = _analysis_output_requirements(normalized)
    if requirements:
        return {
            "answer": "Chưa đủ dữ liệu để kết luận. Cần bổ sung: " + "; ".join(requirements) + ".",
            "requirements": requirements,
        }

    assigned = _assigned_requirements_for_agent(state, agent_name)
    if assigned:
        objective = "; ".join(assigned[:2])
        return {
            "answer": (
                "Chưa tạo được kết luận phân tích hợp lệ từ dữ liệu hiện có cho mục tiêu: "
                f"{objective}. Cần kiểm tra lại dữ liệu đầu vào hoặc truy xuất thêm bằng chứng liên quan."
            ),
            "requirements": [],
        }

    if _analysis_input_facts(state, agent_name):
        return {
            "answer": "Đã có dữ liệu đầu vào nhưng chưa tạo được kết luận phân tích hợp lệ.",
            "requirements": [],
        }

    return {
        "answer": "Chưa đủ dữ liệu để kết luận phân tích.",
        "requirements": [],
    }


def call_analysis_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    started_at = time.perf_counter()

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(_analysis_plan_payload_for_prompt(state, agent_name), ensure_ascii=False),
        "evidence_pack_json": json.dumps(_evidence_pack_payload_for_prompt(state, agent_name), ensure_ascii=False),
        "worker_results_json": json.dumps(_analysis_input_results_payload_for_prompt(state, agent_name), ensure_ascii=False),
        "allowed_keywords_json": _allowed_keywords_payload_for_analysis(state, agent_name),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": _tool_obs_for_agent(state, agent_name),
        "tools_list": get_tools_list(agent_name),
    }

    trace = []
    missing_requirements = _missing_requirements_after_evidence_check(state, agent_name)
    next_requirement = _next_missing_requirement_item(state, agent_name, missing_requirements)
    next_requirement_index = _processed_missing_requirement_count(state, agent_name, missing_requirements)
    payload["allowed_keywords_json"] = _allowed_keywords_payload_for_analysis(
        state,
        agent_name,
        requirement=next_requirement,
    )
    assigned_requirements_n = len(_assigned_requirements_for_agent(state, agent_name))
    tool_count = _tool_call_count_for_round(state, agent_name)
    max_tool_calls = _max_tool_calls_for_agent(state, agent_name)
    evidence_facts_n = len(_analysis_evidence_facts(state, agent_name))
    forced_collect = _is_forced_collect(state, agent_name)
    should_call_tool = (
        bool(get_tools_for_bind(agent_name))
        and bool(missing_requirements)
        and next_requirement_index < len(missing_requirements)
        and tool_count < max_tool_calls
        and not forced_collect
    )

    trace.append(
        make_log(
            state,
            "analysis:evidence_check",
            agent=agent_name,
            assigned_requirements_n=assigned_requirements_n,
            missing_requirements_n=len(missing_requirements),
            missing_requirements=missing_requirements[:4],
            evidence_facts_n=evidence_facts_n,
            tool_count=tool_count,
            max_tool_calls=max_tool_calls,
            forced_collect=forced_collect,
            will_call_tool=should_call_tool,
        )
    )

    if should_call_tool:
        deterministic_tool_call = _deterministic_tool_call_for_missing_requirement(
            state,
            agent_name,
            next_requirement,
        )
        if deterministic_tool_call:
            parsed_output = deterministic_tool_call
            response_text = _serialize_payload(parsed_output)
            parse_error = ""
            fallback_mode = "deterministic_tool_call"
            usage = {}
            calls = parsed_output.get("tool_calls", []) if isinstance(parsed_output, dict) else []
            call = calls[0] if calls and isinstance(calls[0], dict) else {}
            trace.append(
                make_log(
                    state,
                    "analysis:deterministic_tool_call",
                    agent=agent_name,
                    requirement=next_requirement,
                    tool=str(call.get("name", "") or "").strip(),
                    query=str((call.get("args", {}) or {}).get("query", "") or "").strip(),
                )
            )
        else:
            payload["system_instruction"] = _force_analysis_tool_call_instruction(
                profile["system_instruction"],
                next_requirement,
            )
            parsed_output, response_text, parse_error, fallback_mode, usage = _run_analysis_tool_call_once(
                payload,
                agent_name,
            )
        if (
            _parsed_kind(parsed_output) != "tool_calls"
            and _analysis_output_requests_more_data(parsed_output)
        ):
            tool_query, tool_table = _analysis_report_query_from_output(state, parsed_output, next_requirement)
            if tool_query:
                tool_name = scoped_tool_name_for_table(tool_table) or scoped_tool_name_for_query(tool_query, agent_name=agent_name)
                tool_call = synthetic_tool_call(
                    tool_name,
                    {"query": tool_query},
                )
                parsed_output = _tool_call_payload([tool_call])
                response_text = _serialize_payload(parsed_output)
                synthetic_log = make_debug_log(
                    state,
                    "analysis:native_tool_call_synthesized",
                    agent_name=agent_name,
                    tool=tool_call["name"],
                    query=tool_query,
                    table=tool_table,
                    source="analysis_requirements",
                    reason=parse_error[:160] if parse_error else "model_returned_requirements_without_tool_call",
                )
                if synthetic_log:
                    trace.append(synthetic_log)
        elif _parsed_kind(parsed_output) != "tool_calls":
            parsed_output = _analysis_fallback_output(state, agent_name, parsed_output)
            response_text = _serialize_payload(parsed_output)

        if _parsed_kind(parsed_output) == "tool_calls" and _has_nonempty_tool_context(state, agent_name):
            debug_log = make_debug_log(
                state,
                "analysis:continue_for_pending_requirements",
                agent_name=agent_name,
                fulfilled_requirements_n=_nonempty_tool_context_count(state, agent_name),
                assigned_requirements_n=assigned_requirements_n,
                missing_requirements_n=len(missing_requirements),
            )
            if debug_log:
                trace.append(debug_log)
    else:
        if forced_collect or tool_count >= max_tool_calls:
            trace.append(
                make_log(
                    state,
                    "analysis:tool_loop_guard_answer_mode",
                    agent=agent_name,
                    tool_count=tool_count,
                    max_tool_calls=max_tool_calls,
                    forced_collect=forced_collect,
                    missing_requirements_n=len(missing_requirements),
                )
            )
        elif assigned_requirements_n and not missing_requirements:
            trace.append(
                make_log(
                    state,
                    "analysis:evidence_pack_satisfied",
                    agent=agent_name,
                    assigned_requirements_n=assigned_requirements_n,
                    evidence_facts_n=evidence_facts_n,
                )
            )
        payload["system_instruction"] = _force_analysis_answer_instruction(profile["system_instruction"])
        parsed_output, response_text, parse_error, fallback_mode, usage = _run_analysis_once(payload)
        parsed_output = _analysis_fallback_output(state, agent_name, parsed_output)
        response_text = _serialize_payload(parsed_output)

    if fallback_mode and fallback_mode not in {"native_tool_call", "deterministic_tool_call"}:
        fallback_log = make_debug_log(
            state,
            "analysis:structured_output_fallback",
            agent_name=agent_name,
            mode=fallback_mode,
        )
        if fallback_log:
            trace.append(fallback_log)

    parsed_kind_for_error = _parsed_kind(parsed_output)
    if parse_error:
        error_event = "analysis:error"
        error_payload = {
            "agent": agent_name,
            "error": parse_error[:250],
            "empty_response": (not str(response_text or "").strip()),
        }
        if parsed_kind_for_error == "tool_calls" or (
            isinstance(parsed_output, dict)
            and ("answer" in parsed_output or "requirements" in parsed_output)
        ):
            error_event = "analysis:recovered_after_error"
            error_payload["recovered_kind"] = parsed_kind_for_error or "answer"
        trace.append(
            make_log(
                state,
                error_event,
                **error_payload,
            )
        )

    parsed_kind = parsed_kind_for_error
    result_is_tool_call = parsed_kind == "tool_calls"
    if result_is_tool_call:
        calls = parsed_output.get("tool_calls", []) if isinstance(parsed_output, dict) else []
        call = calls[0] if calls and isinstance(calls[0], dict) else {}
        call_args = call.get("args", {}) or {}
        done_summary = {
            "result_kind": "tool_calls",
            "tool": str(call.get("name", "") or "").strip(),
            "query": str(call_args.get("query", "") or "").strip(),
            "tool_calls_n": len(calls),
        }
    else:
        normalized = _analysis_fallback_output(state, agent_name, parsed_output)
        parsed_output = normalized
        response_text = _serialize_payload(parsed_output)
        done_summary = {
            "result_kind": "answer",
            "requirements_n": len(parsed_output.get("requirements", []) or []),
            "answer_len": len(str(parsed_output.get("answer", "") or "")),
            "result": parsed_output,
        }

    trace.append(
        make_log(
            state,
            "analysis:done",
            agent=agent_name,
            **done_summary,
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
                "tool_calls": (parsed_output or {}).get("tool_calls", []) if isinstance(parsed_output, dict) else [],
                "parse_error": parse_error,
                "dispatch_target": state.get("dispatch_target", {}) or {},
                "current_requirement": next_requirement,
                "requirement_index": next_requirement_index,
            }
        ],
        "trace": trace,
    }
