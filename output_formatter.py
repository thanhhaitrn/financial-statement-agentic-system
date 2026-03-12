def format_final_answer(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    status = d.get("status", "answer")

    if status == "need_more":
        missing = d.get("missing", []) or []
        lines = ["ANSWER: Chưa đủ dữ liệu để trả lời."]
        if missing:
            lines.append("Thiếu:")
            lines.extend([f"- {x}" for x in missing])
        return "\n".join(lines)

    ans = (d.get("answer") or "").strip()
    if not ans:
        ans = "Chưa đủ dữ liệu để trả lời."
    if not ans.lower().startswith("answer:"):
        ans = "ANSWER: " + ans
    return ans