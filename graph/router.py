from collections import defaultdict
from langgraph.types import Send


TABLE_TO_AGENT = {
    "BẢNG CÂN ĐỐI KẾ TOÁN": "agent_bs",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": "agent_is",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": "agent_cf",
}


def build_worker_query(
    table: str,
    keywords: list[str],
    company: str = "",
    time_hint: str = "",
) -> str:
    parts = [table] + [k for k in (keywords or []) if k]
    if company:
        parts.append(company)
    if time_hint:
        parts.append(time_hint)
    return " | ".join(parts)


def _group_targets_by_table(plan: dict) -> dict[str, list[str]]:
    targets = plan.get("targets", []) or []
    grouped = defaultdict(list)

    for t in targets:
        table = str(t.get("table", "")).strip()
        if not table:
            continue

        grouped[table].extend(
            [str(k).strip() for k in (t.get("keywords", []) or []) if str(k).strip()]
        )

    return grouped


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def dispatch_workers(state: dict):
    plan = state.get("plan", {}) or {}
    plan_tables = state.get("plan_tables", {}) or {}

    company = plan_tables.get("company", "") or ""
    time_hint = plan_tables.get("time_hint", "") or ""
    need_web = bool(plan_tables.get("need_web", False) or plan.get("need_web", False))

    grouped = _group_targets_by_table(plan)

    jobs = []
    for table, kws in grouped.items():
        worker = TABLE_TO_AGENT.get(table)
        if not worker:
            continue

        jobs.append(
            Send(
                worker,
                {
                    "worker_query": build_worker_query(
                        table,
                        _dedupe_keep_order(kws),
                        company,
                        time_hint,
                    ),
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )

    if need_web:
        jobs.append(
            Send(
                "agent_web",
                {
                    "worker_query": state.get("user_query", ""),
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )

    return jobs