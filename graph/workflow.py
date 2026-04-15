from langgraph.graph import StateGraph, END

from graph.state import GraphState
from graph.nodes import (
    agent_planner,
    agent_router,
    agent_bs_node,
    agent_is_node,
    agent_cf_node,
    agent_web_node,
    agent_profitability_node,
    agent_liquidity_solvency_node,
    agent_cashflow_analysis_node,
    agent_efficiency_node,
    tools_bs_node,
    tools_is_node,
    tools_cf_node,
    tools_web_node,
    finalize_bs_node,
    finalize_is_node,
    finalize_cf_node,
    finalize_web_node,
    finalize_profitability_node,
    finalize_liquidity_solvency_node,
    finalize_cashflow_analysis_node,
    finalize_efficiency_node,
    collect_analysis_results_node,
    collect_worker_results_node,
    agent_synth_node,
)
from graph.router import route_after_router, route_after_worker_collect
from graph.conditions import (
    make_should_continue,
    should_synthesize_after_collect,
    synth_route,
)


workflow = StateGraph(state_schema=GraphState)

# Core sequential nodes
workflow.add_node("agent_main", agent_planner)
workflow.add_node("agent_router", agent_router)

# Worker nodes
workflow.add_node("agent_bs", agent_bs_node)
workflow.add_node("agent_is", agent_is_node)
workflow.add_node("agent_cf", agent_cf_node)
workflow.add_node("agent_web", agent_web_node)
workflow.add_node("agent_profitability", agent_profitability_node)
workflow.add_node("agent_liquidity_solvency", agent_liquidity_solvency_node)
workflow.add_node("agent_cashflow_analysis", agent_cashflow_analysis_node)
workflow.add_node("agent_efficiency", agent_efficiency_node)

# Tool nodes per worker
workflow.add_node("tools_bs", tools_bs_node)
workflow.add_node("tools_is", tools_is_node)
workflow.add_node("tools_cf", tools_cf_node)
workflow.add_node("tools_web", tools_web_node)

# Finalize nodes per worker
workflow.add_node("finalize_bs", finalize_bs_node)
workflow.add_node("finalize_is", finalize_is_node)
workflow.add_node("finalize_cf", finalize_cf_node)
workflow.add_node("finalize_web", finalize_web_node)
workflow.add_node("finalize_profitability", finalize_profitability_node)
workflow.add_node("finalize_liquidity_solvency", finalize_liquidity_solvency_node)
workflow.add_node("finalize_cashflow_analysis", finalize_cashflow_analysis_node)
workflow.add_node("finalize_efficiency", finalize_efficiency_node)

# Collector / synth
workflow.add_node("collect_workers", collect_worker_results_node)
workflow.add_node("collect_analysis", collect_analysis_results_node)
workflow.add_node("agent_synth", agent_synth_node)

workflow.set_entry_point("agent_main")

# planner -> router -> retrieval workers
workflow.add_edge("agent_main", "agent_router")

# initial dispatch fan-out
workflow.add_conditional_edges(
    "agent_router",
    route_after_router,
    {
        "agent_bs": "agent_bs",
        "agent_is": "agent_is",
        "agent_cf": "agent_cf",
        "agent_web": "agent_web",
        "end": END,
    },
)

# worker loops
workflow.add_conditional_edges(
    "agent_bs",
    make_should_continue("agent_bs"),
    {
        "tools": "tools_bs",
        "collect": "finalize_bs",
    },
)
workflow.add_conditional_edges(
    "agent_is",
    make_should_continue("agent_is"),
    {
        "tools": "tools_is",
        "collect": "finalize_is",
    },
)
workflow.add_conditional_edges(
    "agent_cf",
    make_should_continue("agent_cf"),
    {
        "tools": "tools_cf",
        "collect": "finalize_cf",
    },
)
workflow.add_conditional_edges(
    "agent_web",
    make_should_continue("agent_web"),
    {
        "tools": "tools_web",
        "collect": "finalize_web",
    },
)

# tool -> same worker
workflow.add_edge("tools_bs", "agent_bs")
workflow.add_edge("tools_is", "agent_is")
workflow.add_edge("tools_cf", "agent_cf")
workflow.add_edge("tools_web", "agent_web")

# finalize worker -> collect_workers
workflow.add_edge("finalize_bs", "collect_workers")
workflow.add_edge("finalize_is", "collect_workers")
workflow.add_edge("finalize_cf", "collect_workers")
workflow.add_edge("finalize_web", "collect_workers")

# finalize analysis -> collect_analysis
workflow.add_edge("finalize_profitability", "collect_analysis")
workflow.add_edge("finalize_liquidity_solvency", "collect_analysis")
workflow.add_edge("finalize_cashflow_analysis", "collect_analysis")
workflow.add_edge("finalize_efficiency", "collect_analysis")

# collect worker -> analysis, synth or stop
workflow.add_conditional_edges(
    "collect_workers",
    route_after_worker_collect,
    {
        "agent_profitability": "agent_profitability",
        "agent_liquidity_solvency": "agent_liquidity_solvency",
        "agent_cashflow_analysis": "agent_cashflow_analysis",
        "agent_efficiency": "agent_efficiency",
        "agent_synth": "agent_synth",
        "end": END,
    },
)

workflow.add_edge("agent_profitability", "finalize_profitability")
workflow.add_edge("agent_liquidity_solvency", "finalize_liquidity_solvency")
workflow.add_edge("agent_cashflow_analysis", "finalize_cashflow_analysis")
workflow.add_edge("agent_efficiency", "finalize_efficiency")

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
#graph = agentic_graph.get_graph()
#rint(graph.draw_mermaid())