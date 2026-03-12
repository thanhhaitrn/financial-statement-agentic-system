import copy

# những key worker-local cần reset mỗi child
WORKER_LOCAL_KEYS = {
    "w_worker_query",
    "w_last_agent_response",
    "w_last_agent",
    "w_num_steps",
    "w_tool_observations",
    "w_last_tool_results",
    "w_seen_tool_calls",
}

def make_child_state(parent: dict) -> dict:
    child = copy.deepcopy(parent)

    # keep a read-only copy for prompts
    child["root_query"] = parent.get("user_query", parent.get("query", ""))

    #  IMPORTANT: remove legacy shared keys if they exist (defensive)
    for k in ["last_agent_response", "last_agent", "num_steps", "tool_observations", "last_tool_results", "seen_tool_calls"]:
        child.pop(k, None)

    # reset worker-local keys
    child["w_worker_query"] = ""  # will be set by router
    child["w_last_agent_response"] = ""
    child["w_last_agent"] = ""
    child["w_num_steps"] = 0
    child["w_tool_observations"] = []
    child["w_last_tool_results"] = {}
    child["w_seen_tool_calls"] = []

    return child