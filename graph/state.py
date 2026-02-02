from typing import TypedDict

class AgentState(TypedDict):
    query: str
    last_agent_response: str
    last_agent: str
    tool_observations: list
    num_steps: int
    user_location: str  