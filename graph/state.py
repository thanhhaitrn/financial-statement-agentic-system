from typing import TypedDict, List, Dict, Any

class AgentState(TypedDict, total=False):
    # input (global)
    user_query: str
    worker_query: str

    # planning (global)
    plan_tables: Dict[str, Any]
    plan: Dict[str, Any]

    # worker-local (IMPORTANT)
    w_last_agent_response: Any
    w_last_agent: str
    w_num_steps: int

    w_tool_observations: List[str]
    w_last_tool_results: Dict[str, Any]
    w_seen_tool_calls: List[Any]

    # worker outputs (global)
    worker_results: Dict[str, Any]
    worker_messages: List[Dict[str, Any]]
    web_summary: str

    # orchestration (global)
    expected_workers: List[str]
    done_workers: List[str]

    # synth/follow-up (global)
    synth_decision: Dict[str, Any]
    followup_requests: List[Dict[str, Any]]
    missing_components: List[str]
    followup_rounds: int

    # debug (global)
    run_id: str
    trace: List[Dict[str, Any]]