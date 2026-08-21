"""Compile the financial-analysis LangGraph with injectable runtime services."""

from __future__ import annotations

from functools import wraps

from langgraph.graph import END, StateGraph

from graph.conditions import make_should_continue, should_synthesize_after_collect, synth_route
from graph.nodes import (
    agent_cashflow_analysis_node,
    agent_efficiency_node,
    agent_liquidity_solvency_node,
    agent_planner,
    agent_profitability_node,
    agent_router,
    agent_synth_node,
    collect_analysis_results_node,
    evidence_pack_node,
    finalize_cashflow_analysis_node,
    finalize_efficiency_node,
    finalize_liquidity_solvency_node,
    finalize_profitability_node,
    tools_cashflow_analysis_node,
    tools_efficiency_node,
    tools_liquidity_solvency_node,
    tools_profitability_node,
)
from graph.router import route_after_evidence
from graph.state import GraphState, WorkflowServices
from tools.tool_runner import set_collection


ANALYSIS_NODES = {
    "agent_profitability": (
        agent_profitability_node,
        "tools_profitability",
        tools_profitability_node,
        "finalize_profitability",
        finalize_profitability_node,
    ),
    "agent_liquidity_solvency": (
        agent_liquidity_solvency_node,
        "tools_liquidity_solvency",
        tools_liquidity_solvency_node,
        "finalize_liquidity_solvency",
        finalize_liquidity_solvency_node,
    ),
    "agent_cashflow_analysis": (
        agent_cashflow_analysis_node,
        "tools_cashflow_analysis",
        tools_cashflow_analysis_node,
        "finalize_cashflow_analysis",
        finalize_cashflow_analysis_node,
    ),
    "agent_efficiency": (
        agent_efficiency_node,
        "tools_efficiency",
        tools_efficiency_node,
        "finalize_efficiency",
        finalize_efficiency_node,
    ),
}


def _bind_services(node, services: WorkflowServices | None):
    if services is None:
        return node

    @wraps(node)
    def bound(state: dict):
        if services.collection is not None:
            set_collection(services.collection)
        scoped_state = dict(state or {})
        if services.index_fingerprint:
            scoped_state.setdefault("index_fingerprint", services.index_fingerprint)
        if services.model_fingerprint:
            scoped_state.setdefault("model_fingerprint", services.model_fingerprint)
        return node(scoped_state)

    return bound


def build_workflow(services: WorkflowServices | None = None) -> StateGraph:
    workflow = StateGraph(state_schema=GraphState)
    workflow.add_node("agent_main", _bind_services(agent_planner, services))
    workflow.add_node("agent_router", _bind_services(agent_router, services))
    workflow.add_node("build_evidence", _bind_services(evidence_pack_node, services))
    workflow.add_node("collect_analysis", _bind_services(collect_analysis_results_node, services))
    workflow.add_node("agent_synth", _bind_services(agent_synth_node, services))

    for agent_name, (agent_node, tool_name, tool_node, finalize_name, finalize_node) in ANALYSIS_NODES.items():
        workflow.add_node(agent_name, _bind_services(agent_node, services))
        workflow.add_node(tool_name, _bind_services(tool_node, services))
        workflow.add_node(finalize_name, _bind_services(finalize_node, services))

    workflow.set_entry_point("agent_main")
    workflow.add_edge("agent_main", "agent_router")
    workflow.add_edge("agent_router", "build_evidence")
    workflow.add_conditional_edges(
        "build_evidence",
        route_after_evidence,
        {
            **{name: name for name in ANALYSIS_NODES},
            "agent_synth": "agent_synth",
            "end": END,
        },
    )

    for agent_name, (_agent_node, tool_name, _tool_node, finalize_name, _finalize_node) in ANALYSIS_NODES.items():
        workflow.add_conditional_edges(
            agent_name,
            make_should_continue(agent_name),
            {"tools": tool_name, "collect": finalize_name},
        )
        workflow.add_edge(tool_name, agent_name)
        workflow.add_edge(finalize_name, "collect_analysis")

    workflow.add_conditional_edges(
        "collect_analysis",
        should_synthesize_after_collect,
        {"synth": "agent_synth", "stop": END},
    )
    workflow.add_conditional_edges(
        "agent_synth",
        synth_route,
        {"followup": "agent_router", "end": END},
    )
    return workflow


def build_graph(services: WorkflowServices | None = None):
    return build_workflow(services).compile()


# Backward-compatible default graph for the existing root CLI.  New services
# should call build_graph(WorkflowServices(...)) per dataset/run.
workflow = build_workflow()
agentic_graph = workflow.compile()
