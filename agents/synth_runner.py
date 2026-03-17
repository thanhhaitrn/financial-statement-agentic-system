import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from schemas.agent_outputs import SynthDecision
from graph.logger import make_log
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE


synth_chain = PROMPT_TEMPLATE | llm.with_structured_output(SynthDecision)

DEFAULT_DECISION = {
    "status": "error",
    "answer": "Chưa đủ dữ liệu để trả lời.",
    "missing": [],
    "followups": [],
}


# =========================
# Basic helpers
# =========================

def _to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "content"):
        return str(getattr(raw, "content", "") or "")
    return str(raw)


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _normalize_table_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _is_action_text(text: str) -> bool:
    t = (text or "").strip().upper()
    return t.startswith("ACTION:")


def _extract_first_json_object(text: str) -> Optional[str]:
    """
    Trích object JSON đầu tiên từ text.
    Hữu ích khi model bọc JSON trong prose hoặc code fence.
    """
    if not text:
        return None

    # Bỏ code fences nếu có
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(cleaned)):
        ch = cleaned[i]

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
                return cleaned[start:i + 1]

    return None


def _try_parse_json(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value

    text = _to_text(value).strip()
    if not text:
        return None

    # thử parse trực tiếp
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # thử extract object
    maybe_obj = _extract_first_json_object(text)
    if maybe_obj:
        try:
            parsed = json.loads(maybe_obj)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return None


# =========================
# Worker result normalization
# =========================

def _normalize_fact(raw_fact: Any, fallback_table: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(raw_fact, dict):
        return None

    item_name = str(raw_fact.get("item_name", "")).strip()
    time_hint = str(raw_fact.get("time_hint", "")).strip()
    value = raw_fact.get("value", "")
    source = str(raw_fact.get("source", "")).strip()
    table = str(raw_fact.get("table", fallback_table)).strip()

    if not item_name and value in ("", None):
        return None

    return {
        "item_name": item_name,
        "time_hint": time_hint,
        "value": value,
        "source": source,
        "table": table or fallback_table,
    }


def _normalize_worker_result(raw: Any, agent_name: str = "") -> Tuple[Dict[str, Any], str]:
    """
    Trả về:
    - normalized result dict
    - kind: structured | json_text | action_pending | fallback
    """
    if isinstance(raw, dict):
        table = str(raw.get("table", "")).strip()
        facts = raw.get("facts", [])
        normalized_facts = []

        if isinstance(facts, list):
            for f in facts:
                nf = _normalize_fact(f, fallback_table=table)
                if nf:
                    normalized_facts.append(nf)

        return (
            {
                "agent": agent_name,
                "table": table,
                "facts": normalized_facts,
                "raw_text": "",
                "action_pending": False,
            },
            "structured",
        )

    text = _to_text(raw).strip()

    if not text:
        return (
            {
                "agent": agent_name,
                "table": "",
                "facts": [],
                "raw_text": "",
                "action_pending": False,
            },
            "fallback",
        )

    if _is_action_text(text):
        return (
            {
                "agent": agent_name,
                "table": "",
                "facts": [],
                "raw_text": text,
                "action_pending": True,
            },
            "action_pending",
        )

    parsed = _try_parse_json(text)
    if parsed is not None:
        table = str(parsed.get("table", "")).strip()
        facts = parsed.get("facts", [])
        normalized_facts = []

        if isinstance(facts, list):
            for f in facts:
                nf = _normalize_fact(f, fallback_table=table)
                if nf:
                    normalized_facts.append(nf)

        return (
            {
                "agent": agent_name,
                "table": table,
                "facts": normalized_facts,
                "raw_text": text,
                "action_pending": False,
            },
            "json_text",
        )

    return (
        {
            "agent": agent_name,
            "table": "",
            "facts": [],
            "raw_text": text,
            "action_pending": False,
        },
        "fallback",
    )


def _normalize_all_worker_results(worker_results: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    normalized = {}
    logs = []

    for agent_name, raw in (worker_results or {}).items():
        item, kind = _normalize_worker_result(raw, agent_name=agent_name)
        normalized[agent_name] = item
        logs.append(
            {
                "event": "synth:normalize_worker_result",
                "agent": agent_name,
                "kind": kind,
                "facts_n": len(item.get("facts", [])),
                "action_pending": item.get("action_pending", False),
            }
        )

    return normalized, logs


def _flatten_facts(normalized_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for item in (normalized_results or {}).values():
        if isinstance(item, dict):
            for fact in item.get("facts", []) or []:
                if isinstance(fact, dict):
                    facts.append(fact)
    return facts


def _facts_by_table(normalized_results: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for fact in _flatten_facts(normalized_results):
        table = _normalize_table_name(fact.get("table", ""))
        out.setdefault(table, []).append(fact)
    return out


def _build_facts_summary(normalized_results: Dict[str, Any]) -> str:
    lines: List[str] = []

    for table, facts in _facts_by_table(normalized_results).items():
        lines.append(f"[{table}]")
        for fact in facts:
            item_name = fact.get("item_name", "")
            time_hint = fact.get("time_hint", "")
            value = fact.get("value", "")
            source = fact.get("source", "")
            lines.append(
                f"- item_name={item_name}; time_hint={time_hint}; value={value}; source={source}"
            )
        lines.append("")

    return "\n".join(lines).strip()


# =========================
# Numeric helpers for fallback reasoning
# =========================

def _parse_decimal_maybe(value: Any) -> Optional[Decimal]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # bỏ đơn vị chữ nếu có
    text = text.replace(" ", "")
    text = text.replace(",", "")
    text = text.replace(".", "")

    # giữ lại dấu âm và chữ số
    text = re.sub(r"[^\d\-]", "", text)

    if not text or text == "-":
        return None

    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _find_best_fact(
    facts: List[Dict[str, Any]],
    keywords_any: List[str],
    table_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    table_hint_norm = _normalize_table_name(table_hint or "")
    best = None

    for fact in facts:
        item_name = str(fact.get("item_name", "")).lower()
        table = _normalize_table_name(fact.get("table", ""))

        if table_hint_norm and table != table_hint_norm:
            continue

        if any(k.lower() in item_name for k in keywords_any):
            best = fact
            # ưu tiên fact có time_hint
            if fact.get("time_hint"):
                return fact

    return best


def _heuristic_answer_from_facts(user_query: str, facts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Fallback nếu LLM synth fail hoặc suy luận sai.
    Hiện hỗ trợ chắc nhất cho query ROE.
    """
    q = (user_query or "").lower()

    if "roe" not in q:
        return None

    equity_fact = _find_best_fact(
        facts,
        keywords_any=["vốn chủ sở hữu", "tổng vốn chủ sở hữu"],
        table_hint="BẢNG CÂN ĐỐI KẾ TOÁN",
    )
    profit_fact = _find_best_fact(
        facts,
        keywords_any=[
            "lợi nhuận sau thuế",
            "lợi nhuận sau thuế thu nhập doanh nghiệp",
            "lợi nhuận ròng",
        ],
        table_hint="BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    )

    missing = []
    if not equity_fact:
        missing.append("vốn chủ sở hữu")
    if not profit_fact:
        missing.append("lợi nhuận sau thuế")

    if missing:
        return {
            "status": "need_more",
            "answer": "Chưa đủ dữ liệu để tính ROE.",
            "missing": missing,
            "followups": [],
        }

    equity_val = _parse_decimal_maybe(equity_fact.get("value"))
    profit_val = _parse_decimal_maybe(profit_fact.get("value"))

    if equity_val is None or profit_val is None or equity_val == 0:
        return {
            "status": "need_more",
            "answer": "Đã tìm thấy chỉ tiêu cần thiết nhưng chưa chuẩn hóa được giá trị số để tính ROE.",
            "missing": [],
            "followups": [],
        }

    roe = (profit_val / equity_val) * Decimal("100")

    answer = (
        f"ROE xấp xỉ = {profit_fact.get('value')} / {equity_fact.get('value')} = "
        f"{roe.quantize(Decimal('0.01'))}%.\n"
        f"Lưu ý: kết quả này đang dùng vốn chủ sở hữu tại thời điểm {equity_fact.get('time_hint', '')} "
        f"thay vì vốn chủ sở hữu bình quân đầu kỳ-cuối kỳ, nên đây là ROE xấp xỉ chứ chưa phải ROE chuẩn nếu bạn yêu cầu tính theo bình quân."
    )

    return {
        "status": "answer",
        "answer": answer,
        "missing": [],
        "followups": [],
    }


def _coerce_decision(value: Any) -> Dict[str, Any]:
    if value is None:
        return dict(DEFAULT_DECISION)

    if isinstance(value, dict):
        out = dict(DEFAULT_DECISION)
        out.update(value)
        out["missing"] = out.get("missing") or []
        out["followups"] = out.get("followups") or []
        return out

    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        out = dict(DEFAULT_DECISION)
        out.update(dumped)
        out["missing"] = out.get("missing") or []
        out["followups"] = out.get("followups") or []
        return out

    return dict(DEFAULT_DECISION)


# =========================
# Main
# =========================

def run_synth(state: dict) -> dict:
    start_log = make_log(
        state,
        "synth:start",
        followup_rounds=state.get("followup_rounds", 0),
    )

    profile = AGENT_PROFILES["agent_synth"]

    raw_worker_results = state.get("worker_results", {}) or {}
    normalized_worker_results, normalize_logs = _normalize_all_worker_results(raw_worker_results)
    facts = _flatten_facts(normalized_worker_results)
    facts_summary = _build_facts_summary(normalized_worker_results)

    payload = {
        "role": profile["role"],
        "tools_list": "",
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "w_worker_query": "",  # để tương thích nếu prompt cũ còn field này
        "plan_json": _safe_json_dumps(state.get("plan", {})),
        "worker_results_json": _safe_json_dumps(normalized_worker_results),
        "web_summary": state.get("web_summary", "") or "",
        "last_agent_response": state.get("last_agent_response", "") or "",
        "tool_observations": facts_summary,
    }

    decision: Dict[str, Any]

    try:
        raw_decision = synth_chain.invoke(payload)
        decision = _coerce_decision(raw_decision)
    except ValidationError as e:
        decision = {
            "status": "error",
            "answer": f"Synth trả về sai schema: {e}",
            "missing": [],
            "followups": [],
        }
    except Exception as e:
        decision = {
            "status": "error",
            "answer": f"Lỗi khi chạy synth: {e}",
            "missing": [],
            "followups": [],
        }

    # Nếu synth vẫn báo thiếu nhưng facts thực ra đã có, thử fallback heuristic
    if decision.get("status") in {"need_more", "error"} and facts:
        heuristic = _heuristic_answer_from_facts(state.get("user_query", ""), facts)
        if heuristic is not None:
            # chỉ override khi heuristic tốt hơn
            if heuristic.get("status") == "answer":
                decision = heuristic
            elif decision.get("status") == "error":
                decision = heuristic

    done_log = make_log(
        state,
        "synth:done",
        status=decision.get("status", ""),
        followups_n=len(decision.get("followups", []) or []),
        facts_n=len(facts),
        answer_preview=(decision.get("answer", "") or "")[:200],
    )

    return {
        "synth_decision": decision,
        "last_agent_response": decision.get("answer", ""),
        "normalized_worker_results": normalized_worker_results,
        "trace": [start_log, *normalize_logs, done_log],
    }