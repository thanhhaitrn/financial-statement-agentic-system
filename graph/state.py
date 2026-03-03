from typing import TypedDict

class AgentState(TypedDict):
    user_query: str
    query: str
    last_agent_response: str
    last_agent: str
    tool_observations: list
    num_steps: int
    plan: dict 
    worker_results: dict
    web_summary: str 
    expected_workers: list
    done_workers: list