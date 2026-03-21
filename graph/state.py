from typing import TypedDict, Any, Annotated
import operator


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out = dict(left or {})
    out.update(right or {})
    return out

class GraphState(TypedDict, total=False):
    # input
    user_query: str
    debug_trace: bool
    dataset_id: str

    # branch-specific input injected by Send(...)
    worker_query: str

    # sequential/global
    last_agent: str
    last_agent_response: str
    planner_plan: dict
    worker_plan: dict
    expected_workers: list[str]
    followup_rounds: int
    followup_requests: list[dict]
    missing_components: list[str]
    web_summary: str
    synth_context: dict
    synth_web_summary: str
    synth_decision: dict
    final_answer: str

    # collector / routing control
    collect_decision: str

    # parallel / mergeable
    worker_messages: Annotated[list[dict], operator.add]
    tool_observations: Annotated[list[dict], operator.add]
    tool_results: Annotated[list[dict], operator.add]
    worker_results: Annotated[dict[str, Any], merge_dicts]
    done_workers: Annotated[dict[str, int], merge_dicts]
    collected_rounds: Annotated[list[int], operator.add]
    trace: Annotated[list[dict], operator.add]
    tool_call_counts: Annotated[dict[str, dict[str, int]], merge_dicts]
    force_collect_agents: Annotated[dict[str, int], merge_dicts]
