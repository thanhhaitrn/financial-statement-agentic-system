import copy
from langgraph.types import Send

TABLE_TO_AGENT = {
    "BẢNG CÂN ĐỐI KẾ TOÁN": "agent_bs",
    "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": "agent_is",
    "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": "agent_cf",
}

def dispatch_followups(state: dict):
    reqs = state.get("followup_requests", []) or []
    expected = set()

    # compute expected first
    resolved = []
    for r in reqs:
        table = str(r.get("table", "")).strip()
        q = str(r.get("query", "")).strip()
        worker = TABLE_TO_AGENT.get(table)
        if worker and q:
            expected.add(worker)
            resolved.append((worker, q))

    state["expected_workers"] = list(expected)
    state["done_workers"] = []

    jobs = []
    for worker, q in resolved:
        child = copy.deepcopy(state)
        child["expected_workers"] = state["expected_workers"]
        child["done_workers"] = []
        child["query"] = q
        child["last_agent_response"] = ""
        child["tool_observations"] = []
        child["num_steps"] = 0
        jobs.append(Send(worker, child))

    return jobs