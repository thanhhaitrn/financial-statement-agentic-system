"""Typed state and dependency contracts shared by LangGraph nodes."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from dataclasses import dataclass
from typing import TypedDict, Any, Annotated
import operator


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out = dict(left or {})
    out.update(right or {})
    return out


@dataclass(frozen=True)
class RunContext:
    run_id: str = ""
    dataset_id: str = ""
    index_fingerprint: str = ""
    model_fingerprint: str = ""


@dataclass(frozen=True)
class WorkflowServices:
    """Runtime-only dependencies; these objects never enter graph state."""

    collection: Any = None
    index_fingerprint: str = ""
    model_fingerprint: str = ""

class GraphState(TypedDict, total=False):
    # input
    user_query: str
    debug_trace: bool
    dataset_id: str
    run_id: str
    index_fingerprint: str
    collection_generation: str
    model_fingerprint: str

    # branch-specific input injected by Send(...)
    worker_query: str
    dispatch_target: dict
    analysis_input_results: dict
    evidence_queries: list[dict]

    # sequential/global
    last_agent: str
    last_agent_response: str
    planner_plan: dict
    worker_plan: dict
    evidence_pack: dict
    ragas_facts_by_table: dict
    expected_workers: list[str]
    dispatch_phase: str
    followup_rounds: int
    followup_requests: list[dict]
    analysis_dispatch_targets: list[dict]
    pending_analysis_targets: list[dict]
    missing_components: list[str]
    web_summary: str
    synth_decision: dict
    final_answer: str

    # collector / routing control
    collect_decision: str

    # parallel / mergeable
    worker_messages: Annotated[list[dict], operator.add]
    tool_observations: Annotated[list[dict], operator.add]
    tool_results: Annotated[list[dict], operator.add]
    evidence_cache: Annotated[dict[str, Any], merge_dicts]
    worker_results: Annotated[dict[str, Any], merge_dicts]
    done_workers: Annotated[dict[str, int], merge_dicts]
    collected_rounds: Annotated[list[int], operator.add]
    collected_keys: Annotated[list[str], operator.add]
    trace: Annotated[list[dict], operator.add]
    tool_call_counts: Annotated[dict[str, dict[str, int]], merge_dicts]
    force_collect_agents: Annotated[dict[str, int], merge_dicts]
