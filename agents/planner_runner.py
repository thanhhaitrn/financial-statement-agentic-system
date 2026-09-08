"""Build the high-level plan that decides whether a query needs analysis or retrieval."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from tools.langchain_tools import get_tools_list
from agents.line_item_matcher import (
    DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS,
    contains_intent,
    direct_line_item_match,
)
from agents.profiles import AGENT_PROFILES
from config.runtime_policy import planner_axis_expansion_enabled, shadow_routing
from dataset_catalog.registry import get_dataset
from schemas.agent_outputs import PlannerEvidencePlan
from llm.invoke import extract_usage_metadata, invoke_prompt
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log
from common import dedupe_keep_order as _dedupe_keep_order

DEFAULT_PLANNER_PLAN = {
    "difficulty_level": "easy",
    "response_mode": "extractive",
    "premise_requirements": [],
    "analysis_axes": [],
    "company": "",
    "time_hint": "",
    "need_web": False,
    "web_intent": "",
}
EVALUATIVE_INTENT_PATTERNS = DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema PlannerEvidencePlan.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
    )
def _plain_planner_payload(payload: dict) -> dict:
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
        return value

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


def _extract_company_from_query(user_query: str) -> str:
    text = " ".join(str(user_query or "").strip().split())
    if not text:
        return ""

    patterns = [
        r"\bcủa\s+(.+?)(?=\s+(?:tại|ngày|năm|quý|q[1-4]|là|bao nhiêu|bao nhiêu\?|$))",
        r"\b(?:công ty|doanh nghiệp|tập đoàn)\s+(.+?)(?=\s+(?:tại|ngày|năm|quý|q[1-4]|là|bao nhiêu|bao nhiêu\?|$))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        company = match.group(1).strip(" ,.?")
        if company:
            return company

    return ""


def _extract_time_hint_from_query(user_query: str) -> str:
    """Recover an explicit period when the planner omits or fails to parse it."""
    text = " ".join(str(user_query or "").strip().split())
    if not text:
        return ""

    patterns = (
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",
        r"\b(?:quý|quy)\s*[1-4](?:\s*[/-]\s*\d{4}|\s+năm\s+\d{4})?\b",
        r"\bq[1-4](?:\s*[/-]?\s*\d{4})?\b",
        r"\b(?:năm|nam)\s+\d{4}\b",
        r"\b(?:cuối kỳ|cuoi ky|đầu kỳ|dau ky|cuối năm|cuoi nam|"
        r"đầu năm|dau nam|trong kỳ|trong ky|lũy kế|luy ke)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


def _coerce_planner_plan(result: Any) -> tuple[PlannerEvidencePlan, Optional[str], Optional[str]]:
    if isinstance(result, PlannerEvidencePlan):
        return result, None, None

    parsing_error = None

    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, PlannerEvidencePlan):
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
            return PlannerEvidencePlan.model_validate(payload), parsing_error, source
        except ValidationError:
            continue

    if parsing_error:
        raise ValueError(parsing_error)

    raise ValueError("Planner did not return a valid PlannerEvidencePlan payload.")


def _normalize_company(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())

def _planner_trace_summary(planner_plan: dict) -> dict:
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    analysis_axes_trace = []
    for axis in analysis_axes:
        if not isinstance(axis, dict):
            continue
        analysis_axes_trace.append(dict(axis))

    return {
        "difficulty_level": str(planner_plan.get("difficulty_level", "") or "").strip(),
        "response_mode": str(planner_plan.get("response_mode", "") or "").strip(),
        "premise_requirements_n": len(
            planner_plan.get("premise_requirements", []) or []
        ),
        "analysis_axes_n": len(analysis_axes),
        "analysis_axes": analysis_axes_trace,
        "company": str(planner_plan.get("company", "") or "").strip(),
        "time_hint": str(planner_plan.get("time_hint", "") or "").strip(),
        "need_web": bool(planner_plan.get("need_web", False)),
        "web_intent": str(planner_plan.get("web_intent", "") or "").strip(),
    }


def _enrich_plan_fields(state: dict, planner_plan: dict) -> dict:
    enriched = dict(planner_plan or {})
    dataset = None
    user_query = str((state or {}).get("user_query", "") or "")
    dataset_id = str((state or {}).get("dataset_id", "") or "").strip()
    if dataset_id:
        dataset = get_dataset(dataset_id)

    query_company = _extract_company_from_query(user_query)
    if not str(enriched.get("company", "") or "").strip():
        if query_company:
            enriched["company"] = query_company
        elif dataset is not None:
            enriched["company"] = dataset.company

    planner_time_hint = str(enriched.get("time_hint", "") or "").strip()
    enriched["time_hint"] = planner_time_hint or _extract_time_hint_from_query(user_query)

    return enriched


def _normalize_intent_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _contains_evaluative_intent(text: str) -> bool:
    return contains_intent(text, EVALUATIVE_INTENT_PATTERNS)


def _is_qualitative_concept_query(user_query: str) -> bool:
    """True when the question is about a front-matter / qualitative concept
    (going concern, internal control, governance, audit opinion, accounting
    policy) rather than a financial figure. Such questions must not be routed to
    the financial analysis agents."""
    from agents.keyworder_runner import _requires_report_section_followup

    if _requires_report_section_followup(user_query):
        return True
    text = str(user_query or "").lower()
    return "chính sách kế toán" in text or "chinh sach ke toan" in text


GROUNDED_INTERPRETATION_PATTERNS = (
    r"\by nghia\b",
    r"\bham y\b",
    r"\btac dong\b",
    r"\banh huong\b",
    r"\bvai tro\b",
    r"\bhe qua\b",
    r"\btam quan trong\b",
    r"\bphan anh\b",
    r"\bcho thay dieu gi\b",
    r"\bgiai thich\b",
    r"\bdanh gia\b",
    r"\bphan tich\b",
)
GROUNDED_NARRATIVE_SUBJECT_PATTERNS = (
    r"\bchuyen doi\b",
    r"\bco phan hoa\b",
    r"\bniem yet\b",
    r"\bhinh thanh va phat trien\b",
    r"\blich su phat trien\b",
    r"\bhinh thuc so huu\b",
    r"\bquan tri doanh nghiep\b",
    r"\bco cau to chuc\b",
    r"\bkiem soat noi bo\b",
    r"\bchinh sach ke toan\b",
    r"\bchinh sach\b",
    r"\bkhong khau hao\b",
    r"\bco so hoat dong lien tuc\b",
    r"\bgoing concern\b",
)
BROAD_PROFITABILITY_ASSESSMENT_PATTERNS = (
    r"\bkha nang sinh loi\b",
    r"\bhieu qua sinh loi\b",
    r"\bprofitability\b",
)
BROAD_FINANCIAL_ASSESSMENT_PATTERNS = (
    r"\btinh hinh tai chinh\b",
    r"\bsuc khoe tai chinh\b",
    r"\btai chinh tong the\b",
    r"\boverall financial (?:position|health|performance)\b",
)
PROFITABILITY_SUSTAINABILITY_PATTERNS = (
    r"\bben vung\b",
    r"\bduy tri\b",
    r"\bdai han\b",
    r"\ban toan tai chinh\b",
    r"\brui ro tai chinh\b",
    r"\brui ro thanh khoan\b",
)
PROFITABILITY_AXIS_OBJECTIVES = {
    "agent_profitability": (
        "Đánh giá mức sinh lời qua biên lợi nhuận gộp, biên lợi nhuận hoạt "
        "động, biên lợi nhuận ròng, ROA và ROE. Bắt buộc lấy doanh thu thuần, "
        "lợi nhuận gộp, lợi nhuận thuần từ hoạt động kinh doanh và lợi nhuận "
        "sau thuế của kỳ hiện tại lẫn kỳ so sánh; lấy tổng tài sản và tổng vốn "
        "chủ sở hữu cuối kỳ lẫn đầu kỳ để đánh giá biến động trên cơ sở bình quân, "
        "không chỉ nêu tỷ số của một kỳ."
    ),
    "agent_cashflow_analysis": (
        "Đánh giá chất lượng lợi nhuận và biến động tiền qua lưu chuyển tiền "
        "thuần từ hoạt động kinh doanh (CFO), lưu chuyển tiền thuần từ hoạt "
        "động đầu tư (CFI), lưu chuyển tiền thuần từ hoạt động tài chính "
        "(CFF), lưu chuyển tiền thuần trong kỳ, số dư tiền và lợi nhuận sau "
        "thuế của kỳ hiện tại lẫn kỳ so sánh."
    ),
    "agent_efficiency": (
        "Đánh giá hiệu quả sử dụng tài sản hỗ trợ khả năng sinh lời qua doanh "
        "thu thuần, giá vốn và tổng tài sản của kỳ hiện tại lẫn kỳ so sánh, "
        "cùng hàng tồn kho, phải thu và phải trả người bán."
    ),
    "agent_liquidity_solvency": (
        "Đánh giá đòn bẩy và khả năng thanh toán qua tổng tài sản ngắn hạn, "
        "tổng nợ ngắn hạn, tiền, hàng tồn kho, tổng nợ phải trả và vốn chủ sở "
        "hữu của kỳ hiện tại lẫn kỳ so sánh."
    ),
}
PROFITABILITY_AXIS_REQUIRED_MARKERS = {
    "agent_profitability": (
        "biên lợi nhuận ròng",
        "roa",
        "roe",
        "doanh thu thuần",
        "lợi nhuận gộp",
        "lợi nhuận thuần từ hoạt động kinh doanh",
        "lợi nhuận sau thuế",
        "tổng tài sản",
        "tổng vốn chủ sở hữu",
        "kỳ so sánh",
    ),
    "agent_cashflow_analysis": (
        "lưu chuyển tiền thuần từ hoạt động kinh doanh",
        "lưu chuyển tiền thuần từ hoạt động đầu tư",
        "lưu chuyển tiền thuần từ hoạt động tài chính",
        "lưu chuyển tiền thuần trong kỳ",
        "lợi nhuận sau thuế",
        "kỳ so sánh",
    ),
    "agent_efficiency": (
        "doanh thu thuần",
        "giá vốn",
        "tổng tài sản",
        "hàng tồn kho",
        "phải thu",
        "phải trả người bán",
        "kỳ so sánh",
    ),
    "agent_liquidity_solvency": (
        "tổng tài sản ngắn hạn",
        "tổng nợ ngắn hạn",
        "tiền",
        "hàng tồn kho",
        "tổng nợ phải trả",
        "vốn chủ sở hữu",
        "kỳ so sánh",
    ),
}


def _is_grounded_interpretation_query(user_query: str) -> bool:
    """Identify qualitative questions that need inference from reported premises.

    These questions are different from both literal front-matter lookups and the
    four-axis financial-analysis path: the report can establish the premises
    even when it does not contain the requested conclusion verbatim.
    """

    from tools.query_routing import parse_query_slots, route_candidates

    slots = parse_query_slots(user_query)
    # A numeric delta/ratio/comparison keeps the deterministic financial path
    # even when the subject happens to be governance/front-matter (for example
    # a year-over-year change in Board remuneration).
    if slots.operation != "lookup":
        return False

    candidates = route_candidates(user_query)
    top_route_is_front_matter = bool(
        candidates
        and candidates[0].confidence >= 0.60
        and candidates[0].table == "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH"
    )
    return (
        _is_qualitative_concept_query(user_query)
        or contains_intent(user_query, GROUNDED_NARRATIVE_SUBJECT_PATTERNS)
        or top_route_is_front_matter
    ) and contains_intent(
        user_query, GROUNDED_INTERPRETATION_PATTERNS
    )


def _grounded_premise_requirements(user_query: str) -> list[str]:
    """Compile report premises without searching for the requested conclusion."""

    requirements = []
    if contains_intent(user_query, (r"\bdoanh nghiep nha nuoc\b",)):
        requirements.append("trạng thái doanh nghiệp nhà nước trước chuyển đổi")
    if contains_intent(
        user_query,
        (r"\bco phan hoa\b", r"\bcong ty co phan\b", r"\bchuyen doi\b"),
    ):
        requirements.append("sự kiện cổ phần hóa và đăng ký công ty cổ phần")
    if contains_intent(user_query, (r"\bniem yet\b",)):
        requirements.append("sự kiện cấp phép và niêm yết cổ phiếu")
    if contains_intent(
        user_query,
        (r"\bchinh sach\b", r"\bkhong khau hao\b", r"\bgoing concern\b"),
    ):
        requirements.append("nội dung chính sách và điều kiện áp dụng được báo cáo")
    if contains_intent(user_query, (r"\bkiem soat noi bo\b",)):
        requirements.append("mô tả hệ thống kiểm soát nội bộ trong báo cáo")
    return _dedupe_keep_order(requirements)


def _collect_planner_objectives(planner_plan: dict) -> list[str]:
    objectives = []
    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        objective = str(axis.get("objective", "") or "").strip()
        if objective:
            objectives.append(objective)
    return _dedupe_keep_order(objectives)


def _apply_planner_difficulty_heuristics(state: dict, planner_plan: dict) -> tuple[dict, Optional[dict]]:
    enriched = dict(planner_plan or {})
    current = _normalize_intent_text(enriched.get("difficulty_level", ""))
    user_query = str((state or {}).get("user_query", "") or "").strip()
    from tools.query_routing import parse_query_slots

    calculation_slots = parse_query_slots(user_query)
    complete_calculation_contract = bool(
        (
            calculation_slots.operation in {"ratio", "share"}
            and len(calculation_slots.operands) == 2
        )
        or (
            calculation_slots.operation
            in {"delta", "percent_change", "multiple"}
            and (
                calculation_slots.period == "both"
                or calculation_slots.period_role == "both"
                or len(calculation_slots.period_labels) >= 2
            )
        )
    )
    if complete_calculation_contract:
        enriched["difficulty_level"] = "medium"
        enriched["response_mode"] = "extractive"
        enriched["premise_requirements"] = []
        enriched["analysis_axes"] = []
        enriched["need_web"] = False
        enriched["web_intent"] = ""
        return enriched, (
            make_debug_log(
                state,
                "planner:difficulty_upgraded_for_typed_calculation",
                previous_difficulty=current or "",
                upgraded_difficulty="medium",
                operation=calculation_slots.operation,
            )
            if current != "medium"
            else None
        )

    direct_line_item = direct_line_item_match(user_query)
    if direct_line_item is not None:
        previous_axes = list(enriched.get("analysis_axes", []) or [])
        previous_need_web = bool(enriched.get("need_web", False))
        enriched["difficulty_level"] = "easy"
        enriched["response_mode"] = "extractive"
        enriched["premise_requirements"] = []
        enriched["analysis_axes"] = []
        enriched["need_web"] = False
        enriched["web_intent"] = ""
        changed = current != "easy" or bool(previous_axes) or previous_need_web
        return enriched, (
            make_debug_log(
                state,
                "planner:difficulty_downgraded_for_direct_line_item",
                previous_difficulty=current or "",
                downgraded_difficulty="easy",
                direct_line_item=direct_line_item.get("canonical", ""),
                table=direct_line_item.get("table", ""),
            )
            if changed
            else None
        )

    # Qualitative / front-matter concepts (going concern, internal control,
    # governance, accounting policy, audit opinion) must NOT be dispatched to the
    # financial analysis agents even when phrased with "đánh giá/phân tích" — those
    # agents return generic profitability figures and the answer goes off-topic
    # (recall=0). Strip financial axes and avoid the hard 4-axis format for them.
    if _is_qualitative_concept_query(user_query) or _is_grounded_interpretation_query(
        user_query
    ):
        previous_axes = list(enriched.get("analysis_axes", []) or [])
        previous_response_mode = str(
            enriched.get("response_mode", "extractive") or "extractive"
        ).strip()
        enriched["analysis_axes"] = []
        if _is_grounded_interpretation_query(user_query):
            # Interpretation is grounded in report premises, but is not a
            # four-axis financial analysis and must not inherit the easy-mode
            # prohibition on bounded inference.
            enriched["difficulty_level"] = "medium"
            enriched["response_mode"] = "grounded_interpretation"
            # The model may emit one broad premise such as "quá trình chuyển
            # đổi/niêm yết".  It can add useful detail, but must not replace
            # the deterministic event slots compiled from the query: each
            # canonical premise is retrieved and bound independently.
            canonical_premises = _grounded_premise_requirements(user_query)
            model_premises = [
                str(item).strip()
                for item in (
                    enriched.get("premise_requirements", []) or []
                )
                if str(item).strip()
            ]
            enriched["premise_requirements"] = _dedupe_keep_order(
                [*canonical_premises, *model_premises]
            )
        else:
            enriched["response_mode"] = "extractive"
            enriched["premise_requirements"] = []
            # A literal qualitative lookup is still an easy retrieval question.
            enriched["difficulty_level"] = "easy"
        changed = (
            current != enriched["difficulty_level"]
            or bool(previous_axes)
            or previous_response_mode != enriched["response_mode"]
        )
        return enriched, (
            make_debug_log(
                state,
                "planner:qualitative_concept_no_financial_axes",
                previous_difficulty=current or "",
                difficulty=enriched.get("difficulty_level", ""),
                response_mode=enriched.get("response_mode", ""),
            )
            if changed
            else None
        )

    if current == "hard":
        return enriched, None

    text_candidates = [
        user_query,
        *(_collect_planner_objectives(enriched)),
    ]
    if not any(_contains_evaluative_intent(text) for text in text_candidates):
        return enriched, None

    enriched["difficulty_level"] = "hard"
    return enriched, make_debug_log(
        state,
        "planner:difficulty_upgraded_by_heuristic",
        previous_difficulty=current or "",
        upgraded_difficulty="hard",
        matched_texts=[text for text in text_candidates if _contains_evaluative_intent(text)][:3],
    )


def _enforce_response_mode_invariants(planner_plan: dict) -> tuple[dict, list[str]]:
    """Guarantee (response_mode, difficulty_level, analysis_axes, premise_requirements)
    form a consistent tuple so the synth format is never chosen from a contradictory
    plan.  This is the deterministic root-cause guard for format misclassification —
    it runs after every planner path (LLM output or fallback), independent of how the
    fields were set.

    Invariants, in precedence order:
      - analysis_axes non-empty  =>  difficulty=hard, response_mode=extractive and
        premise_requirements cleared.
      - difficulty=hard  =>  response_mode=extractive and premise_requirements cleared.
      - only otherwise may grounded_interpretation force difficulty=medium; it also
        requires premise data and no analysis axes.
    """

    plan = dict(planner_plan or {})
    notes: list[str] = []
    mode = str(plan.get("response_mode", "") or "").strip()
    difficulty = _normalize_intent_text(plan.get("difficulty_level", ""))
    axes = [
        item
        for item in (plan.get("analysis_axes", []) or [])
        if isinstance(item, dict) and str(item.get("axis", "") or "").strip()
    ]
    premises = [
        str(item).strip()
        for item in (plan.get("premise_requirements", []) or [])
        if str(item).strip()
    ]

    # Financial analysis wins over a contradictory narrative mode.  Choosing
    # grounded first would silently discard the axes and route a hard assessment
    # directly from raw retrieval facts.
    if axes or difficulty == "hard":
        if axes and difficulty != "hard":
            plan["difficulty_level"] = "hard"
            notes.append("analysis_axes->hard")
        if mode != "extractive":
            plan["response_mode"] = "extractive"
            notes.append("axes_or_hard->extractive")
        if premises:
            plan["premise_requirements"] = []
            notes.append("financial_analysis->clear_premises")
    elif mode == "grounded_interpretation":
        if not premises:
            # Grounded interpretation with no premise data is not a valid narrative
            # answer; fall back to the conservative extractive/easy contract.
            plan["response_mode"] = "extractive"
            plan["difficulty_level"] = "easy"
            plan["analysis_axes"] = []
            plan["premise_requirements"] = []
            notes.append("grounded_without_premise->extractive_easy")
        else:
            if difficulty != "medium":
                plan["difficulty_level"] = "medium"
                notes.append("grounded->medium")

    return plan, notes


def _expand_broad_profitability_axes(
    state: dict,
    planner_plan: dict,
) -> tuple[dict, list[str]]:
    """Add narrowly scoped supporting axes to a broad profitability assessment.

    A request to assess overall profitability needs more than the headline ratios:
    cash conversion tests earnings quality, while operating efficiency explains how
    assets support returns.  Liquidity/solvency is added only when the user explicitly
    asks about sustainability or financial risk.  Metric lookups and standalone ratio
    calculations do not enter this path.
    """

    plan = dict(planner_plan or {})
    user_query = str((state or {}).get("user_query", "") or "").strip()
    broad_profitability = contains_intent(
        user_query,
        BROAD_PROFITABILITY_ASSESSMENT_PATTERNS,
    )
    broad_financial = contains_intent(
        user_query,
        BROAD_FINANCIAL_ASSESSMENT_PATTERNS,
    )
    if (
        str(plan.get("difficulty_level", "") or "").strip().lower() != "hard"
        or str(plan.get("response_mode", "") or "").strip()
        != "extractive"
        or not _contains_evaluative_intent(user_query)
        or not (broad_profitability or broad_financial)
        or direct_line_item_match(user_query) is not None
    ):
        return plan, []

    existing_axes = [
        dict(axis)
        for axis in (plan.get("analysis_axes", []) or [])
        if isinstance(axis, dict) and str(axis.get("axis", "") or "").strip()
    ]
    existing_by_agent = {
        str(axis.get("axis", "") or "").strip(): axis
        for axis in existing_axes
    }

    required_agents = ["agent_profitability"]
    if broad_financial:
        required_agents.extend(
            [
                "agent_liquidity_solvency",
                "agent_cashflow_analysis",
                "agent_efficiency",
            ]
        )
    else:
        required_agents.extend(
            ["agent_cashflow_analysis", "agent_efficiency"]
        )
        if contains_intent(user_query, PROFITABILITY_SUSTAINABILITY_PATTERNS):
            required_agents.append("agent_liquidity_solvency")

    added_agents = []
    for agent in required_agents:
        if agent in existing_by_agent:
            continue
        existing_by_agent[agent] = {
            "axis": agent,
            "objective": PROFITABILITY_AXIS_OBJECTIVES[agent],
        }
        added_agents.append(agent)

    for agent in required_agents:
        required_markers = PROFITABILITY_AXIS_REQUIRED_MARKERS.get(agent, ())
        if not required_markers:
            continue
        axis = existing_by_agent[agent]
        objective = str(axis.get("objective", "") or "").strip()
        objective_text = _normalize_intent_text(objective)
        if any(marker not in objective_text for marker in required_markers):
            axis["objective"] = " ".join(
                part
                for part in (
                    objective.rstrip(". ") + "." if objective else "",
                    PROFITABILITY_AXIS_OBJECTIVES[agent],
                )
                if part
            )

    standard_order = [
        "agent_profitability",
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    ordered_agents = [
        agent for agent in standard_order if agent in existing_by_agent
    ]
    ordered_agents.extend(
        agent for agent in existing_by_agent if agent not in ordered_agents
    )
    plan["analysis_axes"] = [existing_by_agent[agent] for agent in ordered_agents]
    return plan, added_agents


def run_planner(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_planner"]
    trace = []
    started_at = time.perf_counter()
    llm_usage = {}
    planner_completed = False

    start_log = make_debug_log(
        state,
        "planner:start",
        user_query=state.get("user_query", ""),
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": "{}",
        "worker_results_json": "{}",
        "allowed_keywords_json": "{}",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list("agent_planner"),
    }

    updates = {
        "last_agent": "agent_planner",
        "trace": trace,
    }

    try:
        raw_result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=PlannerEvidencePlan,
            plain_payload_factory=_plain_planner_payload,
        )
        llm_usage = extract_usage_metadata(raw_result.get("raw"))
        plan_obj, parse_warning, recovered_from = _coerce_planner_plan(raw_result)
        updates["planner_plan"] = _enrich_plan_fields(state, plan_obj.model_dump())
        updates["planner_plan"], heuristic_log = _apply_planner_difficulty_heuristics(
            state,
            updates["planner_plan"],
        )
        if raw_result.get("mode") != "structured":
            fallback_log = make_debug_log(
                state,
                "planner:structured_output_fallback",
                mode=raw_result.get("mode", "plain_json"),
            )
            if fallback_log:
                updates["trace"].append(fallback_log)
        if heuristic_log:
            updates["trace"].append(heuristic_log)
        if parse_warning and recovered_from:
            debug_log = make_debug_log(
                state,
                "planner:recovered_from_raw",
                source=recovered_from,
                parsing_error=parse_warning,
            )
            if debug_log:
                updates["trace"].append(debug_log)
        planner_completed = True
    except Exception as e:
        updates["planner_plan"] = _enrich_plan_fields(state, DEFAULT_PLANNER_PLAN)
        # Preserve the conservative no-inference fallback for arbitrary planner
        # failures.  The only deterministic promotion is the narrowly scoped
        # narrative mode, otherwise an invalid model output could fabricate
        # financial-analysis axes or difficulty.
        if _is_grounded_interpretation_query(str(state.get("user_query", "") or "")):
            updates["planner_plan"], heuristic_log = _apply_planner_difficulty_heuristics(
                state,
                updates["planner_plan"],
            )
            if heuristic_log:
                updates["trace"].append(heuristic_log)
        updates["trace"].append(
            make_log(
                state,
                "planner:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )

    # Deterministic consistency guard: reconcile response_mode / difficulty /
    # analysis_axes / premise_requirements so synth never selects a format from a
    # contradictory plan (root-cause fix for format misclassification).
    invariant_plan, invariant_notes = _enforce_response_mode_invariants(
        updates.get("planner_plan", {}) or {}
    )
    updates["planner_plan"] = invariant_plan
    if invariant_notes:
        invariant_log = make_debug_log(
            state,
            "planner:response_mode_invariants",
            corrections=invariant_notes,
            response_mode=invariant_plan.get("response_mode", ""),
            difficulty_level=invariant_plan.get("difficulty_level", ""),
        )
        if invariant_log:
            updates["trace"].append(invariant_log)

    # Model-first: a planner call that succeeded owns difficulty, response mode
    # and axes.  The heuristic expansion stays as the fallback for a failed call,
    # and `shadow` keeps legacy behaviour while reporting what it changed.
    base_plan = updates.get("planner_plan", {}) or {}
    if planner_completed and not planner_axis_expansion_enabled():
        model_first_log = make_debug_log(
            state,
            "routing:model_first_planner_axes",
            analysis_axes_n=len(base_plan.get("analysis_axes", []) or []),
        )
        if model_first_log:
            updates["trace"].append(model_first_log)
    else:
        expanded_plan, added_axes = _expand_broad_profitability_axes(state, base_plan)
        updates["planner_plan"] = expanded_plan
        if added_axes:
            expansion_log = make_debug_log(
                state,
                "planner:profitability_axes_expanded",
                added_axes=added_axes,
                analysis_axes_n=len(expanded_plan.get("analysis_axes", []) or []),
            )
            if expansion_log:
                updates["trace"].append(expansion_log)
            if planner_completed and shadow_routing():
                updates["trace"].append(
                    make_log(
                        state,
                        "routing:shadow_diff",
                        stage="planner_axes",
                        heuristic_added_axes=added_axes,
                        model_axes_n=len(base_plan.get("analysis_axes", []) or []),
                    )
                )

    if planner_completed:
        updates["trace"].append(
            make_log(
                state,
                "planner:done",
                **_planner_trace_summary(updates["planner_plan"]),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )

    dataset_id = str((state or {}).get("dataset_id", "") or "").strip()
    dataset = get_dataset(dataset_id) if dataset_id else None
    query_company = str((updates.get("planner_plan", {}) or {}).get("company", "") or "").strip()
    dataset_company = str(getattr(dataset, "company", "") or "").strip()
    if query_company and dataset_company and _normalize_company(query_company) not in _normalize_company(dataset_company):
        debug_log = make_debug_log(
            state,
            "planner:dataset_company_mismatch",
            query_company=query_company,
            dataset_company=dataset_company,
        )
        if debug_log:
            updates["trace"].append(debug_log)

    return updates
