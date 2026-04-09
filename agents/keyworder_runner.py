import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from agents.planner_hints import infer_metric_priority_keywords, infer_table_keywords
from config.allowed_keywords import build_allowed_keywords_payload
from schemas.agent_outputs import KeywordPlan
from agents.profiles import AGENT_PROFILES
from llm.invoke import extract_usage_metadata, invoke_prompt
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log
from schemas.keyword_guard import repair_keywords, validate_keywords
from schemas.table_names import normalize_table_heading

MAX_SEED_KEYWORDS_PER_TABLE = 2


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _selected_tables(planner_plan: dict) -> list[str]:
    selected = []

    for table in (planner_plan.get("tables", []) or []):
        text = str(table).strip()
        if text:
            selected.append(text)

    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        for table in (axis.get("tables", []) or []):
            text = str(table).strip()
            if text:
                selected.append(text)

    return _dedupe_keep_order(selected)


def _planner_hints_by_table(planner_plan: dict, selected_tables: list[str], user_query: str) -> dict[str, list[str]]:
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    return {
        table: infer_table_keywords(table, user_query, analysis_axes)
        for table in selected_tables
    }


def _priority_hints_by_table(planner_plan: dict, selected_tables: list[str], user_query: str) -> dict[str, list[str]]:
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    return {
        table: infer_metric_priority_keywords(table, user_query, analysis_axes)
        for table in selected_tables
    }


def _allowed_keywords_payload(selected_tables: list[str]) -> str:
    return build_allowed_keywords_payload(selected_tables)


def _limit_seed_keywords(items: list[str], limit: int = MAX_SEED_KEYWORDS_PER_TABLE) -> list[str]:
    if limit <= 0:
        return []
    return _dedupe_keep_order(items)[:limit]


def _keyword_trace_targets(worker_plan: dict) -> list[dict]:
    targets = []
    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        table = str(item.get("table", "") or "").strip()
        keywords = [
            str(keyword).strip()
            for keyword in (item.get("keywords", []) or [])
            if str(keyword).strip()
        ]
        if not table:
            continue
        targets.append(
            {
                "table": table,
                "keywords": keywords[:2],
            }
        )
    return targets


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema KeywordPlan.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- Output phai co dang: {"targets":[...]}.\n'
    )


def _plain_keyworder_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_output_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if content is not None:
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(raw)


def _extract_first_json_object(text: str) -> Optional[str]:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(cleaned)):
        ch = cleaned[idx]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:idx + 1]

    return None


def _try_parse_json_object(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        if isinstance(value.get("targets"), list):
            return value

        content = value.get("content")
        if content is not None:
            return _try_parse_json_object(content)
        return None

    text = _to_text(value).strip()
    if not text:
        return None

    for candidate in (text, _extract_first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def _coerce_target_table(value: Any, selected_tables: list[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in selected_tables:
        return text

    normalized = normalize_table_heading(text)
    if normalized in selected_tables:
        return normalized

    if len(selected_tables) == 1:
        return selected_tables[0]

    return ""


def _sanitize_keyword_plan_payload(payload: dict, selected_tables: list[str]) -> dict:
    if not isinstance(payload, dict):
        return {}

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        return payload

    targets = []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue

        table = _coerce_target_table(item.get("table", ""), selected_tables)
        if not table:
            continue

        keywords = item.get("keywords", []) or []
        if not isinstance(keywords, list):
            keywords = [keywords]

        targets.append(
            {
                "table": table,
                "keywords": [str(keyword).strip() for keyword in keywords if str(keyword).strip()],
            }
        )

    return {"targets": targets}


def _coerce_keyword_plan(result: Any, selected_tables: list[str]) -> tuple[KeywordPlan, Optional[str], Optional[str]]:
    if isinstance(result, KeywordPlan):
        return result, None, None

    parsing_error = None

    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, KeywordPlan):
            return parsed, None, None

        if result.get("parsing_error") is not None:
            parsing_error = str(result.get("parsing_error"))[:250]

        candidates = [
            ("parsed_dict", parsed if isinstance(parsed, dict) else None),
            ("parsed_text", parsed),
            ("raw", result.get("raw")),
            ("content", result.get("content")),
        ]
    else:
        parsed = result
        candidates = [("result", result)]

    for source, candidate in candidates:
        payload = _try_parse_json_object(candidate)
        if payload is None:
            continue
        try:
            return KeywordPlan.model_validate(
                _sanitize_keyword_plan_payload(payload, selected_tables)
            ), parsing_error, source
        except ValidationError:
            continue

    if parsing_error:
        raise ValueError(parsing_error)

    raise ValueError("Keyworder did not return a valid KeywordPlan payload.")


def _fallback_worker_plan_from_hints(planner_plan: dict, selected_tables: list[str], user_query: str) -> dict:
    hint_map = _planner_hints_by_table(planner_plan, selected_tables, user_query)
    targets = []

    for table in selected_tables:
        repairs, details = validate_keywords(
            table,
            hint_map.get(table, []),
            fuzzy=True,
            cutoff=0.93,
        )
        for item in details:
            suggestion = item.get("suggested")
            if suggestion and suggestion not in repairs:
                repairs.append(suggestion)
        targets.append(
            {
                "table": table,
                "keywords": _limit_seed_keywords(repairs),
            }
        )

    return {"targets": targets}


def run_keyworder(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_keyworder"]
    planner_plan = state.get("planner_plan", {}) or {}
    trace = []
    started_at = time.perf_counter()
    llm_usage = {}

    start_log = make_debug_log(
        state,
        "keyworder:start",
        planner_plan=state.get("planner_plan", {}),
    )
    if start_log:
        trace.append(start_log)

    selected_tables = _selected_tables(planner_plan)
    planner_hints_by_table = _planner_hints_by_table(
        planner_plan,
        selected_tables,
        state.get("user_query", ""),
    )
    priority_hints_by_table = _priority_hints_by_table(
        planner_plan,
        selected_tables,
        state.get("user_query", ""),
    )
    fallback_worker_plan = _fallback_worker_plan_from_hints(
        planner_plan,
        selected_tables,
        state.get("user_query", ""),
    )

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(planner_plan, ensure_ascii=False),
        "worker_results_json": "{}",
        "allowed_keywords_json": _allowed_keywords_payload(selected_tables),
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    updates = {
        "last_agent": "agent_keyworder",
        "trace": trace,
    }

    try:
        raw_result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=KeywordPlan,
            plain_payload_factory=_plain_keyworder_payload,
        )
        llm_usage = extract_usage_metadata(raw_result.get("raw"))
        kp, parse_warning, recovered_from = _coerce_keyword_plan(raw_result, selected_tables)
        worker_plan = kp.model_dump()
        if raw_result.get("mode") != "structured":
            fallback_log = make_debug_log(
                state,
                "keyworder:structured_output_fallback",
                mode=raw_result.get("mode", "plain_json"),
            )
            if fallback_log:
                updates["trace"].append(fallback_log)

        targets_in = worker_plan.get("targets", []) or []

        by_table = {}
        for t in targets_in:
            table = str(t.get("table", "")).strip()
            kws = t.get("keywords", []) or []
            if not table:
                continue
            by_table.setdefault(table, [])
            by_table[table].extend(kws)

        cleaned_targets = []
        invalid_all = []
        repaired_all = []
        planner_hint_repairs = []

        for table in selected_tables:
            kws = by_table.get(table, [])
            valid_kws, invalid = validate_keywords(table, kws, fuzzy=True, cutoff=0.88)

            invalid = invalid or []
            invalid_all.extend([{"table": table, **x} for x in invalid])

            for x in invalid:
                s = x.get("suggested")
                if s and s not in valid_kws:
                    valid_kws.append(s)
                    repaired_all.append({
                        "table": table,
                        "from": x.get("raw", ""),
                        "to": s,
                    })

            if not valid_kws and kws:
                fallback_repairs, fallback_details = repair_keywords(table, kws)
                for repaired_kw in fallback_repairs:
                    if repaired_kw not in valid_kws:
                        valid_kws.append(repaired_kw)
                for x in fallback_details:
                    repaired_all.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": x.get("suggested", ""),
                            "source": "keyword",
                        }
                    )

            if not valid_kws and planner_hints_by_table.get(table):
                hint_valid, hint_details = validate_keywords(
                    table,
                    planner_hints_by_table.get(table, []),
                    fuzzy=True,
                    cutoff=0.93,
                )
                for repaired_kw in hint_valid:
                    if repaired_kw not in valid_kws:
                        valid_kws.append(repaired_kw)
                for x in hint_details:
                    suggestion = x.get("suggested")
                    if not suggestion:
                        continue
                    repaired_all.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": suggestion,
                            "source": "planner_hint",
                        }
                    )
                    planner_hint_repairs.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": suggestion,
                        }
                    )

            if priority_hints_by_table.get(table):
                valid_kws = _limit_seed_keywords(
                    list(priority_hints_by_table.get(table, []))
                    + list(valid_kws)
                )

            valid_kws = _limit_seed_keywords(valid_kws)
            cleaned_targets.append({"table": table, "keywords": valid_kws})

        worker_plan["targets"] = cleaned_targets

        empty_tables = {
            target["table"]
            for target in cleaned_targets
            if not target["keywords"]
        }
        if empty_tables:
            fallback_by_table = {
                target["table"]: target["keywords"]
                for target in fallback_worker_plan.get("targets", [])
            }
            for target in cleaned_targets:
                if not target["keywords"]:
                    target["keywords"] = fallback_by_table.get(target["table"], [])

        updates["worker_plan"] = worker_plan

        if parse_warning and recovered_from:
            debug_log = make_debug_log(
                state,
                "keyworder:recovered_from_raw",
                source=recovered_from,
                parsing_error=parse_warning,
            )
            if debug_log:
                updates["trace"].append(debug_log)

        empty_tables = [t["table"] for t in cleaned_targets if not t["keywords"]]
        if empty_tables:
            debug_log = make_debug_log(
                state,
                "keyworder:empty_keyword_targets",
                tables=empty_tables,
            )
            if debug_log:
                updates["trace"].append(debug_log)

        if invalid_all:
            debug_log = make_debug_log(
                state,
                "keyworder:invalid_keywords",
                invalid_count=len(invalid_all),
                samples=invalid_all[:5],
            )
            if debug_log:
                updates["trace"].append(debug_log)

        if repaired_all:
            debug_log = make_debug_log(
                state,
                "keyworder:repaired_keywords",
                repaired_count=len(repaired_all),
                samples=repaired_all[:5],
            )
            if debug_log:
                updates["trace"].append(debug_log)

        if planner_hint_repairs:
            debug_log = make_debug_log(
                state,
                "keyworder:planner_hint_repairs",
                repaired_count=len(planner_hint_repairs),
                samples=planner_hint_repairs[:5],
            )
            if debug_log:
                updates["trace"].append(debug_log)

        updates["trace"].append(
            make_log(
                state,
                "keyworder:done",
                targets_n=len(worker_plan.get("targets", []) or []),
                targets=_keyword_trace_targets(worker_plan),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )
        return updates

    except Exception as e:
        updates["worker_plan"] = fallback_worker_plan
        fallback_reason = str(e)[:250]
        if any(target.get("keywords") for target in fallback_worker_plan.get("targets", [])):
            updates["trace"].append(
                make_log(
                    state,
                    "keyworder:fallback_from_hints",
                    reason=fallback_reason,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                )
            )
        else:
            updates["trace"].append(
                make_log(
                    state,
                    "keyworder:error",
                    error_type=type(e).__name__,
                    error=fallback_reason,
                )
            )
        updates["trace"].append(
            make_log(
                state,
                "keyworder:done",
                targets_n=len(updates["worker_plan"].get("targets", []) or []),
                targets=_keyword_trace_targets(updates["worker_plan"]),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        return updates
