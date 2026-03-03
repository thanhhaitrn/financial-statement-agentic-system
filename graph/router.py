import json
import copy
from langgraph.types import Send

TABLE_TO_AGENT = {
    "BẢNG CÂN ĐỐI KẾ TOÁN": "agent_bs",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": "agent_is",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": "agent_cf"
}

def parse_plan(state: dict) -> dict:
    raw = str(state.get("last_agent_response", "") or "").strip()
    try:
        plan = json.loads(raw)
        assert isinstance(plan, dict)
    except Exception:
        plan = {"targets": [], "metrics": [], "company": "", "time_hint": "", "need_web": False}
    state["plan"] = plan
    state.setdefault("worker_results", {})
    state.setdefault("web_summary", "")
    return state

def build_worker_query(table: str, keywords: list[str], company: str = "", time_hint: str = "") -> str:
    parts = [table] + [k for k in keywords if k]
    if company:
        parts.append(company)
    if time_hint:
        parts.append(time_hint)
    return " | ".join(parts)

def dispatch_workers(state: dict):
    plan = state.get("plan", {}) or {}
    targets = plan.get("targets", []) or []
    company = plan.get("company", "") or ""
    time_hint = plan.get("time_hint", "") or ""

    jobs = []

    # 1) First pass: decide which workers are needed
    expected = set()
    resolved = []  # list of (worker, table, kws)
    for t in targets:
        table = str(t.get("table", "")).strip().upper()
        kws = t.get("keywords", []) or []
        worker = TABLE_TO_AGENT.get(table)
        if not worker:
            continue
        expected.add(worker)
        resolved.append((worker, table, kws))

    if plan.get("need_web"):
        expected.add("agent_web")

    # 2) Set expected/done on the *parent state* BEFORE making children
    state["expected_workers"] = list(expected)
    state["done_workers"] = []

    print("EXPECTED:", state["expected_workers"])

    # 3) Now create children (and copy expected_workers into them)
    for worker, table, kws in resolved:
        child = copy.deepcopy(state)
        child["expected_workers"] = state["expected_workers"]
        child["done_workers"] = []
        child["query"] = build_worker_query(table, kws, company, time_hint)
        child["user_query"] = state.get("user_query", state.get("query", ""))
        child["last_agent_response"] = ""
        child["tool_observations"] = []
        child["num_steps"] = 0
        jobs.append(Send(worker, child))

    if plan.get("need_web"):
        child = copy.deepcopy(state)
        child["expected_workers"] = state["expected_workers"]
        child["done_workers"] = []
        child["query"] = state.get("user_query", state.get("query", ""))
        child["user_query"] = state.get("user_query", state.get("query", ""))
        child["last_agent_response"] = ""
        child["tool_observations"] = []
        child["num_steps"] = 0
        jobs.append(Send("agent_web", child))

    return jobs