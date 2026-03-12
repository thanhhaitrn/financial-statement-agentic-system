# graph/router.py
from collections import defaultdict
from langgraph.types import Send
from graph.logger import log_step
from graph.state_utils import make_child_state  

TABLE_TO_AGENT = {
    "BẢNG CÂN ĐỐI KẾ TOÁN": "agent_bs",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": "agent_is",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": "agent_cf",
}

def build_worker_query(table: str, keywords: list[str], company: str = "", time_hint: str = "") -> str:
    parts = [table] + [k for k in (keywords or []) if k]
    if company:
        parts.append(company)
    if time_hint:
        parts.append(time_hint)
    return " | ".join(parts)

def dispatch_workers(state: dict):
    plan = state.get("plan", {}) or {}
    targets = plan.get("targets", []) or []

    plan_tables = state.get("plan_tables", {}) or {}
    company = plan_tables.get("company", "") or ""
    time_hint = plan_tables.get("time_hint", "") or ""
    need_web = bool(plan_tables.get("need_web", False) or plan.get("need_web", False))

    grouped = defaultdict(list)
    for t in targets:
        table = str(t.get("table", "")).strip()
        grouped[table].extend([k for k in (t.get("keywords", []) or []) if k])

    resolved = []
    expected = set()

    for table, kws in grouped.items():
        worker = TABLE_TO_AGENT.get(table)
        if not worker:
            continue

        # de-dup keywords
        seen = set()
        kws_unique = []
        for k in kws:
            if k not in seen:
                kws_unique.append(k)
                seen.add(k)

        expected.add(worker)
        resolved.append((worker, table, kws_unique))

    if need_web:
        expected.add("agent_web")

    state["expected_workers"] = list(expected)
    state["done_workers"] = []

    log_step(
        state,
        "dispatch",
        expected=state["expected_workers"],
        targets_n=len(targets),
        tables=list(grouped.keys()),
        need_web=need_web,
    )

    jobs = []
    for worker, table, kws in resolved:
        child = make_child_state(state)  
        child["w_worker_query"] = build_worker_query(table, kws, company, time_hint)
        jobs.append(Send(worker, child))

    if need_web:
        child = make_child_state(state)  
        child["w_worker_query"] = state.get("user_query", state.get("query", ""))
        jobs.append(Send("agent_web", child))

    return jobs