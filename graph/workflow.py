from langgraph.graph import StateGraph, END

from graph.state import GraphState
from graph.nodes import (
    agent_planner,
    agent_keyworder,
    agent_bs_node,
    agent_is_node,
    agent_cf_node,
    agent_web_node,
    tools_bs_node,
    tools_is_node,
    tools_cf_node,
    tools_web_node,
    finalize_bs_node,
    finalize_is_node,
    finalize_cf_node,
    finalize_web_node,
    collect_all_workers,
    agent_synth_node,
)
from graph.dispatch_nodes import (
    prepare_dispatch_state,
    prepare_followup_dispatch_state
)
from graph.router import dispatch_workers
from graph.conditions import (
    make_should_continue,
    should_synthesize_after_collect,
    synth_route,
)
from graph.followup_router import (
    dispatch_followups,
)


workflow = StateGraph(state_schema=GraphState)

# Core sequential nodes
workflow.add_node("agent_main", agent_planner)
workflow.add_node("agent_keyworder", agent_keyworder)
workflow.add_node("prepare_dispatch", prepare_dispatch_state)

# Worker nodes
workflow.add_node("agent_bs", agent_bs_node)
workflow.add_node("agent_is", agent_is_node)
workflow.add_node("agent_cf", agent_cf_node)
workflow.add_node("agent_web", agent_web_node)

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

# Collector / synth
workflow.add_node("collect_all", collect_all_workers)
workflow.add_node("agent_synth", agent_synth_node)

# Follow-up prep
workflow.add_node("prepare_followup_dispatch", prepare_followup_dispatch_state)

workflow.set_entry_point("agent_main")

# planner -> keyworder -> prepare_dispatch
workflow.add_edge("agent_main", "agent_keyworder")
workflow.add_edge("agent_keyworder", "prepare_dispatch")

# initial dispatch fan-out
workflow.add_conditional_edges(
    "prepare_dispatch",
    dispatch_workers,
    {
        "agent_bs": "agent_bs",
        "agent_is": "agent_is",
        "agent_cf": "agent_cf",
        "agent_web": "agent_web",
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

# finalize -> collect_all
workflow.add_edge("finalize_bs", "collect_all")
workflow.add_edge("finalize_is", "collect_all")
workflow.add_edge("finalize_cf", "collect_all")
workflow.add_edge("finalize_web", "collect_all")

# collector -> synth or stop
workflow.add_conditional_edges(
    "collect_all",
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
        "followup": "prepare_followup_dispatch",
        "end": END,
    },
)

# follow-up dispatch fan-out
workflow.add_conditional_edges(
    "prepare_followup_dispatch",
    dispatch_followups,
    {
        "agent_bs": "agent_bs",
        "agent_is": "agent_is",
        "agent_cf": "agent_cf",
        "agent_web": "agent_web",
    },
)

agentic_graph = workflow.compile()
