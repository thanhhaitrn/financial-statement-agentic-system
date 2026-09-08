"""Route planner objectives to evidence_plan and analysis_plan payloads."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from tools.langchain_tools import get_tools_list
from tools.query_routing import parse_query_slots, route_candidates
from agents.agent_registry import (
    ANALYSIS_ASPECT_LABELS,
    ANALYSIS_TABLE_ALLOWLIST,
    is_analysis_agent,
)
from config.runtime_policy import (
    DEFAULT_POLICY,
    active_policy,
    evidence_augmentation_enabled,
    router_direct_bypass_enabled,
    shadow_routing,
)
from agents.line_item_matcher import (
    DIRECT_LINE_ITEM_CALCULATION_PATTERNS,
    DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS,
    contains_intent,
    direct_line_item_match,
)
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import (
    ALLOWED_KEYWORDS,
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    build_allowed_keywords_payload,
    iter_keyword_table_pairs,
    normalize_keyword_synonyms,
)
from graph.logger import make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt
from schemas.agent_outputs import AnalysisPlanItem, EvidenceDispatchPlan, EvidencePlanItem, Target
from schemas.table_names import normalize_table_heading
from agents.prompts import PROMPT_TEMPLATE
from common import dedupe_keep_order as _dedupe_keep_order

MAX_TARGET_REQUIREMENTS = DEFAULT_POLICY.execution.max_target_requirements
OPTIONAL_ROUTER_REQUIREMENTS = {
    "chi phí bán hàng",
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
ROUTER_EVALUATIVE_INTENT_PATTERNS = [
    *DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS,
    r"\bphân tích\b",
    r"\bphan tich\b",
]
ROUTER_CALCULATION_INTENT_PATTERNS = DIRECT_LINE_ITEM_CALCULATION_PATTERNS
COMPACT_ROUTER_SYSTEM_INSTRUCTION = """Bạn là Evidence Router cho truy vấn BCTC easy/medium.

NHIỆM VỤ:
- Trả duy nhất JSON EvidenceDispatchPlan: {"evidence_plan":[...],"analysis_plan":[]}.
- evidence_plan gồm các item {table, query, needby}.
- Router có thể xuất từng query riêng; hệ thống sẽ tự compact các query cùng table + needby sau chuẩn hóa.
- Với easy/medium, analysis_plan luôn là [].

QUY TẮC QUERY:
- Mỗi query là đúng 1 khoản mục/line-item, 1 chủ đề thuyết minh ngắn, hoặc 1 chủ đề phần đầu báo cáo.
- Không gộp nhiều khoản mục trong cùng một query.
- Với 3 báo cáo chính, ưu tiên keyword có trong allowed_keywords_json.
- Nếu user/planner cần nhiều biến đầu vào, tạo nhiều evidence item.
- Với easy/medium, note_ref đi kèm line fact chỉ là tham chiếu nguồn; không tạo query thuyết minh chỉ vì có note_ref.
- Dùng "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH" khi user hỏi thông tin không phải line-item/bảng số liệu BCTC như thông tin công ty, địa chỉ/trụ sở, hoạt động kinh doanh, giấy đăng ký doanh nghiệp, chuẩn mực/chế độ kế toán áp dụng, công ty/đơn vị kiểm toán, báo cáo Ban Tổng Giám đốc, HĐQT/Ban TGĐ/Ban kiểm soát, kế toán trưởng, báo cáo kiểm toán/soát xét, ý kiến/kết luận, vấn đề cần nhấn mạnh, người ký/ngày ký.
- Chỉ dùng bảng thuyết minh khi chính user/planner yêu cầu thuyết minh/chính sách/chi tiết khoản mục và router tạo evidence item table="THUYẾT MINH BÁO CÁO TÀI CHÍNH".

MAP BẢNG:
- Tài sản, nợ phải trả, vốn chủ sở hữu, hàng tồn kho, phải thu, tiền, đầu tư tài chính, tài sản cố định -> "BẢNG CÂN ĐỐI KẾ TOÁN".
- Doanh thu, giá vốn, lợi nhuận, chi phí, EPS -> "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH".
- Dòng tiền, lưu chuyển tiền, tiền đầu kỳ/cuối kỳ -> "BÁO CÁO LƯU CHUYỂN TIỀN TỆ".
- Thuyết minh, chính sách kế toán, chi tiết khoản mục, bên liên quan, cam kết, rủi ro tài chính -> "THUYẾT MINH BÁO CÁO TÀI CHÍNH".
- Chi tiết theo một khoản đầu tư/đơn vị cụ thể (DỰ PHÒNG giảm giá/tổn thất, GIÁ GỐC, giá trị hợp lý, tỷ lệ sở hữu...) -> "THUYẾT MINH BÁO CÁO TÀI CHÍNH". Giữ nguyên entity và metric/value type trong query để khớp typed metadata; không giả định tên doanh nghiệp hay số thuyết minh. Chỉ dùng báo cáo chính khi user hỏi số tổng hợp.
- CẶP DỄ NHẦM — chọn đúng hướng, không lấy keyword "gần giống":
  - "TRẢ TRƯỚC cho người bán" (tài sản, mình trả trước cho nhà cung cấp) KHÁC "PHẢI TRẢ người bán" (nợ phải trả). Hỏi trả trước -> keyword "trả trước cho người bán ngắn hạn/dài hạn".
  - "Chi phí TRẢ TRƯỚC" (tài sản chờ phân bổ: hoa hồng môi giới, thưởng bán hàng, công cụ dụng cụ…) KHÁC "chi phí PHẢI TRẢ" (nợ trích trước). Hỏi chi phí trả trước/hoa hồng môi giới/công cụ dụng cụ -> "chi phí trả trước ngắn hạn/dài hạn".
  - "PHẢI THU về CHO VAY" (mình CHO vay, tài sản) KHÁC "vay và nợ thuê tài chính" (mình ĐI vay, nợ). Hỏi cho vay/thu hồi cho vay/lãi cho vay phải thu -> keyword "phải thu về cho vay ngắn hạn/dài hạn".
- Câu hỏi PER-ĐƠN-VỊ hoặc SO SÁNH/XẾP HẠNG chi tiết ("dự án NÀO", "công ty NÀO", "khoản NÀO", "liệt kê…", "giảm/tăng nhiều nhất") -> "THUYẾT MINH BÁO CÁO TÀI CHÍNH" với query là khoản mục tương ứng: chi tiết per-dự-án/per-công-ty/per-khoản-vay chỉ có trong note; báo cáo chính chỉ có số tổng.
- Tình trạng hoạt động của công ty con/đơn vị được đầu tư (bị lỗ, chờ giải thể, chưa hoạt động, tỷ lệ sở hữu) -> "THUYẾT MINH BÁO CÁO TÀI CHÍNH", query "tỷ lệ sở hữu của công ty tại các đơn vị" hoặc "tình trạng hoạt động công ty con".
- Thông tin công ty, địa chỉ/trụ sở chính, hoạt động kinh doanh chính, giấy đăng ký doanh nghiệp, chuẩn mực/chế độ kế toán áp dụng, tuyên bố tuân thủ chuẩn mực kế toán, Báo cáo của Ban Tổng Giám đốc/Ban Giám đốc, HĐQT/Ban TGĐ/Ban kiểm soát, ban điều hành, kế toán trưởng, báo cáo kiểm toán độc lập, báo cáo soát xét, ý kiến kiểm toán, kết luận soát xét, vấn đề cần nhấn mạnh, kiểm toán viên, công ty/đơn vị/hãng kiểm toán, người ký/ngày ký -> "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH".

OUTPUT:
- Chỉ JSON hợp lệ, không markdown, không giải thích.
- Evidence item cho BCTC phải có table hợp lệ và query không rỗng.
"""
VALID_EVIDENCE_TABLES = {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION}


def _normalize_evidence_table(value: Any) -> str:
    table = normalize_table_heading(str(value or "").strip())
    if table in VALID_EVIDENCE_TABLES:
        return table
    return ""


def _table_from_route_payload(item: dict) -> str:
    if not isinstance(item, dict):
        return ""

    table = _normalize_evidence_table(item.get("table", ""))
    if table:
        return table

    for query in _evidence_queries_from_raw_item(item):
        candidates = route_candidates(query)
        if candidates and candidates[0].confidence >= 0.60:
            return candidates[0].table

    return ""


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
    """Route candidates over the static + dataset-derived keyword vocabulary.

    Rebuilt lazily (cached per vocabulary snapshot) instead of at import time so
    keywords registered by ensure_built via set_dynamic_keywords are visible to
    the followup router.
    """
    pairs = iter_keyword_table_pairs()
    cache_key = len(pairs)
    cached = _ROUTE_CANDIDATES_CACHE.get("specs")
    if cached is not None and _ROUTE_CANDIDATES_CACHE.get("key") == cache_key:
        return cached

    candidates = []
    for keyword, table in pairs:
        candidates.append(
            {
                "table": table,
                "match_text": str(keyword or "").strip().lower(),
                "tokens": _text_tokens(keyword),
            }
        )

    _ROUTE_CANDIDATES_CACHE["key"] = cache_key
    _ROUTE_CANDIDATES_CACHE["specs"] = candidates
    return candidates


_ROUTE_CANDIDATES_CACHE: dict = {}
MAIN_REPORT_TABLE_ORDER = (TABLE_BS, TABLE_IS, TABLE_CF)
MAIN_REPORT_TABLES = set(MAIN_REPORT_TABLE_ORDER)


def _requires_note_followup(requirement: str) -> bool:
    return any(
        candidate.table == TABLE_NOTE and candidate.confidence >= 0.80
        for candidate in route_candidates(requirement)
    )


def _requires_report_section_followup(requirement: str) -> bool:
    candidates = route_candidates(requirement)
    return bool(
        candidates
        and candidates[0].table == TABLE_REPORT_SECTION
        and candidates[0].confidence >= 0.85
    )


def _direct_line_item_evidence_from_query(user_query: str) -> list[dict]:
    if _requires_note_followup(user_query) or _requires_report_section_followup(user_query):
        return []

    raw_query = str(user_query or "").strip()
    match = direct_line_item_match(
        raw_query,
        selected_tables=MAIN_REPORT_TABLE_ORDER,
        evaluative_patterns=ROUTER_EVALUATIVE_INTENT_PATTERNS,
        calculation_patterns=ROUTER_CALCULATION_INTENT_PATTERNS,
    )
    if match is None:
        return []

    return [
        {
            "table": match["table"],
            "query": raw_query or match["canonical"],
            "canonical_query": match["canonical"],
            "search_query": raw_query or match["canonical"],
            "needby": [],
        }
    ]


def _entity_evidence_from_query(user_query: str) -> list[dict]:
    """Fetch named entities only from deterministically supported tables.

    The former fallback queried BS, IS, CF, and NOTE for every hard question,
    which inflated latency and let unrelated rows outrank the target.
    """
    raw_query = str(user_query or "").strip()
    if not raw_query:
        return []

    calculation_items = _calculation_operand_evidence_items(raw_query)
    if calculation_items:
        return calculation_items

    items: list[dict] = []
    seen_tables: set[str] = set()

    def _add(table: str) -> None:
        table = str(table or "").strip()
        if table and table not in seen_tables:
            seen_tables.add(table)
            items.append({"table": table, "query": raw_query, "needby": []})

    for candidate in route_candidates(raw_query):
        if candidate.confidence >= 0.60:
            _add(candidate.table)

    return items


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

    # Substring matches score high. Token-only matches need enough coverage to
    # avoid rewriting broad follow-up requirements into unrelated line items.
    if best_keyword and best_score >= 5.0:
        return best_keyword

    return str(requirement or "").strip()


def _upgrade_query_to_user_keyword(query: str, table: str, user_query: str) -> str:
    """Prefer the fuller line-item the user actually named.

    The LLM router often narrows a query by dropping a distinguishing qualifier
    ("tiền và các khoản tương đương tiền" -> "các khoản tương đương tiền",
    "phải trả dài hạn khác" -> "phải trả khác"), which then retrieves the wrong
    (sub-)line. If the user query contains a longer allowed-keyword for this table
    that *contains* the narrowed query, restore the longer keyword.
    """
    q_norm = str(query or "").strip().lower()
    uq_norm = str(user_query or "").strip().lower()
    if not q_norm or not uq_norm:
        return query
    q_tokens = _text_tokens(q_norm)
    if not q_tokens:
        return query
    allowed = ALLOWED_KEYWORDS.get(table, set()) or set()
    best = query
    best_len = len(q_norm)
    for keyword in allowed:
        kw_norm = str(keyword or "").strip().lower()
        if not kw_norm or len(kw_norm) <= best_len:
            continue
        # The keyword must be one the user actually mentioned, and must be a
        # more-specific superset of the narrowed query (all its tokens, plus the
        # dropped qualifier). Token-superset catches non-contiguous qualifiers
        # ("phải trả khác" -> "phải trả dài hạn khác").
        if kw_norm in uq_norm and q_tokens <= _text_tokens(kw_norm):
            best = keyword
            best_len = len(kw_norm)
    return best


def _restore_user_keywords_in_evidence_plan(evidence_plan: list[dict], user_query: str) -> list[dict]:
    restored = []
    for item in evidence_plan or []:
        if not isinstance(item, dict):
            continue
        new_item = dict(item)
        upgraded = _upgrade_query_to_user_keyword(
            str(item.get("query", "") or ""),
            str(item.get("table", "") or ""),
            user_query,
        )
        if upgraded and upgraded != item.get("query"):
            new_item["query"] = upgraded
        restored.append(new_item)
    return restored


def _matching_main_report_keywords(requirement: str) -> dict[str, list[str]]:
    requirement = normalize_keyword_synonyms(requirement)
    matches: dict[str, list[str]] = {}
    for table in MAIN_REPORT_TABLE_ORDER:
        allowed = ALLOWED_KEYWORDS.get(table, set()) or set()
        for keyword in sorted(
            allowed,
            key=lambda item: (
                requirement.find(str(item).lower())
                if str(item).lower() in requirement
                else len(requirement) + 1,
                -len(str(item)),
                str(item),
            ),
        ):
            if _keyword_match_score(requirement, keyword) >= 60.0:
                matches.setdefault(table, []).append(keyword)

    return {
        table: _dedupe_keep_order(requirements)
        for table, requirements in matches.items()
        if requirements
    }


def _supplemental_main_report_table(table: str, query_text: str) -> str:
    """Recall-first: when a NOTE / report-section query also names a main-report
    line item, return the BS/IS/CF table to pull alongside it (the primary line
    carries the headline figure / total). Returns "" when nothing applies or the
    query is already routed to a main-report table."""
    if table in MAIN_REPORT_TABLES:
        return ""
    return _main_report_route_for_requirement(query_text) or ""


def _main_report_route_for_requirement(requirement: str) -> Optional[str]:
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
    return best_table


def _normalize_main_report_followup_requirements(requirement: str, table: str) -> list[str]:
    matches = _matching_main_report_keywords(requirement)
    table_matches = matches.get(table, [])
    if table_matches:
        return table_matches
    return [_normalize_main_report_followup_requirement(requirement, table)]


def _table_for_allowed_query(query: str) -> str:
    return _main_report_route_for_requirement(query) or ""


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
    if table in {TABLE_NOTE, TABLE_REPORT_SECTION}:
        return _compact_note_followup_requirement(requirement)
    return str(requirement or "").strip()


def _normalize_followup_requirements_for_target(requirement: str, table: str) -> list[str]:
    if table in MAIN_REPORT_TABLES:
        return _normalize_main_report_followup_requirements(requirement, table)
    normalized = _normalize_followup_requirement_for_target(requirement, table)
    return [normalized] if normalized else []


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


def _evidence_item_queries(item: dict) -> list[str]:
    return _evidence_queries_from_raw_item(item)


def _first_query_map_value(item: dict, map_key: str, scalar_key: str, query: str) -> str:
    values = item.get(map_key)
    if isinstance(values, dict):
        direct = str(values.get(query, "") or "").strip()
        if direct:
            return direct
    return str(item.get(scalar_key, "") or "").strip()


def _followup_route_hints_from_plan(planner_plan: dict) -> dict[str, str]:
    hints: dict[str, str] = {}
    for item in (planner_plan.get("followup_requests", []) or []):
        if not isinstance(item, dict):
            continue
        table = _table_from_route_payload(item)
        if not table:
            continue
        for requirement in _dedupe_keep_order(item.get("requirements", []) or []):
            hints[str(requirement or "").strip()] = table
    return hints


def _followup_route_hints_from_worker_plan(worker_plan: dict) -> dict[str, str]:
    hints: dict[str, str] = {}
    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        table = _table_from_route_payload(item)
        if not table:
            continue
        for query in _evidence_item_queries(item):
            hints[query] = table

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        table = _table_from_route_payload(target)
        if not table:
            continue
        for requirement in _dedupe_keep_order(target.get("requirements", []) or []):
            hints[str(requirement or "").strip()] = table
    return hints


def _heuristic_followup_route(requirement: str) -> str:
    candidates = route_candidates(requirement)
    if candidates and candidates[0].confidence >= 0.60:
        return candidates[0].table
    # Unknown follow-ups remain unscoped instead of silently querying IS.
    return ""


def _route_followup_requirement(requirement: str, hint: Optional[str] = None) -> str:
    normalized_requirement = str(requirement or "").strip().lower()
    candidates = route_candidates(normalized_requirement)
    if candidates and candidates[0].confidence >= 0.60:
        return candidates[0].table

    if hint:
        hinted_table = _normalize_evidence_table(hint)
        if hinted_table:
            return hinted_table

    req_tokens = _text_tokens(normalized_requirement)
    best_candidate = None
    best_score = 0.0

    for candidate in _candidate_route_specs():
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
        return str(best_candidate.get("table", "") or "").strip()

    return _heuristic_followup_route(normalized_requirement)


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
    grouped_requirements: dict[str, list[str]] = {}

    for requirement in followup_requirements:
        hint = route_hints.get(requirement)
        routed_table = _route_followup_requirement(requirement, hint=hint)
        normalized_requirements = _normalize_followup_requirements_for_target(
            requirement,
            routed_table,
        )
        grouped_requirements.setdefault(routed_table, [])
        grouped_requirements[routed_table].extend(normalized_requirements)

    table_targets = []
    for table, requirements in grouped_requirements.items():
        table_targets.append(
            {
                "table": table,
                "requirements": _dedupe_keep_order(requirements)[:MAX_TARGET_REQUIREMENTS],
                "source": "followup",
            }
        )

    evidence_plan = _merge_evidence_plans(
        _evidence_plan_from_table_targets(
            table_targets,
            [],
        )
    )

    return {
        "evidence_plan": _compact_evidence_plan_by_table_needby(evidence_plan),
        "analysis_plan": [],
        "targets": [],
    }

def _split_target_requirement_item(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    parts = [item.strip() for item in re.split(r"\s*[;,]\s*", text) if item.strip()]
    if len(parts) <= 1:
        return [text]
    return parts


def _normalize_target_requirements(requirements: list[str]) -> list[str]:
    expanded = []
    for item in requirements or []:
        expanded.extend(_split_target_requirement_item(item))
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
        queries = _evidence_item_queries(item)
        if not queries:
            continue
        payload = {
            "table": str(item.get("table", "") or "").strip(),
            "needby": [
                str(agent).strip()
                for agent in (item.get("needby", []) or item.get("needed_by", []) or [])
                if str(agent).strip()
            ][:3],
        }
        if len(queries) == 1:
            payload["query"] = queries[0]
        else:
            payload["queries_n"] = len(queries)
            payload["queries"] = queries[:5]
        items.append(
            {
                key: value
                for key, value in payload.items()
                if key == "needby" or value not in ("", None, [], {})
            }
        )
    return items[:8]


def _router_evidence_query_count(worker_plan: dict) -> int:
    return sum(
        len(_evidence_item_queries(item))
        for item in (worker_plan.get("evidence_plan", []) or [])
        if isinstance(item, dict)
    )


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

    table = _table_from_route_payload(data)
    if table:
        data["table"] = table

    return data


def _normalize_target_payload(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None

    item = _repair_target_route_payload(item)
    agent = str(item.get("agent", "") or "").strip()
    if is_analysis_agent(agent):
        try:
            target = Target.model_validate(item)
        except ValidationError:
            return None

        payload = target.model_dump(exclude_none=True)
        requirements = payload.get("requirements", []) or []
        requirements = _dedupe_keep_order(requirements)
        payload["requirements"] = requirements[:MAX_TARGET_REQUIREMENTS]
        if not payload["requirements"]:
            return None
        return payload

    table = _table_from_route_payload(item)
    requirements = _normalize_target_requirements(_evidence_queries_from_raw_item(item))
    if not table or not requirements:
        return None

    payload = {
        "table": table,
        "requirements": requirements[:MAX_TARGET_REQUIREMENTS],
    }
    source = str(item.get("source", "") or "").strip()
    if source:
        payload["source"] = source
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


def _typed_calculation_metadata(query: str, table: str) -> dict:
    """Build a contract only when every operand is explicit and direction is fixed."""

    slots = parse_query_slots(query)
    coverage_metadata = {
        "coverage_template": slots.coverage_template,
        "required_legs": list(slots.required_legs),
    } if slots.coverage_template and slots.required_legs else {}
    if slots.operation in {"ratio", "share"} and len(slots.operands) == 2:
        operands = []
        matrix_ratio = _is_multi_value_type_ratio(slots)
        note_share_schedule = bool(
            slots.operation == "share"
            and _ratio_operands_share_schedule(slots)
        )
        for operand in slots.operands:
            operand_table = _operand_contract_table(
                slots,
                operand,
                fallback_table=table,
            )
            if matrix_ratio or note_share_schedule:
                operand_table = TABLE_NOTE
            operands.append(
                {
                    "role": operand.role,
                    "query": operand.query,
                    "metric": operand.metric,
                    "entity": operand.entity,
                    "period": operand.period,
                    "period_role": operand.period_role,
                    "reporting_basis": operand.reporting_basis,
                    "period_label": operand.period_label,
                    "value_type": operand.value_type,
                    "aggregation_level": operand.aggregation,
                    "scope_label": operand.scope_label,
                    "counterparty": operand.counterparty,
                    "transaction_type": operand.transaction_type,
                    "movement_type": operand.movement_type,
                    "geography": operand.geography,
                    "policy_topic": operand.policy_topic,
                    "section_key": operand.section_key,
                    **({"table": operand_table} if operand_table else {}),
                }
            )
        return {
            **coverage_metadata,
            "operation": slots.operation,
            "operands": operands,
        }

    if (
        not slots.metric
        or not _two_period_operand_specs(query, slots)
        or len(slots.value_type) > 1
        or slots.operation
        not in {"delta", "percent_change", "multiple"}
    ):
        return coverage_metadata

    common = {
        "query": str(query or "").strip(),
        "metric": slots.metric,
        "entity": slots.entity,
        "value_type": slots.value_type[0] if slots.value_type else "",
        "aggregation_level": slots.aggregation,
        "scope_label": slots.scope_label,
        "counterparty": slots.counterparty,
        "transaction_type": slots.transaction_type,
        "movement_type": slots.movement_type,
        "geography": slots.geography,
        "policy_topic": slots.policy_topic,
        "section_key": slots.section_key,
        "reporting_basis": slots.reporting_basis,
    }
    if table:
        common["table"] = table
    roles = _two_period_operand_specs(query, slots)

    return {
        **coverage_metadata,
        "operation": slots.operation,
        "operands": [
            {
                **common,
                "role": role,
                "query": operand_query,
                "period": period,
                "period_label": (
                    (parse_query_slots(operand_query).period_labels or ("",))[0]
                ),
                "reporting_basis": (
                    parse_query_slots(operand_query).reporting_basis
                    or slots.reporting_basis
                ),
                **({"period_role": period_role} if period_role else {}),
            }
            for role, operand_query, period, period_role in roles
        ],
    }


def _two_period_operand_specs(query: str, slots) -> tuple[tuple[str, str, str, str], ...]:
    """Return ordered, self-contained current/previous retrieval legs."""

    temporal_specs: tuple[tuple[str, str, str, str], ...] = ()
    if slots.period_role == "both":
        temporal_specs = (
            ("current", "năm nay", "", "current"),
            ("previous", "năm trước", "", "previous"),
        )
    elif slots.period == "both":
        temporal_specs = (
            ("current", "cuối kỳ", "cuối", ""),
            ("previous", "đầu kỳ", "đầu", ""),
        )
    elif len(slots.period_labels) >= 2:
        first, second = slots.period_labels[:2]
        # "từ A đến B" declares B - A.  Other common forms name the target
        # first ("2025 so với 2024", "2025 và 2024").
        if re.search(
            r"\b(?:từ|tu)\b.+\b(?:đến|den)\b",
            str(query or "").casefold(),
        ):
            current_label, previous_label = second, first
        else:
            current_label, previous_label = first, second
        temporal_specs = (
            ("current", current_label, "", "current"),
            ("previous", previous_label, "", "previous"),
        )
    if not temporal_specs:
        return ()

    base_parts = [slots.metric]
    if (
        slots.aggregation == "total"
        and not re.search(
            r"\b(?:tổng|tong)\b",
            str(slots.metric or "").casefold(),
        )
    ):
        base_parts.insert(0, "tổng")
    for value in (
        slots.entity,
        slots.scope_label,
        slots.counterparty,
        slots.geography,
    ):
        if str(value or "").strip():
            base_parts.append(str(value).strip())
    if slots.value_type:
        base_parts.extend(slots.value_type)

    base = " ".join(_dedupe_keep_order(base_parts)).strip()
    return tuple(
        (
            role,
            " ".join(part for part in (base, label) if part).strip(),
            period,
            period_role,
        )
        for role, label, period, period_role in temporal_specs
    )


def _is_multi_value_type_ratio(slots) -> bool:
    operands = tuple(getattr(slots, "operands", ()) or ())
    if len(operands) != 2:
        return False
    metrics = {str(operand.metric or "").strip().casefold() for operand in operands}
    value_types = {
        str(operand.value_type or "").strip().casefold()
        for operand in operands
        if str(operand.value_type or "").strip()
    }
    return len(metrics) == 1 and "" not in metrics and len(value_types) == 2


def _strong_operand_route_tables(operand) -> list[str]:
    return _dedupe_keep_order(
        [
            candidate.table
            for candidate in route_candidates(operand.query)
            if candidate.confidence >= 0.60
        ]
    )[:2]


def _operand_contract_table(slots, operand, *, fallback_table: str = "") -> str:
    """Pin a calculation leg only when its semantic route is unambiguous."""

    if _is_multi_value_type_ratio(slots):
        return TABLE_NOTE
    strong_tables = _strong_operand_route_tables(operand)
    return strong_tables[0] if len(strong_tables) == 1 else ""


def _operand_route_tables(slots, operand, *, fallback_table: str = "") -> list[str]:
    """Return a bounded route set for one calculation leg.

    A same-metric, multi-value-type ratio is a matrix/schedule lookup.  Its
    detailed cost/depreciation columns normally live in NOTE, while the main
    statement route is retained as a bounded fallback.
    """

    tables = []
    if _is_multi_value_type_ratio(slots):
        tables.append(TABLE_NOTE)
    strong_tables = _strong_operand_route_tables(operand)
    for candidate_table in strong_tables:
        if candidate_table not in tables:
            tables.append(candidate_table)
    # A specific share numerator that has no confident statement route is
    # normally a schedule component.  Keep NOTE as a bounded candidate while
    # retaining the statement fallback; this is not a global note expansion.
    if (
        not strong_tables
        and slots.operation == "share"
        and operand.role == "numerator"
        and TABLE_NOTE not in tables
    ):
        tables.append(TABLE_NOTE)
    fallback = _normalize_evidence_table(fallback_table)
    if fallback and fallback not in tables:
        tables.append(fallback)
    return tables[:2]


def _ratio_operands_share_schedule(slots) -> bool:
    if len(slots.operands) != 2:
        return False
    numerator, denominator = slots.operands
    left = {
        token
        for token in _text_tokens(numerator.metric)
        if token not in {"tong", "cong"}
    }
    right = {
        token
        for token in _text_tokens(denominator.metric)
        if token not in {"tong", "cong"}
    }
    return bool(left and right and left.intersection(right))


def _ratio_operand_evidence_items(
    query: str,
    *,
    needby: list[str] | None = None,
    fallback_table: str = "",
) -> list[dict]:
    slots = parse_query_slots(query)
    if slots.operation not in {"ratio", "share"} or len(slots.operands) != 2:
        return []

    contract = _typed_calculation_metadata(query, fallback_table).get("operands", [])
    if len(contract) != 2:
        return []
    needed_by = _dedupe_keep_order(needby or [])
    tables_by_role = {
        operand.role: _operand_route_tables(
            slots,
            operand,
            fallback_table=fallback_table,
        )
        for operand in slots.operands
    }
    if (
        _ratio_operands_share_schedule(slots)
        and TABLE_NOTE in tables_by_role.get("numerator", [])
        and TABLE_NOTE not in tables_by_role.get("denominator", [])
    ):
        tables_by_role["denominator"] = (
            tables_by_role.get("denominator", []) + [TABLE_NOTE]
        )[:2]

    items = []
    for operand in slots.operands:
        for table in tables_by_role.get(operand.role, []):
            items.append(
                {
                    "table": table,
                    "query": operand.query,
                    "canonical_query": operand.metric,
                    "search_query": operand.query,
                    "needby": needed_by,
                    "operation": slots.operation,
                    "operand_role": operand.role,
                    "operands": contract,
                    "period": operand.period,
                    "period_role": operand.period_role,
                    "reporting_basis": operand.reporting_basis,
                    "period_label": operand.period_label,
                    "value_type": operand.value_type,
                    "scope_label": operand.scope_label,
                    "counterparty": operand.counterparty,
                    "transaction_type": operand.transaction_type,
                    "movement_type": operand.movement_type,
                    "geography": operand.geography,
                    "policy_topic": operand.policy_topic,
                    "section_key": operand.section_key,
                }
            )
    return items


def _period_operand_evidence_items(
    query: str,
    *,
    needby: list[str] | None = None,
    fallback_table: str = "",
) -> list[dict]:
    slots = parse_query_slots(query)
    if slots.operation not in {"delta", "percent_change", "multiple"}:
        return []
    fallback = _normalize_evidence_table(fallback_table)
    if not fallback:
        fallback = next(
            (
                candidate.table
                for candidate in route_candidates(query)
                if candidate.confidence >= 0.60
            ),
            "",
        )
    metadata = _typed_calculation_metadata(query, fallback)
    contract = metadata.get("operands", [])
    if len(contract) != 2:
        return []

    needed_by = _dedupe_keep_order(needby or [])

    items = []
    for operand in contract:
        operand_query = str(operand.get("query", "") or "").strip()
        tables = _dedupe_keep_order(
            [
                candidate.table
                for candidate in route_candidates(operand_query)
                if candidate.confidence >= 0.60
            ]
            + ([fallback] if fallback else [])
        )[:2]
        for table in tables:
            items.append(
                {
                    "table": table,
                    "query": operand_query,
                    "canonical_query": str(operand.get("metric", "") or "").strip(),
                    "search_query": operand_query,
                    "needby": needed_by,
                    "operation": slots.operation,
                    "operand_role": operand.get("role", ""),
                    "operands": contract,
                    "period": operand.get("period", ""),
                    "period_role": operand.get("period_role", ""),
                    "reporting_basis": operand.get("reporting_basis", ""),
                    "period_label": operand.get("period_label", ""),
                    "value_type": operand.get("value_type", ""),
                    "scope_label": operand.get("scope_label", ""),
                    "counterparty": operand.get("counterparty", ""),
                    "transaction_type": operand.get("transaction_type", ""),
                    "movement_type": operand.get("movement_type", ""),
                    "geography": operand.get("geography", ""),
                    "policy_topic": operand.get("policy_topic", ""),
                    "section_key": operand.get("section_key", ""),
                }
            )
    return items


def _calculation_operand_evidence_items(
    query: str,
    *,
    needby: list[str] | None = None,
    fallback_table: str = "",
) -> list[dict]:
    ratio_items = _ratio_operand_evidence_items(
        query,
        needby=needby,
        fallback_table=fallback_table,
    )
    if ratio_items:
        return ratio_items
    return _period_operand_evidence_items(
        query,
        needby=needby,
        fallback_table=fallback_table,
    )


def _expand_ratio_evidence_items(items: list[Any]) -> list[dict]:
    expanded = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        queries = _evidence_queries_from_raw_item(item) or [""]
        for query in queries:
            candidate = dict(item)
            candidate["query"] = query
            calculation_items = []
            if not str(candidate.get("operand_role", "") or "").strip():
                calculation_items = _calculation_operand_evidence_items(
                    query,
                    needby=_needby_values(candidate),
                    fallback_table=_table_from_route_payload(candidate),
                )
            if calculation_items:
                source = str(candidate.get("source", "") or "").strip()
                for calculation_item in calculation_items:
                    if source:
                        calculation_item["source"] = source
                    expanded.append(calculation_item)
            else:
                expanded.append(candidate)
    return expanded


def _normalize_evidence_plan_payloads(items: list[Any]) -> list[dict]:
    normalized: list[dict] = []
    merged_by_key: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []

    for item in _expand_ratio_evidence_items(items):
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
            for field in (
                "web_intent",
                "canonical_query",
                "search_query",
                "period",
                "period_role",
                "period_label",
                "value_type",
                "scope_label",
                "counterparty",
                "transaction_type",
                "movement_type",
                    "geography",
                    "policy_topic",
                    "section_key",
                    "source",
            ):
                if item.get(field) not in ("", None, [], {}):
                    payload[field] = item.get(field)
            table = (
                _normalize_evidence_table(payload.get("table", ""))
                or _table_for_allowed_query(payload.get("query", ""))
            )
            raw_query_text = str(payload.get("query", "") or "").strip()
            query_text = (
                raw_query_text
                if str(payload.get("operand_role", "") or "").strip()
                else _normalize_followup_requirement_for_target(raw_query_text, table)
            )
            if not query_text:
                continue

            needby = _needby_values(payload)

            # Recall-first routing: a NOTE / report-section query that ALSO names
            # a main-report line item (e.g. "tiền và các khoản tương đương tiền")
            # must still pull the primary BS/IS/CF line, where the headline figure
            # and the total live. The LLM often routes such queries to THUYẾT MINH
            # only, so the code-110/410 total never enters the candidate pool. We
            # add the main-report table alongside (never replacing) the chosen one.
            tables_for_query = [table]
            supplemental = _supplemental_main_report_table(table, query_text)
            if supplemental and supplemental != table:
                tables_for_query.append(supplemental)
            # Recall-first augmentation: never replaces the table the router
            # chose, only also fetches the main-report line the query names.
            # Measured load-bearing (A/B 2026-09-03: disabling it cut cash-flow
            # facts 39 -> 1), so it is independent of the model_first switch.
            if evidence_augmentation_enabled():
                threshold = active_policy().routing.fallback_confidence_threshold
                for route in route_candidates(query_text):
                    if route.confidence >= threshold and route.table not in tables_for_query:
                        tables_for_query.append(route.table)

            for plan_table in tables_for_query:
                key = (plan_table, query_text)
                if key not in merged_by_key:
                    merged_by_key[key] = {
                        "table": plan_table,
                        "query": query_text,
                        "needby": [],
                    }
                    canonical_query = str(payload.get("canonical_query", "") or "").strip()
                    search_query = str(payload.get("search_query", "") or "").strip()
                    if canonical_query and canonical_query != query_text:
                        merged_by_key[key]["canonical_query"] = canonical_query
                    if search_query and search_query != query_text:
                        merged_by_key[key]["search_query"] = search_query
                    order.append(key)

                calculation_metadata = {
                    field: payload.get(field)
                    for field in (
                        "operation",
                        "operand_role",
                        "operands",
                        "period",
                        "period_role",
                        "period_label",
                        "value_type",
                        "scope_label",
                        "counterparty",
                        "transaction_type",
                        "movement_type",
                        "geography",
                        "policy_topic",
                        "section_key",
                        "source",
                    )
                    if payload.get(field) not in ("", None, [], {})
                }
                if not calculation_metadata:
                    calculation_metadata = _typed_calculation_metadata(
                        str(payload.get("query", "") or query_text), plan_table
                    )
                for field, value in calculation_metadata.items():
                    if merged_by_key[key].get(field) in ("", None, [], {}):
                        merged_by_key[key][field] = value

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


def _planner_axis_agents(planner_plan: dict) -> list[str]:
    """Analysis agents the planner actually chose, in planner order."""

    order: list[str] = []
    for axis in ((planner_plan or {}).get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        agent = str(axis.get("axis", "") or "").strip()
        if is_analysis_agent(agent) and agent not in order:
            order.append(agent)
    return order


def reconcile_analysis_plan_coverage(
    analysis_plan: list[dict],
    planner_plan: dict,
) -> tuple[list[dict], list[str]]:
    """Make the analysis plan cover exactly the planner's axes.

    A planner axis with no analysis item is an agent that will never run, and
    the final answer would then be missing that aspect with nothing in the trace
    to explain it.  Missing agents are materialized from the planner's own axis
    and objective; no agent is inferred beyond what the model chose.
    """

    axis_agents = _planner_axis_agents(planner_plan)
    if not axis_agents:
        return list(analysis_plan or []), []

    by_agent: dict[str, dict] = {}
    for item in analysis_plan or []:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if agent and agent not in by_agent:
            by_agent[agent] = dict(item)

    planned_by_agent = {
        target["agent"]: target for target in _planner_analysis_targets(planner_plan)
    }

    notes: list[str] = []
    reconciled: list[dict] = []
    for agent in axis_agents:
        item = by_agent.get(agent)
        if item is None:
            planned = planned_by_agent.get(agent, {})
            objective = str(planned.get("objective", "") or "").strip()
            if not objective:
                objective = "; ".join(planned.get("objectives", []) or [])
            if not objective:
                objective = ANALYSIS_ASPECT_LABELS.get(agent, agent)
            item = {"agent": agent, "objective": objective, "evidence_queries": []}
            notes.append(f"materialized_from_axis:{agent}")
        reconciled.append(item)

    for agent in by_agent:
        if agent not in axis_agents:
            notes.append(f"dropped_unplanned_agent:{agent}")

    return reconciled, notes


def _user_query_mentions_optional_requirement(user_query: str, requirement: str) -> bool:
    query_text = str(user_query or "").strip().lower()
    requirement_text = str(requirement or "").strip().lower()
    if not query_text or not requirement_text:
        return False
    return requirement_text in query_text


def _filter_optional_table_requirements(
    table_targets: list[dict],
    user_query: str,
) -> list[dict]:
    filtered_targets: list[dict] = []

    for target in table_targets or []:
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


def _merge_table_targets(table_targets: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    def add_target(table: str, requirements: list[str]) -> None:
        table_name = _normalize_evidence_table(table)
        if not table_name:
            return

        clean_requirements = _dedupe_keep_order(
            [str(item).strip() for item in requirements or [] if str(item).strip()]
        )
        if not clean_requirements:
            return

        if table_name not in merged:
            merged[table_name] = {
                "table": table_name,
                "requirements": [],
            }
            order.append(table_name)

        merged[table_name]["requirements"] = _dedupe_keep_order(
            list(merged[table_name].get("requirements", []) or []) + clean_requirements
        )[:MAX_TARGET_REQUIREMENTS]

    for target in table_targets or []:
        add_target(
            _table_from_route_payload(target),
            target.get("requirements", []) or [],
        )

    return [merged[key] for key in order]


_ANALYSIS_QUERY_FOCUS_MARKERS = {
    "agent_profitability": (
        "doanh thu",
        "giá vốn",
        "lợi nhuận",
        "thu nhập",
        "chi phí",
        "eps",
        "lãi cơ bản trên cổ phiếu",
        "tổng cộng tài sản",
        "vốn chủ sở hữu",
    ),
    "agent_liquidity_solvency": (
        "tài sản ngắn hạn",
        "nợ ngắn hạn",
        "nợ dài hạn",
        "nợ phải trả",
        "vốn chủ sở hữu",
        "hàng tồn kho",
        "phải thu",
        "phải trả",
        "tiền và các khoản tương đương tiền",
        "đầu tư tài chính ngắn hạn",
        "chi phí lãi vay",
        "vay",
    ),
    "agent_cashflow_analysis": (
        "lưu chuyển tiền",
        "dòng tiền",
        "tiền thu",
        "tiền chi",
        "trả nợ gốc",
        "đi vay",
        "cổ tức",
        "capex",
        "lợi nhuận sau thuế",
    ),
    "agent_efficiency": (
        "doanh thu",
        "giá vốn",
        "tổng cộng tài sản",
        "tài sản cố định",
        "hàng tồn kho",
        "phải thu",
        "phải trả người bán",
        "chi phí bán hàng",
    ),
}
_PRIMARY_ANALYSIS_AGENT_BY_TABLE = {
    TABLE_BS: "agent_liquidity_solvency",
    TABLE_IS: "agent_profitability",
    TABLE_CF: "agent_cashflow_analysis",
}
_BROAD_PROFITABILITY_ASSESSMENT_PATTERNS = (
    r"\bkha nang sinh loi\b",
    r"\bhieu qua sinh loi\b",
    r"\bprofitability\b",
)
_BROAD_PROFITABILITY_CORE_METRICS = (
    (
        TABLE_IS,
        "doanh thu thuần về bán hàng và cung cấp dịch vụ",
        ("agent_profitability", "agent_efficiency"),
        "flow",
    ),
    (
        TABLE_IS,
        "lợi nhuận gộp về bán hàng và cung cấp dịch vụ",
        ("agent_profitability",),
        "flow",
    ),
    (
        TABLE_IS,
        "lợi nhuận thuần từ hoạt động kinh doanh",
        ("agent_profitability",),
        "flow",
    ),
    (
        TABLE_IS,
        "lợi nhuận sau thuế thu nhập doanh nghiệp",
        ("agent_profitability", "agent_cashflow_analysis"),
        "flow",
    ),
    (
        TABLE_BS,
        "tổng cộng tài sản",
        ("agent_profitability", "agent_efficiency"),
        "stock",
    ),
    (
        TABLE_BS,
        "tổng vốn chủ sở hữu",
        ("agent_profitability", "agent_liquidity_solvency"),
        "stock",
    ),
    (
        TABLE_CF,
        "lưu chuyển tiền thuần từ hoạt động kinh doanh",
        ("agent_cashflow_analysis",),
        "flow",
    ),
)
_COMPREHENSIVE_FINANCIAL_CORE_METRICS = (
    *_BROAD_PROFITABILITY_CORE_METRICS,
    (
        TABLE_IS,
        "giá vốn hàng bán và dịch vụ cung cấp",
        ("agent_profitability", "agent_efficiency"),
        "flow",
    ),
    (
        TABLE_IS,
        "chi phí lãi vay",
        ("agent_liquidity_solvency",),
        "flow",
    ),
    (
        TABLE_BS,
        "tổng tài sản ngắn hạn",
        ("agent_liquidity_solvency",),
        "stock",
    ),
    (
        TABLE_BS,
        "tổng nợ ngắn hạn",
        ("agent_liquidity_solvency",),
        "stock",
    ),
    (
        TABLE_BS,
        "tổng nợ phải trả",
        ("agent_liquidity_solvency",),
        "stock",
    ),
    (
        TABLE_BS,
        "tiền và các khoản tương đương tiền",
        ("agent_liquidity_solvency", "agent_cashflow_analysis"),
        "stock",
    ),
    (
        TABLE_BS,
        "hàng tồn kho",
        ("agent_liquidity_solvency", "agent_efficiency"),
        "stock",
    ),
    (
        TABLE_BS,
        "các khoản phải thu ngắn hạn",
        ("agent_liquidity_solvency", "agent_efficiency"),
        "stock",
    ),
    (
        TABLE_BS,
        "phải trả người bán ngắn hạn",
        ("agent_efficiency",),
        "stock",
    ),
    (
        TABLE_CF,
        "lưu chuyển tiền thuần từ hoạt động đầu tư",
        ("agent_cashflow_analysis",),
        "flow",
    ),
    (
        TABLE_CF,
        "lưu chuyển tiền thuần từ hoạt động tài chính",
        ("agent_cashflow_analysis",),
        "flow",
    ),
    (
        TABLE_CF,
        "lưu chuyển tiền thuần trong kỳ",
        ("agent_cashflow_analysis",),
        "flow",
    ),
)
_BROAD_PROFITABILITY_CORE_ALIASES = {
    "lợi nhuận gộp về bán hàng và cung cấp dịch vụ": {"lợi nhuận gộp"},
    "tổng vốn chủ sở hữu": {"vốn chủ sở hữu"},
}


def _is_broad_profitability_assessment(
    planner_plan: dict,
    user_query: str,
    analysis_targets: list[dict],
) -> bool:
    """Recognize only the hard, overall profitability path.

    A standalone ROA/ROE/margin request must keep its narrow retrieval plan. The
    broad path is already normalized by Planner, so Router only needs to enforce
    the core operands that every comparative profitability assessment consumes.
    """

    difficulty = str(
        planner_plan.get("difficulty_level", "") or ""
    ).strip().lower()
    if difficulty != "hard":
        return False
    response_mode = str(planner_plan.get("response_mode", "") or "").strip().lower()
    if response_mode not in {"", "extractive"}:
        return False
    planned_agents = {
        str(target.get("agent", "") or "").strip()
        for target in (analysis_targets or [])
        if isinstance(target, dict)
    }
    comprehensive_agents = {
        "agent_profitability",
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    }
    return (
        "agent_profitability" in planned_agents
        and (
            comprehensive_agents.issubset(planned_agents)
            or contains_intent(
                user_query,
                _BROAD_PROFITABILITY_ASSESSMENT_PATTERNS,
            )
        )
    )


def _is_comprehensive_financial_assessment(
    planner_plan: dict,
    analysis_targets: list[dict],
) -> bool:
    if str(planner_plan.get("difficulty_level", "") or "").lower() != "hard":
        return False
    planned_agents = {
        str(target.get("agent", "") or "").strip()
        for target in (analysis_targets or [])
        if isinstance(target, dict)
    }
    return {
        "agent_profitability",
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    }.issubset(planned_agents)


def _profitability_comparison_suffix(user_query: str) -> str:
    """Use explicit report years when supplied, otherwise current/prior roles."""

    labels = [
        str(label).strip()
        for label in parse_query_slots(user_query).period_labels
        if re.fullmatch(r"(?:19|20)\d{2}", str(label).strip())
    ]
    if len(labels) >= 2:
        return f"năm {labels[0]} và năm {labels[1]}"
    if len(labels) == 1:
        current_year = int(labels[0])
        return f"năm {current_year} và năm {current_year - 1}"
    return "năm nay và năm trước"


def _unscoped_core_profitability_metric(
    item: dict,
    *,
    core_metrics: tuple = _BROAD_PROFITABILITY_CORE_METRICS,
) -> tuple[str, str] | None:
    """Return a core metric only for its consolidated main-statement route."""

    table = _normalize_evidence_table(item.get("table", ""))
    if table not in {TABLE_BS, TABLE_IS, TABLE_CF}:
        return None
    queries = _evidence_item_queries(item)
    if len(queries) != 1:
        return None
    slots = parse_query_slots(queries[0])
    if any(
        str(value or "").strip()
        for value in (
            slots.entity,
            slots.scope_label,
            slots.counterparty,
            slots.geography,
            slots.policy_topic,
        )
    ):
        return None
    metric = normalize_keyword_synonyms(slots.metric)
    for (
        core_table,
        core_metric,
        _audience,
        _period_kind,
    ) in core_metrics:
        aliases = _BROAD_PROFITABILITY_CORE_ALIASES.get(core_metric, set())
        if table == core_table and (metric == core_metric or metric in aliases):
            return (core_table, core_metric)
    return None


def _keep_broad_profitability_support_item(
    item: dict,
    *,
    user_query: str,
    planned_agents: list[str],
) -> bool:
    """Keep only support explicitly requested outside the canonical core.

    The broad profitability contract already supplies every operand used by its
    three default analysis axes. Router guesses such as current assets/current
    liabilities or a generic NOTE lookup add requirements the agents then try
    to close, despite the user never asking for them. Explicitly named metrics
    remain eligible, as do facts assigned to a planned sustainability/liquidity
    axis.
    """

    if not isinstance(item, dict):
        return False
    needby = _needby_values(item)
    if (
        "agent_liquidity_solvency" in planned_agents
        and "agent_liquidity_solvency" in needby
    ):
        return True

    table = _normalize_evidence_table(item.get("table", ""))
    if table in {TABLE_NOTE, TABLE_REPORT_SECTION} and contains_intent(
        user_query,
        (
            r"\bthuyet minh\b",
            r"\bchinh sach\b",
            r"\bchi tiet\b",
        ),
    ):
        return True

    user_text = normalize_keyword_synonyms(user_query).casefold()
    for query in _evidence_item_queries(item):
        if " ".join(query.casefold().split()) == " ".join(
            str(user_query or "").casefold().split()
        ):
            # `_entity_evidence_from_query` may fan the entire analytical
            # sentence back out as a retrieval query. Canonical core/support
            # items already carry its actual line-item metrics.
            continue
        slots = parse_query_slots(query)
        for value in (
            slots.metric,
            slots.entity,
            slots.scope_label,
            slots.counterparty,
            slots.geography,
        ):
            term = normalize_keyword_synonyms(value).casefold().strip()
            if term and term in user_text:
                return True
    return False


def _ensure_broad_profitability_core_evidence(
    evidence_plan: list[dict],
    *,
    planner_plan: dict,
    user_query: str,
    analysis_targets: list[dict],
) -> list[dict]:
    """Prepend complete comparative profitability inputs and audiences.

    Planner objectives are guidance for the Router model, not an evidence
    contract. Enforcing the metric pairs after model routing prevents any omitted
    operand from making margins/returns/cash-conversion impossible. Core pairs
    are placed first so per-statement prompt caps cannot discard them behind
    optional facts.
    """

    if not _is_broad_profitability_assessment(
        planner_plan,
        user_query,
        analysis_targets,
    ):
        return list(evidence_plan or [])

    planned_agents = _dedupe_keep_order(
        str(target.get("agent", "") or "").strip()
        for target in (analysis_targets or [])
        if isinstance(target, dict)
        and is_analysis_agent(str(target.get("agent", "") or "").strip())
    )
    core_metrics = (
        _COMPREHENSIVE_FINANCIAL_CORE_METRICS
        if _is_comprehensive_financial_assessment(
            planner_plan,
            analysis_targets,
        )
        else _BROAD_PROFITABILITY_CORE_METRICS
    )
    inherited_audiences: dict[tuple[str, str], list[str]] = {}
    remaining_items = []
    for item in evidence_plan or []:
        if not isinstance(item, dict):
            continue
        metric_key = _unscoped_core_profitability_metric(
            item,
            core_metrics=core_metrics,
        )
        if metric_key is None:
            if _keep_broad_profitability_support_item(
                item,
                user_query=user_query,
                planned_agents=planned_agents,
            ):
                remaining_items.append(item)
            continue
        inherited_audiences[metric_key] = _dedupe_keep_order(
            list(inherited_audiences.get(metric_key, []) or [])
            + _needby_values(item)
        )

    flow_suffix = _profitability_comparison_suffix(user_query)
    core_items = []
    for table, metric, default_audience, period_kind in core_metrics:
        needby = _dedupe_keep_order(
            [
                agent
                for agent in (
                    *default_audience,
                    *inherited_audiences.get((table, metric), []),
                )
                if agent in planned_agents
            ]
        )
        suffix = flow_suffix if period_kind == "flow" else "cuối kỳ và đầu kỳ"
        core_items.append(
            {
                "table": table,
                "query": f"{metric} {suffix}",
                "canonical_query": metric,
                "needby": needby or ["agent_profitability"],
                **(
                    {"period_role": "both"}
                    if period_kind == "flow"
                    else {"period": "both"}
                ),
            }
        )

    return [*core_items, *remaining_items]


def _focused_needed_by(
    table: str,
    query: str,
    analysis_targets: list[dict],
) -> list[str]:
    """Infer the smallest useful analysis audience for an unscoped fact."""

    table_name = _normalize_evidence_table(table)
    query_text = " ".join(str(query or "").strip().casefold().split())
    planned_agents = _dedupe_keep_order(
        str(target.get("agent", "") or "").strip()
        for target in (analysis_targets or [])
        if isinstance(target, dict)
        and is_analysis_agent(str(target.get("agent", "") or "").strip())
    )
    compatible_agents = [
        agent
        for agent in planned_agents
        if table_name in ANALYSIS_TABLE_ALLOWLIST.get(agent, set())
    ]
    if not compatible_agents:
        return []

    focused = [
        agent
        for agent in compatible_agents
        if any(
            marker in query_text
            for marker in _ANALYSIS_QUERY_FOCUS_MARKERS.get(agent, ())
        )
    ]
    # Every cash-flow statement line belongs to the cash-flow axis even if the
    # caption is an unusual synonym not covered by the marker list.
    if (
        table_name == TABLE_CF
        and "agent_cashflow_analysis" in compatible_agents
        and "agent_cashflow_analysis" not in focused
    ):
        focused.append("agent_cashflow_analysis")
    if focused:
        return _dedupe_keep_order(focused)

    primary = _PRIMARY_ANALYSIS_AGENT_BY_TABLE.get(table_name, "")
    if primary in compatible_agents:
        return [primary]

    # A note or an uncommon line item with no recognizable focus still goes to
    # one planned axis instead of being broadcast to every analysis worker.
    return compatible_agents[:1]


def _evidence_plan_from_table_targets(
    table_targets: list[dict],
    analysis_targets: list[dict],
) -> list[dict]:
    evidence_plan = []
    seen = set()

    for target in table_targets or []:
        if not isinstance(target, dict):
            continue
        table = _table_from_route_payload(target)
        if not table:
            continue
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])

        for requirement in requirements:
            query = _normalize_followup_requirement_for_target(requirement, table)
            if not query:
                continue
            key = (table, query)
            if key in seen:
                continue
            seen.add(key)
            evidence_plan.append(
                {
                    "table": table,
                    "query": query,
                    "needby": _focused_needed_by(
                        table,
                        query,
                        analysis_targets,
                    ),
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
            table = str(item.get("table", "") or "").strip()
            for query in _evidence_item_queries(item):
                evidence_queries.append({
                    "table": table,
                    "query": query,
                    **_query_metadata_for_item(item, query),
                })

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
    output = []

    for item in evidence_plan or []:
        if not isinstance(item, dict):
            continue

        table = _table_from_route_payload(item)
        if not table:
            for query in _evidence_item_queries(item):
                table = _table_for_allowed_query(query)
                if table:
                    break
        if not table:
            table = ""

        explicit_needby = _needby_values(item)
        inferred_needed_by = _focused_needed_by(
            table,
            " | ".join(_evidence_item_queries(item)),
            analysis_targets,
        )
        needby = explicit_needby or inferred_needed_by
        for query in _evidence_item_queries(item):
            output.append(
                {
                    "table": table,
                    "query": query,
                    "needby": _dedupe_keep_order(needby),
                    **_query_metadata_for_item(item, query),
                }
            )

    return output


_EVIDENCE_METADATA_FIELDS = (
    "web_intent",
    "time_hint",
    "period",
    "period_role",
    "period_label",
    "unit",
    "value_type",
    "evidence_query",
    "source",
    "note_ref",
    "source_table",
    "source_item",
    "operation",
    "operand_role",
    "operands",
    "scope_label",
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
    "section_key",
    "coverage_template",
    "required_legs",
)


def _query_metadata_for_item(item: dict, query: str) -> dict:
    metadata = {}
    per_query = item.get("query_metadata", {}) if isinstance(item, dict) else {}
    if isinstance(per_query, dict) and isinstance(per_query.get(query), dict):
        metadata.update(per_query[query])
    for field in _EVIDENCE_METADATA_FIELDS:
        value = item.get(field) if isinstance(item, dict) else None
        if value not in ("", None, [], {}) and field not in metadata:
            metadata[field] = value
    table = _table_from_route_payload(item) if isinstance(item, dict) else ""
    for field, value in _typed_calculation_metadata(query, table).items():
        if value not in ("", None, [], {}) and field not in metadata:
            metadata[field] = value
    return metadata


def _merge_evidence_plans(*plans: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []

    for plan in plans:
        for item in plan or []:
            if not isinstance(item, dict):
                continue

            table = _table_from_route_payload(item)
            if not table:
                for candidate_query in _evidence_item_queries(item):
                    table = _table_for_allowed_query(candidate_query)
                    if table:
                        break
            for raw_query in _evidence_item_queries(item):
                item_metadata = _query_metadata_for_item(item, raw_query)
                raw_canonical_query = (
                    _first_query_map_value(
                        item,
                        "canonical_queries",
                        "canonical_query",
                        raw_query,
                    )
                    or raw_query
                )
                canonical_query = (
                    raw_canonical_query
                    if str(
                        item_metadata.get("operand_role", "")
                        or item.get("operand_role", "")
                    ).strip()
                    else _normalize_followup_requirement_for_target(
                        raw_canonical_query,
                        table,
                    )
                )
                query = raw_query or canonical_query
                if not query:
                    continue

                key = (table, query)
                if key not in merged:
                    merged[key] = {
                        "table": table,
                        "query": query,
                        "needby": [],
                    }
                    if canonical_query and canonical_query != query:
                        merged[key]["canonical_query"] = canonical_query
                    order.append(key)

                for field, value in item_metadata.items():
                    if value not in ("", None, [], {}) and merged[key].get(field) in ("", None, [], {}):
                        merged[key][field] = value

                search_query = (
                    _first_query_map_value(item, "search_queries", "search_query", raw_query)
                    or str(item.get("original_query") or item.get("raw_query") or "").strip()
                )
                if search_query and search_query != query:
                    merged[key]["search_query"] = search_query
                if canonical_query and canonical_query != query:
                    merged[key]["canonical_query"] = canonical_query
                merged[key]["needby"] = _dedupe_keep_order(
                    list(merged[key].get("needby", []) or []) + _needby_values(item)
                )

    return [merged[key] for key in order]


def _compact_evidence_plan_by_table_needby(evidence_plan: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, tuple[str, ...]], dict] = {}
    order: list[tuple[str, tuple[str, ...]]] = []

    for item in evidence_plan or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table", "") or "").strip()
        needby = tuple(_needby_values(item))
        key = (table, needby)
        if key not in grouped:
            grouped[key] = {
                "table": table,
                "needby": list(needby),
                "_queries": [],
                "_canonical_queries": {},
                "_search_queries": {},
                "_query_metadata": {},
            }
            order.append(key)

        group = grouped[key]
        for query in _evidence_item_queries(item):
            if query not in group["_queries"]:
                group["_queries"].append(query)

            canonical_query = _first_query_map_value(
                item,
                "canonical_queries",
                "canonical_query",
                query,
            )
            if canonical_query and canonical_query != query:
                group["_canonical_queries"][query] = canonical_query

            search_query = (
                _first_query_map_value(item, "search_queries", "search_query", query)
                or str(item.get("original_query") or item.get("raw_query") or "").strip()
            )
            if search_query and search_query != query:
                group["_search_queries"][query] = search_query

            metadata = _query_metadata_for_item(item, query)
            if metadata:
                current = dict(group["_query_metadata"].get(query, {}) or {})
                group["_query_metadata"][query] = {**current, **metadata}

    output = []
    for key in order:
        group = grouped[key]
        queries = list(group.pop("_queries", []) or [])
        canonical_queries = dict(group.pop("_canonical_queries", {}) or {})
        search_queries = dict(group.pop("_search_queries", {}) or {})
        query_metadata = dict(group.pop("_query_metadata", {}) or {})
        if not queries:
            continue

        payload = {
            "table": group.get("table", ""),
            "needby": group.get("needby", []),
        }
        if len(queries) == 1:
            query = queries[0]
            payload["query"] = query
            if canonical_queries.get(query):
                payload["canonical_query"] = canonical_queries[query]
            if search_queries.get(query):
                payload["search_query"] = search_queries[query]
            payload.update(query_metadata.get(query, {}) or {})
        else:
            payload["queries"] = queries
            if canonical_queries:
                payload["canonical_queries"] = canonical_queries
            if search_queries:
                payload["search_queries"] = search_queries
            if query_metadata:
                payload["query_metadata"] = query_metadata
                # Operation and the complete ordered operand contract are
                # global to the calculation, even though operand_role/period
                # remain query-specific.  Promote the shared fields so plan
                # consumers that do not inspect query_metadata still retain
                # the deterministic arithmetic contract.
                metadata_values = [
                    query_metadata.get(query, {})
                    for query in queries
                    if isinstance(query_metadata.get(query, {}), dict)
                ]
                for field in ("operation", "operands"):
                    field_values = [
                        metadata.get(field)
                        for metadata in metadata_values
                        if metadata.get(field) not in ("", None, [], {})
                    ]
                    common_value = field_values[0] if field_values else None
                    if (
                        common_value not in ("", None, [], {})
                        and all(value == common_value for value in field_values)
                    ):
                        payload[field] = common_value

        output.append(
            {
                field: value
                for field, value in payload.items()
                if field == "needby" or value not in ("", None, [], {})
            }
        )

    return output


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
    calculation_items = _calculation_operand_evidence_items(
        text_value,
        needby=needed_by,
    )
    if calculation_items:
        return _merge_evidence_plans(calculation_items)

    if _requires_report_section_followup(text_value):
        report_query = _compact_note_followup_requirement(text_value)
        if report_query:
            return _merge_evidence_plans(
                [
                    {
                        "table": TABLE_REPORT_SECTION,
                        "query": report_query,
                        "needby": needed_by,
                    }
                ]
            )
        return []

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

    compact_query = _compact_note_followup_requirement(text_value)
    if compact_query:
        for candidate in route_candidates(text_value):
            if (
                candidate.table in {TABLE_NOTE, TABLE_REPORT_SECTION}
                and candidate.confidence >= 0.60
            ):
                items.append(
                    {
                        "table": candidate.table,
                        "query": compact_query,
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


def _planner_premise_evidence_items(planner_plan: dict) -> list[dict]:
    if str(planner_plan.get("response_mode", "") or "").strip() != "grounded_interpretation":
        return []
    items = []
    for premise in _dedupe_keep_order(
        planner_plan.get("premise_requirements", []) or []
    ):
        premise_text = str(premise)
        premise_items = _fallback_evidence_items_from_text(
            premise_text,
            needby=[],
        )
        narrative_routes = [
            candidate.table
            for candidate in route_candidates(premise_text)
            if (
                candidate.table in {TABLE_NOTE, TABLE_REPORT_SECTION}
                and candidate.confidence >= 0.60
            )
        ]
        present_tables = {
            str(item.get("table", "") or "").strip()
            for item in premise_items
            if isinstance(item, dict)
        }
        for table in narrative_routes:
            if table not in present_tables:
                premise_items.append(
                    {"table": table, "query": premise_text, "needby": []}
                )
                present_tables.add(table)
        if not premise_items:
            # A premise is a report fact, not an external conclusion. When its
            # vocabulary is not exclusive enough for the deterministic router,
            # keep both narrative sources instead of silently dropping it.
            premise_items = [
                {"table": TABLE_NOTE, "query": premise_text, "needby": []},
                {
                    "table": TABLE_REPORT_SECTION,
                    "query": premise_text,
                    "needby": [],
                },
            ]
        items.extend(premise_items)
    return _merge_evidence_plans(items)


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
    evidence_items.extend(_planner_premise_evidence_items(planner_plan))

    if bool(planner_plan.get("need_web", False)) and user_query:
        evidence_items.append(
            {
                "table": "",
                "query": str(user_query or "").strip(),
                "needby": axis_agents,
                "web_intent": str(planner_plan.get("web_intent", "") or "").strip(),
            }
        )

    return {
        "evidence_plan": _merge_evidence_plans(evidence_items),
        "analysis_plan": [],
        "targets": [],
    }


def _analysis_coverage_log(
    state: dict,
    planner_plan: dict,
    worker_plan: dict,
    coverage_notes: list[str],
):
    """Trace planner axes vs the agents the router will actually dispatch."""

    planned = _planner_axis_agents(planner_plan)
    dispatched = [
        str(item.get("agent", "") or "").strip()
        for item in (worker_plan.get("analysis_plan", []) or [])
        if isinstance(item, dict) and str(item.get("agent", "") or "").strip()
    ]
    if not planned and not dispatched:
        return None
    return make_log(
        state,
        "router:analysis_coverage",
        planned_agents=planned,
        dispatched_agents=dispatched,
        missing_agents=sorted(set(planned) - set(dispatched)),
        unplanned_agents=sorted(set(dispatched) - set(planned)) if planned else [],
        reconciliation=coverage_notes,
    )


def _finalize_router_targets(
    worker_plan: dict,
    planner_plan: dict,
    user_query: str = "",
    coverage_notes: Optional[list[str]] = None,
    model_authored: bool = False,
) -> dict:
    normalized_targets = list((worker_plan or {}).get("targets", []) or [])
    direct_evidence_plan = _restore_user_keywords_in_evidence_plan(
        list((worker_plan or {}).get("evidence_plan", []) or []),
        user_query,
    )
    direct_analysis_plan = list((worker_plan or {}).get("analysis_plan", []) or [])
    table_targets = [
        target
        for target in normalized_targets
        if _table_from_route_payload(target)
    ]
    table_targets = _filter_optional_table_requirements(table_targets, user_query)

    table_targets = _merge_table_targets(table_targets)
    calculation_evidence = _calculation_operand_evidence_items(user_query)
    premise_evidence = _planner_premise_evidence_items(planner_plan)

    difficulty_level = str(planner_plan.get("difficulty_level", "") or "").strip().lower()
    if difficulty_level != "hard":
        evidence_plan = _merge_evidence_plans(
            calculation_evidence,
            premise_evidence,
            direct_evidence_plan,
            _evidence_plan_from_table_targets(
                table_targets,
                [],
            ),
        )
        return {
            "evidence_plan": _compact_evidence_plan_by_table_needby(evidence_plan),
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

    evidence_plan_expanded = _merge_evidence_plans(
        _with_inferred_needed_by(calculation_evidence, analysis_targets),
        _with_inferred_needed_by(direct_evidence_plan, analysis_targets),
        _evidence_plan_from_table_targets(
            table_targets,
            analysis_targets,
        ),
        # G1: also fetch the specific line item the question names, so narrow
        # analytical questions retrieve their own entity (recall-first), not just
        # the generic axis figures the analysis agents gather.
        _with_inferred_needed_by(_entity_evidence_from_query(user_query), analysis_targets),
    )
    # Heuristic top-up of the router's evidence, on the same recall-first
    # switch as the table augmentation above.
    if evidence_augmentation_enabled() or not model_authored:
        evidence_plan_expanded = _ensure_broad_profitability_core_evidence(
            evidence_plan_expanded,
            planner_plan=planner_plan,
            user_query=user_query,
            analysis_targets=analysis_targets,
        )
    analysis_plan = _analysis_plan_from_targets(
        analysis_targets,
        evidence_plan_expanded,
    )
    analysis_plan, reconciliation_notes = reconcile_analysis_plan_coverage(
        analysis_plan,
        planner_plan,
    )
    if coverage_notes is not None:
        coverage_notes.extend(reconciliation_notes)
    return {
        "evidence_plan": _compact_evidence_plan_by_table_needby(evidence_plan_expanded),
        "analysis_plan": analysis_plan,
        "targets": _analysis_targets_from_plan(analysis_plan),
    }


def _direct_router_plan_from_query(planner_plan: dict, user_query: str) -> Optional[dict]:
    difficulty_level = str(planner_plan.get("difficulty_level", "") or "").strip().lower()
    if difficulty_level != "easy":
        return None
    if _is_followup_mode(planner_plan) or bool(planner_plan.get("need_web", False)):
        return None
    if planner_plan.get("analysis_axes"):
        return None

    evidence_plan = _direct_line_item_evidence_from_query(user_query)
    if not evidence_plan:
        return None

    return _finalize_router_targets(
        {"evidence_plan": evidence_plan},
        planner_plan,
        user_query=user_query,
    )


def _router_tables_from_analysis_axes(planner_plan: dict) -> list[str]:
    tables = []
    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        agent = str(axis.get("axis", "") or "").strip()
        for table in sorted(ANALYSIS_TABLE_ALLOWLIST.get(agent, set())):
            if table:
                tables.append(table)
    return _dedupe_keep_order(tables)


def _router_allowed_keyword_tables(planner_plan: dict, user_query: str) -> list[str] | None:
    tables = []

    direct_evidence = _direct_line_item_evidence_from_query(user_query)
    tables.extend(
        str(item.get("table", "") or "").strip()
        for item in direct_evidence
        if str(item.get("table", "") or "").strip()
    )

    for requirement in _followup_requirements_from_plan(planner_plan):
        table = _route_followup_requirement(requirement)
        if table:
            tables.append(table)

    tables.extend(_router_tables_from_analysis_axes(planner_plan))

    tables.extend(
        candidate.table
        for candidate in route_candidates(user_query)
        if candidate.confidence >= 0.60
    )

    tables = _dedupe_keep_order(tables)
    return tables or None


def _router_system_instruction(planner_plan: dict, default_instruction: str) -> str:
    difficulty_level = str(planner_plan.get("difficulty_level", "") or "").strip().lower()
    if (
        difficulty_level in {"easy", "medium"}
        and not _is_followup_mode(planner_plan)
        and not bool(planner_plan.get("need_web", False))
    ):
        return COMPACT_ROUTER_SYSTEM_INSTRUCTION
    return default_instruction


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

    updates = {
        "last_agent": "agent_router",
        "trace": trace,
    }
    user_query = state.get("user_query", "")
    # Model-first: no deterministic plan may stand in for the router. The bypass
    # remains for `legacy`, and `shadow` reports every query it would have taken.
    direct_worker_plan = _direct_router_plan_from_query(planner_plan, user_query)
    if direct_worker_plan is not None and not router_direct_bypass_enabled():
        direct_worker_plan = None
    elif direct_worker_plan is not None and shadow_routing():
        trace.append(
            make_log(
                state,
                "routing:shadow_diff",
                stage="router_direct_bypass",
                bypassed=True,
                evidence_items_n=len(direct_worker_plan.get("evidence_plan", []) or []),
            )
        )
    if direct_worker_plan is not None:
        updates["worker_plan"] = direct_worker_plan
        updates["expected_workers"] = []
        updates["dispatch_phase"] = "evidence"
        updates["pending_analysis_targets"] = []
        debug_log = make_debug_log(
            state,
            "router:heuristic_direct_line_item",
            evidence_plan=_router_trace_evidence_plan(direct_worker_plan),
        )
        if debug_log:
            updates["trace"].append(debug_log)
        updates["trace"].append(
            make_log(
                state,
                "router:done",
                mode="heuristic_direct_line_item",
                targets_n=0,
                targets=[],
                evidence_items_n=len(direct_worker_plan.get("evidence_plan", []) or []),
                evidence_queries_n=_router_evidence_query_count(direct_worker_plan),
                evidence_plan=_router_trace_evidence_plan(direct_worker_plan),
                analysis_plan_n=0,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        return updates

    allowed_keyword_tables = _router_allowed_keyword_tables(planner_plan, user_query)
    payload = {
        "role": profile["role"],
        "system_instruction": _router_system_instruction(
            planner_plan,
            profile["system_instruction"],
        ),
        "user_query": user_query,
        "worker_query": "",
        "plan_json": json.dumps(planner_plan, ensure_ascii=False),
        "worker_results_json": "{}",
        "allowed_keywords_json": build_allowed_keywords_payload(
            selected_tables=allowed_keyword_tables
        ),
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list("agent_router"),
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
        coverage_notes: list[str] = []
        worker_plan = _finalize_router_targets(
            _sanitize_router_plan_payload(plan_obj.model_dump()),
            planner_plan,
            user_query=state.get("user_query", ""),
            coverage_notes=coverage_notes,
            model_authored=True,
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
        coverage_log = _analysis_coverage_log(
            state,
            planner_plan,
            worker_plan,
            coverage_notes,
        )
        if coverage_log:
            updates["trace"].append(coverage_log)

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
                evidence_queries_n=_router_evidence_query_count(updates.get("worker_plan", {}) or {}),
                evidence_plan=_router_trace_evidence_plan(updates.get("worker_plan", {}) or {}),
                analysis_plan_n=len((updates.get("worker_plan", {}) or {}).get("analysis_plan", []) or []),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )
        return updates

    except Exception as e:
        coverage_notes = []
        worker_plan = _finalize_router_targets(
            _fallback_router_payload_from_planner(
                planner_plan,
                user_query=state.get("user_query", ""),
            ),
            planner_plan,
            user_query=state.get("user_query", ""),
            coverage_notes=coverage_notes,
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
        coverage_log = _analysis_coverage_log(
            state,
            planner_plan,
            worker_plan,
            coverage_notes,
        )
        if coverage_log:
            updates["trace"].append(coverage_log)
        updates["trace"].append(
            make_log(
                state,
                "router:heuristic_fallback",
                error_type=type(e).__name__,
                error=str(e)[:250],
                evidence_items_n=len((worker_plan.get("evidence_plan", []) or [])),
                evidence_queries_n=_router_evidence_query_count(worker_plan),
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
                evidence_queries_n=_router_evidence_query_count(updates.get("worker_plan", {}) or {}),
                evidence_plan=_router_trace_evidence_plan(updates.get("worker_plan", {}) or {}),
                analysis_plan_n=len((updates.get("worker_plan", {}) or {}).get("analysis_plan", []) or []),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        return updates


def run_keyworder(state: dict) -> dict:
    return run_router(state)
