from typing import TypedDict, List, Dict, Any

class AgentState(TypedDict, total=False):
    # input
    user_query: str
    query: str # Query for retrieval

    # last step
    last_agent_response: Any   # can be str or ChatMessage
    last_agent: str
    num_steps: int

    # planning
    plan_tables: Dict[str, Any]   # tables-only planner output
    plan: Dict[str, Any]          # keyworder output (targets/metrics)

    # tools
    tool_observations: List[str]
    last_tool_results: Dict[str, Any]
    seen_tool_calls: List[Any]

    # worker outputs
    worker_results: Dict[str, Any]     # keyed by agent name
    worker_messages: List[Dict[str, Any]]  # ✅ ordered history list
    web_summary: str

    # barrier / orchestration
    expected_workers: List[str]
    done_workers: List[str]

    # synth follow-up
    synth_decision: Dict[str, Any]
    followup_requests: List[Dict[str, Any]]
    missing_components: List[str]
    followup_rounds: int

    # debug
    run_id: str
    trace: List[Dict[str, Any]]