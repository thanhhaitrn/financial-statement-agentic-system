"""Compile the financial-analysis LangGraph workflow and its node edges."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from langgraph.graph import StateGraph, END

from graph.state import GraphState
from graph.nodes import (
    agent_planner,
    agent_router,
    evidence_pack_node,
    agent_profitability_node,
    agent_liquidity_solvency_node,
    agent_cashflow_analysis_node,
    agent_efficiency_node,
    tools_profitability_node,
    tools_liquidity_solvency_node,
    tools_cashflow_analysis_node,
    tools_efficiency_node,
    finalize_profitability_node,
    finalize_liquidity_solvency_node,
    finalize_cashflow_analysis_node,
    finalize_efficiency_node,
    collect_analysis_results_node,
    agent_synth_node,
)
from graph.router import route_after_evidence
from graph.conditions import (
    make_should_continue,
    should_synthesize_after_collect,
    synth_route,
)


workflow = StateGraph(state_schema=GraphState)

# Core sequential nodes
workflow.add_node("agent_main", agent_planner)
workflow.add_node("agent_router", agent_router)
workflow.add_node("build_evidence", evidence_pack_node)

# Analysis nodes
workflow.add_node("agent_profitability", agent_profitability_node)
workflow.add_node("agent_liquidity_solvency", agent_liquidity_solvency_node)
workflow.add_node("agent_cashflow_analysis", agent_cashflow_analysis_node)
workflow.add_node("agent_efficiency", agent_efficiency_node)

# Analysis fallback tools
workflow.add_node("tools_profitability", tools_profitability_node)
workflow.add_node("tools_liquidity_solvency", tools_liquidity_solvency_node)
workflow.add_node("tools_cashflow_analysis", tools_cashflow_analysis_node)
workflow.add_node("tools_efficiency", tools_efficiency_node)

# Finalize analysis nodes
workflow.add_node("finalize_profitability", finalize_profitability_node)
workflow.add_node("finalize_liquidity_solvency", finalize_liquidity_solvency_node)
workflow.add_node("finalize_cashflow_analysis", finalize_cashflow_analysis_node)
workflow.add_node("finalize_efficiency", finalize_efficiency_node)

# Collector / synth
workflow.add_node("collect_analysis", collect_analysis_results_node)
workflow.add_node("agent_synth", agent_synth_node)

workflow.set_entry_point("agent_main")

# planner -> router/evidence planner -> shared evidence pack
workflow.add_edge("agent_main", "agent_router")
workflow.add_edge("agent_router", "build_evidence")

workflow.add_conditional_edges(
    "build_evidence",
    route_after_evidence,
    {
        "agent_profitability": "agent_profitability",
        "agent_liquidity_solvency": "agent_liquidity_solvency",
        "agent_cashflow_analysis": "agent_cashflow_analysis",
        "agent_efficiency": "agent_efficiency",
        "agent_synth": "agent_synth",
        "end": END,
    },
)

# analysis loops
workflow.add_conditional_edges(
    "agent_profitability",
    make_should_continue("agent_profitability"),
    {
        "tools": "tools_profitability",
        "collect": "finalize_profitability",
    },
)
workflow.add_conditional_edges(
    "agent_liquidity_solvency",
    make_should_continue("agent_liquidity_solvency"),
    {
        "tools": "tools_liquidity_solvency",
        "collect": "finalize_liquidity_solvency",
    },
)
workflow.add_conditional_edges(
    "agent_cashflow_analysis",
    make_should_continue("agent_cashflow_analysis"),
    {
        "tools": "tools_cashflow_analysis",
        "collect": "finalize_cashflow_analysis",
    },
)
workflow.add_conditional_edges(
    "agent_efficiency",
    make_should_continue("agent_efficiency"),
    {
        "tools": "tools_efficiency",
        "collect": "finalize_efficiency",
    },
)

# tool -> same analysis worker
workflow.add_edge("tools_profitability", "agent_profitability")
workflow.add_edge("tools_liquidity_solvency", "agent_liquidity_solvency")
workflow.add_edge("tools_cashflow_analysis", "agent_cashflow_analysis")
workflow.add_edge("tools_efficiency", "agent_efficiency")

# finalize analysis -> collect_analysis
workflow.add_edge("finalize_profitability", "collect_analysis")
workflow.add_edge("finalize_liquidity_solvency", "collect_analysis")
workflow.add_edge("finalize_cashflow_analysis", "collect_analysis")
workflow.add_edge("finalize_efficiency", "collect_analysis")

# collect analysis -> synth or stop
workflow.add_conditional_edges(
    "collect_analysis",
    should_synthesize_after_collect,
    {
        "synth": "agent_synth",
        "stop": END,
    },
)

# synth -> follow-up or end
workflow.add_conditional_edges(
    "agent_synth",
    synth_route,
    {
        "followup": "agent_router",
        "end": END,
    },
)

agentic_graph = workflow.compile()
