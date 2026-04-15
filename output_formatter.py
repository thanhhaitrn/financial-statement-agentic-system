def _dedupe_keep_order(items):
    seen = set()
    output = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _format_answer_prefix(answer: str) -> str:
    text = str(answer or "").strip()
    if not text:
        text = "Chưa đủ dữ liệu để trả lời."
    if not text.lower().startswith("answer:"):
        text = "ANSWER: " + text
    return text


def format_final_answer(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    status = d.get("status", "answer")
    answer = str(d.get("answer", "") or "").strip()

    if status == "need_more":
        missing = list(d.get("missing", []) or [])
        for followup in (d.get("followups", []) or []):
            if not isinstance(followup, dict):
                continue
            missing.extend(followup.get("requirements", []) or [])

        lines = [_format_answer_prefix(answer)]
        missing = _dedupe_keep_order(missing)
        if missing:
            lines.append("Còn thiếu dữ liệu:")
            lines.extend([f"- {x}" for x in missing])
        return "\n".join(lines)

    return _format_answer_prefix(answer)
