"""Route planner objectives to evidence_plan and analysis_plan payloads."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from agents.agent_tools_list import get_tools_list
from agents.agent_registry import is_analysis_agent, is_retrieval_agent
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import (
    ALIASES,
    ALLOWED_KEYWORDS,
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    build_allowed_keywords_payload,
)
from graph.logger import make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt
from schemas.agent_outputs import AnalysisPlanItem, EvidenceDispatchPlan, EvidencePlanItem, Target
from agents.prompts import PROMPT_TEMPLATE

MAX_TARGET_REQUIREMENTS = 8
OPTIONAL_ROUTER_REQUIREMENTS = {
    "chi phí bán hàng",
}
ANALYSIS_RETRIEVAL_ALLOWLIST = {
    "agent_profitability": {"agent_bs", "agent_is", "agent_note"},
    "agent_liquidity_solvency": {"agent_bs", "agent_is", "agent_cf", "agent_note"},
    "agent_cashflow_analysis": {"agent_bs", "agent_is", "agent_cf", "agent_note"},
    "agent_efficiency": {"agent_bs", "agent_is", "agent_note"},
}
FOLLOWUP_ROUTE_STOPWORDS = {
    "va",
    "và",
    "ve",
    "về",
    "cua",
    "của",
    "cho",
    "tu",
    "từ",
    "den",
    "đến",
    "cuoi",
    "cuối",
    "ky",
    "kỳ",
    "neu",
    "nếu",
    "muon",
    "muốn",
    "so",
    "sanh",
    "xu",
    "huong",
    "hướng",
    "nam",
    "năm",
    "quy",
    "quý",
    "thang",
    "tháng",
}
TABLE_TO_AGENT = {
    TABLE_BS: "agent_bs",
    TABLE_IS: "agent_is",
    TABLE_CF: "agent_cf",
    TABLE_NOTE: "agent_note",
}
KEYWORD_TABLE_PAIRS = [
    (keyword, table)
    for table, keywords in ALLOWED_KEYWORDS.items()
    for keyword in keywords
]


def _followup_requirements_from_plan(planner_plan: dict) -> list[str]:
    return _dedupe_keep_order(
        [
            str(item).strip()
            for item in (planner_plan.get("followup_requirements", []) or [])
            if str(item).strip()
        ]
    )


def _is_followup_mode(planner_plan: dict) -> bool:
    if planner_plan.get("followup_mode"):
        return True
    return bool(_followup_requirements_from_plan(planner_plan))


def _text_tokens(text: str) -> set[str]:
    tokens = set()
    for item in re.findall(r"\w+", str(text or "").lower()):
        if not item or item in FOLLOWUP_ROUTE_STOPWORDS:
            continue
        if re.fullmatch(r"(19|20)\d{2}", item):
            continue
        tokens.add(item)
    return tokens


def _candidate_route_specs() -> list[dict]:
    candidates = []
    for keyword, table in KEYWORD_TABLE_PAIRS:
        candidates.append(
            {
                "agent": TABLE_TO_AGENT.get(table, ""),
                "table": table,
                "match_text": str(keyword or "").strip().lower(),
                "tokens": _text_tokens(keyword),
            }
        )

    for alias, canonical in ALIASES.items():
        for table, keywords in ALLOWED_KEYWORDS.items():
            if canonical not in keywords:
                continue
            candidates.append(
                {
                    "agent": TABLE_TO_AGENT.get(table, ""),
                    "table": table,
                    "match_text": str(alias or "").strip().lower(),
                    "tokens": _text_tokens(alias),
                }
            )
    return candidates


FOLLOWUP_ROUTE_CANDIDATES = _candidate_route_specs()
MAIN_REPORT_TABLE_ORDER = (TABLE_BS, TABLE_IS, TABLE_CF)
MAIN_REPORT_TABLES = set(MAIN_REPORT_TABLE_ORDER)
NOTE_FOLLOWUP_MARKERS = (
    "thuyết minh",
    "thuyet minh",
    "note",
    "chính sách kế toán",
    "chinh sach ke toan",
    "bên liên quan",
    "ben lien quan",
    "cam kết",
    "cam ket",
    "nghĩa vụ tiềm tàng",
    "nghia vu tiem tang",
    "rủi ro tài chính",
    "rui ro tai chinh",
    "sự kiện sau ngày",
    "su kien sau ngay",
    "kỳ hạn vay",
    "ky han vay",
    "tài sản bảo đảm",
    "tai san bao dam",
    "tài sản đảm bảo",
    "tai san dam bao",
    "cơ cấu nợ",
    "co cau no",
    "chi tiết khoản mục",
    "chi tiet khoan muc",
    "tài sản thuê ngoài",
    "tai san thue ngoai",
)


def _requires_note_followup(requirement: str) -> bool:
    text = str(requirement or "").strip().lower()
    return any(marker in text for marker in NOTE_FOLLOWUP_MARKERS)


def _keyword_match_score(requirement: str, candidate_text: str) -> float:
    req_norm = str(requirement or "").strip().lower()
    candidate_norm = str(candidate_text or "").strip().lower()
    if not req_norm or not candidate_norm:
        return 0.0

    if candidate_norm == req_norm:
        return 100.0
    if candidate_norm in req_norm:
        return 80.0
    if req_norm in candidate_norm:
        return 60.0

    req_tokens = _text_tokens(req_norm)
    candidate_tokens = _text_tokens(candidate_norm)
    if not req_tokens or not candidate_tokens:
        return 0.0

    overlap = len(req_tokens.intersection(candidate_tokens))
    if not overlap:
        return 0.0

    coverage = overlap / max(len(candidate_tokens), 1)
    precision = overlap / max(len(req_tokens), 1)
    return coverage * 5.0 + precision * 2.0 + overlap


def _normalize_main_report_followup_requirement(requirement: str, table: str) -> str:
    allowed = ALLOWED_KEYWORDS.get(table, set()) or set()
    if table not in MAIN_REPORT_TABLES or not allowed:
        return str(requirement or "").strip()

    best_keyword = ""
    best_score = 0.0

    for keyword in allowed:
        score = _keyword_match_score(requirement, keyword)
        if score > best_score:
            best_keyword = keyword
            best_score = score

    for alias, canonical in ALIASES.items():
        if canonical not in allowed:
            continue
        score = _keyword_match_score(requirement, alias)
        if score > best_score:
            best_keyword = canonical
            best_score = score

    # Substring matches score high. Token-only matches need enough coverage to
    # avoid rewriting broad follow-up requirements into unrelated line items.
    if best_keyword and best_score >= 5.0:
        return best_keyword

    return str(requirement or "").strip()


def _matching_main_report_keywords(requirement: str) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for table in MAIN_REPORT_TABLE_ORDER:
        allowed = ALLOWED_KEYWORDS.get(table, set()) or set()
        for keyword in allowed:
            if _keyword_match_score(requirement, keyword) >= 60.0:
                matches.setdefault(table, []).append(keyword)

        for alias, canonical in ALIASES.items():
            if canonical not in allowed:
                continue
            if _keyword_match_score(requirement, alias) >= 60.0:
                matches.setdefault(table, []).append(canonical)

    return {
        table: _dedupe_keep_order(requirements)
        for table, requirements in matches.items()
        if requirements
    }


def _main_report_route_for_requirement(requirement: str) -> Optional[tuple[str, str]]:
    matches = _matching_main_report_keywords(requirement)
    if not matches:
        return None

    best_table = max(
        matches,
        key=lambda table: (
            len(matches.get(table, []) or []),
            max(
                _keyword_match_score(requirement, keyword)
                for keyword in matches.get(table, []) or [""]
            ),
        ),
    )
    return TABLE_TO_AGENT.get(best_table, ""), best_table


def _normalize_main_report_followup_requirements(requirement: str, table: str) -> list[str]:
    matches = _matching_main_report_keywords(requirement)
    table_matches = matches.get(table, [])
    if table_matches:
        return table_matches
    return [_normalize_main_report_followup_requirement(requirement, table)]


def _table_for_allowed_query(query: str) -> str:
    route = _main_report_route_for_requirement(query)
    if route:
        return route[1]
    return ""


def _compact_note_followup_requirement(requirement: str) -> str:
    original = str(requirement or "").strip()
    text = original
    if not text:
        return ""

    text = re.sub(
        r"^(cần|thiếu|bổ sung|lấy|truy xuất)\s+"
        r"((dữ liệu|thông tin|chi tiết|diễn giải)\s+)?"
        r"((về|cho|của|liên quan đến)\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(
        r"\s+để\s+(tính|đánh giá|phân tích|trả lời|xác định)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" .;,-")

    return text or original


def _normalize_followup_requirement_for_target(requirement: str, table: str) -> str:
    if table in MAIN_REPORT_TABLES:
        return _normalize_main_report_followup_requirement(requirement, table)
    if table == TABLE_NOTE:
        return _compact_note_followup_requirement(requirement)
    return str(requirement or "").strip()


def _normalize_followup_requirements_for_target(requirement: str, table: str) -> list[str]:
    if table in MAIN_REPORT_TABLES:
        return _normalize_main_report_followup_requirements(requirement, table)
    normalized = _normalize_followup_requirement_for_target(requirement, table)
    return [normalized] if normalized else []


def _table_for_agent(agent_name: str) -> str:
    return next(
        (table for table, agent in TABLE_TO_AGENT.items() if agent == agent_name),
        "",
    )


def _agent_for_evidence_table(table: str) -> str:
    table_name = str(table or "").strip()
    if not table_name:
        return "agent_web"
    return TABLE_TO_AGENT.get(table_name, "")


def _needby_values(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []
    raw = item.get("needby")
    if raw is None:
        raw = item.get("needed_by")
    return [
        str(value).strip()
        for value in (raw or [])
        if is_analysis_agent(str(value).strip())
    ]


def _followup_route_hints_from_plan(planner_plan: dict) -> dict[str, tuple[str, str]]:
    hints: dict[str, tuple[str, str]] = {}
    for item in (planner_plan.get("followup_requests", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        table = str(item.get("table", "") or "").strip()
        if not is_retrieval_agent(agent):
            continue
        if not table:
            table = _table_for_agent(agent)
        for requirement in _dedupe_keep_order(item.get("requirements", []) or []):
            hints[str(requirement or "").strip()] = (agent, table)
    return hints


def _followup_route_hints_from_worker_plan(worker_plan: dict) -> dict[str, tuple[str, str]]:
    hints: dict[str, tuple[str, str]] = {}
    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        table = str(item.get("table", "") or "").strip()
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue
        if not agent and table:
            agent = TABLE_TO_AGENT.get(table, "")
        if not is_retrieval_agent(agent):
            continue
        hints[query] = (agent, table or _table_for_agent(agent))

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        table = str(target.get("table", "") or "").strip()
        if not is_retrieval_agent(agent):
            continue
        for requirement in _dedupe_keep_order(target.get("requirements", []) or []):
            hints[str(requirement or "").strip()] = (agent, table)
    return hints


def _heuristic_followup_route(requirement: str) -> tuple[str, str]:
    text = str(requirement or "").strip().lower()

    if _requires_note_followup(text):
        return "agent_note", TABLE_NOTE

    if any(
        marker in text
        for marker in (
            "dòng tiền",
            "lưu chuyển tiền",
            "tiền thu",
            "tiền chi",
            "trả nợ",
            "vay",
            "cổ tức",
        )
    ):
        return "agent_cf", TABLE_CF

    if any(
        marker in text
        for marker in (
            "vốn chủ sở hữu",
            "tổng tài sản",
            "tổng cộng tài sản",
            "nguồn vốn",
            "nợ",
            "hàng tồn kho",
            "phải thu",
            "phải trả",
        )
    ):
        return "agent_bs", TABLE_BS

    return "agent_is", TABLE_IS


def _route_followup_requirement(requirement: str, hint: Optional[tuple[str, str]] = None) -> tuple[str, str]:
    normalized_requirement = str(requirement or "").strip().lower()
    main_report_route = _main_report_route_for_requirement(normalized_requirement)
    if main_report_route:
        return main_report_route

    if _requires_note_followup(normalized_requirement):
        return "agent_note", TABLE_NOTE

    if hint:
        hinted_agent = str(hint[0] if len(hint) > 0 else "").strip()
        hinted_table = str(hint[1] if len(hint) > 1 else "").strip()
        if hinted_agent in set(TABLE_TO_AGENT.values()):
            if not hinted_table:
                hinted_table = _table_for_agent(hinted_agent)
            return hinted_agent, hinted_table

    req_tokens = _text_tokens(normalized_requirement)
    best_candidate = None
    best_score = 0.0

    for candidate in FOLLOWUP_ROUTE_CANDIDATES:
        score = 0.0
        match_text = str(candidate.get("match_text", "") or "").strip()
        candidate_tokens = set(candidate.get("tokens", set()) or set())

        if match_text and match_text in normalized_requirement:
            score += 5.0

        overlap = len(req_tokens.intersection(candidate_tokens))
        if overlap:
            score += overlap / max(len(candidate_tokens), 1)
            score += overlap / max(len(req_tokens), 1)

        if score > best_score:
            best_candidate = candidate
            best_score = score

    if best_candidate and best_score >= 1.0:
        return (
            str(best_candidate.get("agent", "") or "").strip(),
            str(best_candidate.get("table", "") or "").strip(),
        )

    return _heuristic_followup_route(requirement)


def _normalize_followup_router_targets(
    worker_plan: dict,
    planner_plan: dict,
    pending_analysis_targets: list[dict] | None = None,
) -> dict:
    followup_requirements = _followup_requirements_from_plan(planner_plan)
    if not followup_requirements:
        return worker_plan

    route_hints = {
        **_followup_route_hints_from_worker_plan(worker_plan),
        **_followup_route_hints_from_plan(planner_plan),
    }
    grouped_requirements: dict[tuple[str, str], list[str]] = {}

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue

        agent = str(target.get("agent", "") or "").strip()
        if not is_retrieval_agent(agent):
            continue

    for requirement in followup_requirements:
        hint = route_hints.get(requirement)
        routed_agent, routed_table = _route_followup_requirement(requirement, hint=hint)
        normalized_requirements = _normalize_followup_requirements_for_target(
            requirement,
            routed_table,
        )
        key = (routed_agent, routed_table)
        grouped_requirements.setdefault(key, [])
        grouped_requirements[key].extend(normalized_requirements)

    retrieval_targets = []
    for (agent, table), requirements in grouped_requirements.items():
        retrieval_targets.append(
            {
                "agent": agent,
                "table": table,
                "requirements": _dedupe_keep_order(requirements)[:MAX_TARGET_REQUIREMENTS],
                "source": "followup",
            }
        )

    evidence_plan = _merge_evidence_plans(
        worker_plan.get("evidence_plan", []) or [],
        _evidence_plan_from_retrieval_targets(
            retrieval_targets,
            [],
        ),
    )

    return {
        "evidence_plan": evidence_plan,
        "analysis_plan": [],
        "targets": [],
    }


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _split_retrieval_requirement_item(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    parts = [item.strip() for item in re.split(r"\s*[;,]\s*", text) if item.strip()]
    if len(parts) <= 1:
        return [text]
    return parts


def _normalize_retrieval_requirements(requirements: list[str]) -> list[str]:
    expanded = []
    for item in requirements or []:
        expanded.extend(_split_retrieval_requirement_item(item))
    return _dedupe_keep_order(expanded)


def _router_trace_targets(worker_plan: dict) -> list[dict]:
    targets = []
    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        payload = {
            "agent": agent,
            "objective": str(item.get("objective", "") or "").strip(),
        }
        if not is_analysis_agent(agent):
            payload["table"] = str(item.get("table", "") or "").strip()
            payload["requirements"] = [
                str(req).strip()
                for req in (item.get("requirements", []) or [])
                if str(req).strip()
            ][:2]
        targets.append(payload)
    return targets


def _router_trace_evidence_plan(worker_plan: dict) -> list[dict]:
    items = []
    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "table": str(item.get("table", "") or "").strip(),
                "query": str(item.get("query", "") or "").strip(),
                "needby": [
                    str(agent).strip()
                    for agent in (item.get("needby", []) or item.get("needed_by", []) or [])
                    if str(agent).strip()
                ][:3],
            }
        )
    return items[:8]


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema EvidenceDispatchPlan.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- Output phai co dang: {"evidence_plan":[...],"analysis_plan":[...]}.\n'
    )


def _plain_router_payload(payload: dict) -> dict:
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


def _coerce_router_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "queries", "requirements", "targets", "evidence_plan", "analysis_plan"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _repair_dispatch_payload_keys(payload: dict) -> dict:
    repaired = dict(payload)

    if "evidence_plan" not in repaired:
        for key in ("evidence", "evidence_items", "retrieval_plan", "retrieval_queries", "queries", "items"):
            values = _coerce_router_list(repaired.get(key))
            if values:
                repaired["evidence_plan"] = values
                break

    if "analysis_plan" not in repaired:
        for key in ("analysis", "analysis_items", "analyses"):
            values = _coerce_router_list(repaired.get(key))
            if values:
                repaired["analysis_plan"] = values
                break

    if "targets" not in repaired:
        for key in ("retrieval_targets", "workers", "worker_targets"):
            values = _coerce_router_list(repaired.get(key))
            if values:
                repaired["targets"] = values
                break

    return repaired


def _try_parse_json_object(value: Any) -> Optional[dict]:
    if isinstance(value, list):
        return {"evidence_plan": value, "analysis_plan": [], "targets": []}

    if isinstance(value, dict):
        repaired = _repair_dispatch_payload_keys(value)
        if (
            isinstance(repaired.get("evidence_plan"), list)
            or isinstance(repaired.get("analysis_plan"), list)
            or isinstance(repaired.get("targets"), list)
        ):
            return repaired

        for key in (
            "evidence_dispatch_plan",
            "dispatch_plan",
            "router_plan",
            "worker_plan",
            "plan",
            "output",
            "data",
            "result",
        ):
            nested = value.get(key)
            if nested is not None:
                parsed = _try_parse_json_object(nested)
                if parsed is not None:
                    return parsed

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
        if isinstance(parsed, list):
            return {"evidence_plan": parsed, "analysis_plan": [], "targets": []}
        if isinstance(parsed, dict):
            return _try_parse_json_object(parsed)

    return None


def _validate_dispatch_payload(
    payload: dict,
    parsing_error: Optional[str],
    source: str,
) -> tuple[EvidenceDispatchPlan, Optional[str], Optional[str]]:
    try:
        return EvidenceDispatchPlan.model_validate(payload), parsing_error, source
    except ValidationError as first_error:
        sanitized_payload = _sanitize_router_plan_payload(payload)
        try:
            return (
                EvidenceDispatchPlan.model_validate(sanitized_payload),
                parsing_error or str(first_error)[:250],
                f"{source}:sanitized",
            )
        except ValidationError:
            raise first_error


def _coerce_dispatch_plan(result: Any) -> tuple[EvidenceDispatchPlan, Optional[str], Optional[str]]:
    if isinstance(result, EvidenceDispatchPlan):
        return result, None, None

    parsing_error = None

    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, EvidenceDispatchPlan):
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
        candidates = [("result", result)]

    for source, candidate in candidates:
        payload = _try_parse_json_object(candidate)
        if payload is None:
            continue
        try:
            return _validate_dispatch_payload(payload, parsing_error, source)
        except ValidationError:
            continue

    if parsing_error:
        raise ValueError(parsing_error)

    raise ValueError("Router did not return a valid EvidenceDispatchPlan payload.")


def _repair_target_route_payload(item: dict) -> dict:
    data = dict(item)
    agent = str(data.get("agent", "") or "").strip()
    if is_analysis_agent(agent):
        return data

    table = str(data.get("table", "") or "").strip()
    if is_retrieval_agent(agent):
        if not table:
            data["table"] = _table_for_agent(agent)
        return data

    if table:
        inferred_agent = _agent_for_evidence_table(table)
        if inferred_agent and is_retrieval_agent(inferred_agent):
            data["agent"] = inferred_agent
            return data

    for query in _evidence_queries_from_raw_item(data):
        route = _main_report_route_for_requirement(query)
        if route:
            data["agent"], data["table"] = route
            return data
        if _requires_note_followup(query):
            data["agent"], data["table"] = "agent_note", TABLE_NOTE
            return data

    return data


def _normalize_target_payload(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None

    item = _repair_target_route_payload(item)
    try:
        target = Target.model_validate(item)
    except ValidationError:
        return None

    payload = target.model_dump(exclude_none=True)
    requirements = payload.get("requirements", []) or []
    if is_retrieval_agent(str(payload.get("agent", "") or "").strip()):
        requirements = _normalize_retrieval_requirements(requirements)
    else:
        requirements = _dedupe_keep_order(requirements)

    payload["requirements"] = requirements[:MAX_TARGET_REQUIREMENTS]
    if not payload["requirements"]:
        return None
    return payload


def _evidence_queries_from_raw_item(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []

    raw_items = []
    if str(item.get("query", "") or "").strip():
        raw_items.append(item.get("query"))

    for key in ("queries", "requirements", "keywords"):
        value = item.get(key)
        if isinstance(value, (list, tuple, set)):
            raw_items.extend(value)
        elif str(value or "").strip():
            raw_items.append(value)

    return _dedupe_keep_order([str(value).strip() for value in raw_items if str(value).strip()])


def _normalize_evidence_plan_payloads(items: list[Any]) -> list[dict]:
    normalized: list[dict] = []
    merged_by_key: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue

        queries = _evidence_queries_from_raw_item(item)
        if not queries:
            queries = [""]

        for query in queries:
            candidate = dict(item)
            candidate["query"] = query
            try:
                evidence_item = EvidencePlanItem.model_validate(candidate)
            except ValidationError:
                continue

            payload = evidence_item.model_dump(exclude_none=True)
            legacy_agent = str(item.get("agent", "") or "").strip()
            table = (
                str(payload.get("table", "") or "").strip()
                or _table_for_agent(legacy_agent)
                or _table_for_allowed_query(payload.get("query", ""))
            )
            retrieval_agent = _agent_for_evidence_table(table)
            if not retrieval_agent:
                continue
            query_text = _normalize_followup_requirement_for_target(payload.get("query", ""), table)
            if not query_text:
                continue

            needby = _needby_values(payload)
            key = (table, query_text)
            if key not in merged_by_key:
                merged_by_key[key] = {
                    "table": table,
                    "query": query_text,
                    "needby": [],
                }
                order.append(key)

            merged_by_key[key]["needby"] = _dedupe_keep_order(
                list(merged_by_key[key].get("needby", []) or []) + needby
            )

    for key in order:
        normalized.append(merged_by_key[key])
    return normalized


def _normalize_analysis_plan_payloads(items: list[Any]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            analysis_item = AnalysisPlanItem.model_validate(item)
        except ValidationError:
            continue

        payload = analysis_item.model_dump()
        agent = str(payload.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        objective = str(payload.get("objective", "") or "").strip()
        if not objective:
            continue

        if agent not in merged:
            merged[agent] = {
                "agent": agent,
                "objective": objective,
                "evidence_queries": [],
            }
            order.append(agent)

        if not merged[agent].get("objective"):
            merged[agent]["objective"] = objective

    return [merged[agent] for agent in order]


def _sanitize_router_plan_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"evidence_plan": [], "analysis_plan": [], "targets": []}

    raw_targets = payload.get("targets")
    normalized_targets = []
    seen = set()

    for item in (raw_targets if isinstance(raw_targets, list) else []):
        target = _normalize_target_payload(item)
        if target is None:
            continue

        key = (
            str(target.get("agent", "")).strip(),
            str(target.get("table", "") or "").strip(),
            tuple(target.get("requirements", []) or []),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized_targets.append(target)

    raw_evidence_plan = payload.get("evidence_plan")
    raw_analysis_plan = payload.get("analysis_plan")
    return {
        "evidence_plan": _normalize_evidence_plan_payloads(
            raw_evidence_plan if isinstance(raw_evidence_plan, list) else []
        ),
        "analysis_plan": _normalize_analysis_plan_payloads(
            raw_analysis_plan if isinstance(raw_analysis_plan, list) else []
        ),
        "targets": normalized_targets,
    }


def _planner_analysis_targets(planner_plan: dict) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue

        agent = str(axis.get("axis", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        objective = str(axis.get("objective", "") or "").strip()
        if agent not in merged:
            merged[agent] = {
                "agent": agent,
                "objective": "",
                "objectives": [],
            }
            order.append(agent)

        if objective:
            if not merged[agent].get("objective"):
                merged[agent]["objective"] = objective
            merged[agent]["objectives"] = _dedupe_keep_order(
                list(merged[agent].get("objectives", []) or []) + [objective]
            )

    return [merged[agent] for agent in order]


def _user_query_mentions_optional_requirement(user_query: str, requirement: str) -> bool:
    query_text = str(user_query or "").strip().lower()
    requirement_text = str(requirement or "").strip().lower()
    if not query_text or not requirement_text:
        return False
    return requirement_text in query_text


def _filter_optional_router_requirements(
    retrieval_targets: list[dict],
    user_query: str,
) -> list[dict]:
    filtered_targets: list[dict] = []

    for target in retrieval_targets or []:
        if not isinstance(target, dict):
            continue

        requirements = []
        for requirement in target.get("requirements", []) or []:
            text = str(requirement or "").strip()
            if not text:
                continue
            if (
                text.lower() in OPTIONAL_ROUTER_REQUIREMENTS
                and not _user_query_mentions_optional_requirement(user_query, text)
            ):
                continue
            requirements.append(text)

        if not requirements:
            continue

        filtered_targets.append({**target, "requirements": _dedupe_keep_order(requirements)})

    return filtered_targets


def _merge_retrieval_targets(retrieval_targets: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []

    def add_target(agent: str, table: str, requirements: list[str]) -> None:
        agent_name = str(agent or "").strip()
        table_name = str(table or "").strip()
        if not agent_name or not is_retrieval_agent(agent_name):
            return

        clean_requirements = _dedupe_keep_order(
            [str(item).strip() for item in requirements or [] if str(item).strip()]
        )
        if not clean_requirements:
            return

        key = (agent_name, table_name)
        if key not in merged:
            merged[key] = {
                "agent": agent_name,
                "table": table_name,
                "requirements": [],
            }
            order.append(key)

        merged[key]["requirements"] = _dedupe_keep_order(
            list(merged[key].get("requirements", []) or []) + clean_requirements
        )[:MAX_TARGET_REQUIREMENTS]

    for target in retrieval_targets or []:
        add_target(
            target.get("agent", ""),
            target.get("table", ""),
            target.get("requirements", []) or [],
        )

    return [merged[key] for key in order]


def _evidence_plan_from_retrieval_targets(
    retrieval_targets: list[dict],
    analysis_targets: list[dict],
) -> list[dict]:
    analysis_agents = [
        str(target.get("agent", "") or "").strip()
        for target in analysis_targets or []
        if str(target.get("agent", "") or "").strip()
    ]
    evidence_plan = []
    seen = set()

    for target in retrieval_targets or []:
        if not isinstance(target, dict):
            continue
        retrieval_agent = str(target.get("agent", "") or "").strip()
        if not is_retrieval_agent(retrieval_agent):
            continue
        table = str(target.get("table", "") or "").strip() or _table_for_agent(retrieval_agent)
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        needed_by = [
            analysis_agent
            for analysis_agent in analysis_agents
            if retrieval_agent in ANALYSIS_RETRIEVAL_ALLOWLIST.get(analysis_agent, set())
        ]
        if not needed_by and analysis_agents:
            needed_by = list(analysis_agents)

        for requirement in requirements:
            query = _normalize_followup_requirement_for_target(requirement, table)
            if not query:
                continue
            key = (retrieval_agent, table, query)
            if key in seen:
                continue
            seen.add(key)
            evidence_plan.append(
                {
                    "table": table,
                    "query": query,
                    "needby": needed_by,
                }
            )

    return evidence_plan


def _analysis_plan_from_targets(
    analysis_targets: list[dict],
    evidence_plan: list[dict],
) -> list[dict]:
    plan = []
    for target in analysis_targets or []:
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        objectives = _dedupe_keep_order(target.get("objectives", []) or [])
        objective_text = str(target.get("objective", "") or "").strip()
        if objective_text:
            objectives = _dedupe_keep_order([objective_text] + objectives)
        if not objectives:
            objectives = _dedupe_keep_order(target.get("requirements", []) or [])

        evidence_queries = []
        for item in evidence_plan or []:
            if not isinstance(item, dict):
                continue
            needby = _needby_values(item)
            if needby and agent not in needby:
                continue
            if not needby and evidence_plan and len(analysis_targets or []) > 0:
                continue
            evidence_queries.append(
                {
                    "table": str(item.get("table", "") or "").strip(),
                    "query": str(item.get("query", "") or "").strip(),
                }
            )

        plan.append(
            {
                "agent": agent,
                "objective": "; ".join(objectives),
                "evidence_queries": evidence_queries,
            }
        )

    return plan


def _analysis_targets_from_plan(analysis_plan: list[dict]) -> list[dict]:
    targets = []
    for item in analysis_plan or []:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue
        targets.append(
            {
                "agent": agent,
                "objective": str(item.get("objective", "") or "").strip(),
                "evidence_queries": list(item.get("evidence_queries", []) or []),
            }
        )
    return targets


def _with_inferred_needed_by(evidence_plan: list[dict], analysis_targets: list[dict]) -> list[dict]:
    analysis_agents = [
        str(target.get("agent", "") or "").strip()
        for target in analysis_targets or []
        if is_analysis_agent(str(target.get("agent", "") or "").strip())
    ]
    output = []

    for item in evidence_plan or []:
        if not isinstance(item, dict):
            continue

        table = str(item.get("table", "") or "").strip()
        retrieval_agent = str(item.get("agent", "") or "").strip() or _agent_for_evidence_table(table)
        if not is_retrieval_agent(retrieval_agent):
            continue

        explicit_needby = _needby_values(item)
        inferred_needed_by = [
            analysis_agent
            for analysis_agent in analysis_agents
            if retrieval_agent in ANALYSIS_RETRIEVAL_ALLOWLIST.get(analysis_agent, set())
        ]
        if not inferred_needed_by and analysis_agents:
            inferred_needed_by = list(analysis_agents)
        needby = explicit_needby or inferred_needed_by
        output.append(
            {
                "table": table or _table_for_agent(retrieval_agent),
                "query": str(item.get("query", "") or "").strip(),
                "needby": _dedupe_keep_order(needby),
            }
        )

    return output


def _merge_evidence_plans(*plans: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []

    for plan in plans:
        for item in plan or []:
            if not isinstance(item, dict):
                continue

            table = str(item.get("table", "") or "").strip()
            agent = str(item.get("agent", "") or "").strip() or _agent_for_evidence_table(table)
            if not table:
                table = _table_for_allowed_query(item.get("query", ""))
                if table:
                    agent = _agent_for_evidence_table(table)
            if not is_retrieval_agent(agent):
                continue
            table = table or _table_for_agent(agent)
            query = _normalize_followup_requirement_for_target(item.get("query", ""), table)
            if not query:
                continue

            key = (table, query)
            if key not in merged:
                merged[key] = {
                    "table": table,
                    "query": query,
                    "needby": [],
                }
                order.append(key)

            merged[key]["needby"] = _dedupe_keep_order(
                list(merged[key].get("needby", []) or []) + _needby_values(item)
            )

    return [merged[key] for key in order]


def _fallback_evidence_items_from_text(text: str, needby: list[str] | None = None) -> list[dict]:
    text_value = str(text or "").strip()
    if not text_value:
        return []

    items = []
    needed_by = [
        str(agent).strip()
        for agent in (needby or [])
        if is_analysis_agent(str(agent).strip())
    ]

    for table in MAIN_REPORT_TABLE_ORDER:
        allowed = ALLOWED_KEYWORDS.get(table, set()) or set()
        for query in _normalize_main_report_followup_requirements(text_value, table):
            if query in allowed:
                items.append(
                    {
                        "table": table,
                        "query": query,
                        "needby": needed_by,
                    }
                )

    if _requires_note_followup(text_value):
        note_query = _compact_note_followup_requirement(text_value)
        if note_query:
            items.append(
                {
                    "table": TABLE_NOTE,
                    "query": note_query,
                    "needby": needed_by,
                }
            )

    return _merge_evidence_plans(items)


def _planner_axis_agents(planner_plan: dict) -> list[str]:
    agents = []
    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        agent = str(axis.get("axis", "") or "").strip()
        if is_analysis_agent(agent):
            agents.append(agent)
    return _dedupe_keep_order(agents)


def _fallback_router_payload_from_planner(planner_plan: dict, user_query: str = "") -> dict:
    axis_agents = _planner_axis_agents(planner_plan)
    evidence_items: list[dict] = []
    text_entries: list[tuple[str, list[str]]] = []

    for requirement in _followup_requirements_from_plan(planner_plan):
        text_entries.append((requirement, axis_agents))

    for item in (planner_plan.get("followup_requests", []) or []):
        if not isinstance(item, dict):
            continue
        for requirement in _dedupe_keep_order(item.get("requirements", []) or []):
            text_entries.append((requirement, axis_agents))

    for component in _dedupe_keep_order(planner_plan.get("required_components", []) or []):
        text_entries.append((component, axis_agents))

    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        agent = str(axis.get("axis", "") or "").strip()
        needby = [agent] if is_analysis_agent(agent) else []
        for component in _dedupe_keep_order(axis.get("components", []) or []):
            text_entries.append((component, needby))
        objective = str(axis.get("objective", "") or "").strip()
        if objective:
            text_entries.append((objective, needby))

    if user_query:
        text_entries.append((user_query, axis_agents))

    for text, needby in text_entries:
        evidence_items.extend(_fallback_evidence_items_from_text(text, needby=needby))

    if bool(planner_plan.get("need_web", False)) and user_query:
        evidence_items.append(
            {
                "table": "",
                "query": str(user_query or "").strip(),
                "needby": axis_agents,
            }
        )

    return {
        "evidence_plan": _merge_evidence_plans(evidence_items),
        "analysis_plan": [],
        "targets": [],
    }


def _finalize_router_targets(worker_plan: dict, planner_plan: dict, user_query: str = "") -> dict:
    normalized_targets = list((worker_plan or {}).get("targets", []) or [])
    direct_evidence_plan = list((worker_plan or {}).get("evidence_plan", []) or [])
    direct_analysis_plan = list((worker_plan or {}).get("analysis_plan", []) or [])
    retrieval_targets = [
        target
        for target in normalized_targets
        if is_retrieval_agent(str(target.get("agent", "") or "").strip())
    ]
    retrieval_targets = _filter_optional_router_requirements(retrieval_targets, user_query)

    retrieval_targets = _merge_retrieval_targets(retrieval_targets)

    difficulty_level = str(planner_plan.get("difficulty_level", "") or "").strip().lower()
    if difficulty_level != "hard":
        evidence_plan = _merge_evidence_plans(
            direct_evidence_plan,
            _evidence_plan_from_retrieval_targets(
                retrieval_targets,
                [],
            ),
        )
        return {
            "evidence_plan": evidence_plan,
            "analysis_plan": [],
            "targets": [],
        }

    planned_analysis_targets = _planner_analysis_targets(planner_plan) or _analysis_targets_from_plan(direct_analysis_plan)
    planned_by_agent = {
        str(target.get("agent", "") or "").strip(): dict(target)
        for target in planned_analysis_targets
        if str(target.get("agent", "") or "").strip()
    }
    analysis_agent_names = [
        str(target.get("agent", "") or "").strip()
        for target in planned_analysis_targets
        if str(target.get("agent", "") or "").strip()
    ]

    analysis_targets = []
    for agent in analysis_agent_names:
        planned_target = planned_by_agent.get(agent, {})
        objectives = _dedupe_keep_order(
            list(planned_target.get("objectives", []) or [])
            + list(planned_target.get("requirements", []) or [])
        )
        objective = str(planned_target.get("objective", "") or "").strip()
        if not objective and objectives:
            objective = "; ".join(objectives)
        analysis_targets.append(
            {
                "agent": agent,
                "objective": objective,
                "objectives": objectives or ([objective] if objective else []),
            }
        )

    evidence_plan = _merge_evidence_plans(
        _with_inferred_needed_by(direct_evidence_plan, analysis_targets),
        _evidence_plan_from_retrieval_targets(
            retrieval_targets,
            analysis_targets,
        ),
    )
    analysis_plan = _analysis_plan_from_targets(
        analysis_targets,
        evidence_plan,
    )
    return {
        "evidence_plan": evidence_plan,
        "analysis_plan": analysis_plan,
        "targets": _analysis_targets_from_plan(analysis_plan),
    }


def run_router(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_router"]
    planner_plan = state.get("planner_plan", {}) or {}
    trace = []
    started_at = time.perf_counter()
    llm_usage = {}

    start_log = make_debug_log(
        state,
        "router:start",
        planner_plan=planner_plan,
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(planner_plan, ensure_ascii=False),
        "worker_results_json": "{}",
        "allowed_keywords_json": build_allowed_keywords_payload(),
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list("agent_router"),
    }

    updates = {
        "last_agent": "agent_router",
        "trace": trace,
    }

    try:
        raw_result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=EvidenceDispatchPlan,
            plain_payload_factory=_plain_router_payload,
        )
        llm_usage = extract_usage_metadata(raw_result.get("raw"))
        plan_obj, parse_warning, recovered_from = _coerce_dispatch_plan(raw_result)
        worker_plan = _finalize_router_targets(
            _sanitize_router_plan_payload(plan_obj.model_dump()),
            planner_plan,
            user_query=state.get("user_query", ""),
        )
        if _is_followup_mode(planner_plan):
            worker_plan = _normalize_followup_router_targets(
                worker_plan,
                planner_plan,
                pending_analysis_targets=state.get("pending_analysis_targets", []) or [],
            )

        if bool(planner_plan.get("need_web", False)) and any(
            not str(item.get("table", "") or "").strip()
            for item in (worker_plan.get("evidence_plan", []) or [])
        ):
            worker_plan["need_web"] = True

        updates["worker_plan"] = worker_plan
        updates["expected_workers"] = []
        updates["dispatch_phase"] = "evidence"
        updates["pending_analysis_targets"] = []

        if raw_result.get("mode") != "structured":
            fallback_log = make_debug_log(
                state,
                "router:structured_output_fallback",
                mode=raw_result.get("mode", "plain_json"),
            )
            if fallback_log:
                updates["trace"].append(fallback_log)

        if parse_warning and recovered_from:
            debug_log = make_debug_log(
                state,
                "router:recovered_from_raw",
                source=recovered_from,
                parsing_error=parse_warning,
            )
            if debug_log:
                updates["trace"].append(debug_log)

        updates["trace"].append(
            make_log(
                state,
                "router:done",
                targets_n=len((updates.get("worker_plan", {}) or {}).get("targets", []) or []),
                targets=_router_trace_targets(updates.get("worker_plan", {}) or {}),
                evidence_items_n=len((updates.get("worker_plan", {}) or {}).get("evidence_plan", []) or []),
                evidence_plan=_router_trace_evidence_plan(updates.get("worker_plan", {}) or {}),
                analysis_plan_n=len((updates.get("worker_plan", {}) or {}).get("analysis_plan", []) or []),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )
        return updates

    except Exception as e:
        worker_plan = _finalize_router_targets(
            _fallback_router_payload_from_planner(
                planner_plan,
                user_query=state.get("user_query", ""),
            ),
            planner_plan,
            user_query=state.get("user_query", ""),
        )
        if _is_followup_mode(planner_plan):
            worker_plan = _normalize_followup_router_targets(
                worker_plan,
                planner_plan,
                pending_analysis_targets=state.get("pending_analysis_targets", []) or [],
            )

        if bool(planner_plan.get("need_web", False)) and any(
            not str(item.get("table", "") or "").strip()
            for item in (worker_plan.get("evidence_plan", []) or [])
        ):
            worker_plan["need_web"] = True

        updates["worker_plan"] = worker_plan
        updates["expected_workers"] = []
        updates["dispatch_phase"] = "evidence"
        updates["pending_analysis_targets"] = []
        updates["trace"].append(
            make_log(
                state,
                "router:heuristic_fallback",
                error_type=type(e).__name__,
                error=str(e)[:250],
                evidence_items_n=len((worker_plan.get("evidence_plan", []) or [])),
                analysis_plan_n=len((worker_plan.get("analysis_plan", []) or [])),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        updates["trace"].append(
            make_log(
                state,
                "router:done",
                targets_n=len((updates.get("worker_plan", {}) or {}).get("targets", []) or []),
                targets=_router_trace_targets(updates.get("worker_plan", {}) or {}),
                evidence_items_n=len((updates.get("worker_plan", {}) or {}).get("evidence_plan", []) or []),
                evidence_plan=_router_trace_evidence_plan(updates.get("worker_plan", {}) or {}),
                analysis_plan_n=len((updates.get("worker_plan", {}) or {}).get("analysis_plan", []) or []),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        return updates


def run_keyworder(state: dict) -> dict:
    return run_router(state)
