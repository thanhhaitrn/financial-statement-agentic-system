import copy

GLOBAL_KEYS = {
    "user_query",
    "expected_workers",
    "done_workers",
    "trace",
    "run_step",
    "run_id",
    "plan_tables",
}

def make_child_state(parent: dict) -> dict:
    child = copy.deepcopy(parent)

    # keep a read-only copy for prompts if needed
    child["root_query"] = parent.get("user_query", parent.get("query", ""))

    # remove global-only keys to avoid concurrent update errors
    for k in GLOBAL_KEYS:
        child.pop(k, None)

    # reset worker-local fields
    child["tool_observations"] = []
    child["seen_tool_calls"] = []
    child["num_steps"] = 0
    child["last_agent_response"] = ""

    return child