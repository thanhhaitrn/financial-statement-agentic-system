from langgraph.graph import StateGraph, END
from graph.nodes import agent_planner, agent_bs_node, agent_cf_node, agent_is_node, agent_synth_node, agent_web_node, tools_node, collect_worker_answer
from graph.state import AgentState
from graph.router import dispatch_workers
from graph.conditions import should_continue, which_agents, ready_to_synthesize

workflow = StateGraph(state_schema=AgentState)

workflow.add_node("agent_main", agent_planner)  # Agent1
workflow.add_node("dispatch", lambda s: s)          
workflow.add_node("agent_bs", agent_bs_node)         # Agent2
workflow.add_node("agent_is", agent_is_node)         # Agent3
workflow.add_node("agent_cf", agent_cf_node)         # Agent4
workflow.add_node("agent_web", agent_web_node)       # Agent6
workflow.add_node("agent_synth", agent_synth_node)   # Agent5

workflow.add_node("tools", tools_node) 
workflow.add_node("collect", collect_worker_answer)
workflow.add_node("barrier", lambda s: s)

workflow.set_entry_point("agent_main")

# planner -> dispatch
workflow.add_edge("agent_main", "dispatch")

# dispatch fan-out
workflow.add_conditional_edges(
    "dispatch",
    dispatch_workers,
    {
        "agent_bs": "agent_bs",
        "agent_is": "agent_is",
        "agent_cf": "agent_cf",
        "agent_web": "agent_web",
    }
)

# workers -> tools or synth/end
for w in ["agent_bs", "agent_is", "agent_cf", "agent_web"]:
    workflow.add_conditional_edges(
        w,
        should_continue,
        {
            "tools": "tools",
            "collect": "collect",
            "end": END
        }
    )

# tools -> back to the worker that requested it
workflow.add_conditional_edges(
    "tools",
    which_agents,
    {
        "agent_bs": "agent_bs",
        "agent_is": "agent_is",
        "agent_cf": "agent_cf",
        "agent_web": "agent_web",
    }
)

# synth ends
workflow.add_edge("collect", "barrier")
workflow.add_edge("agent_synth", END)
workflow.add_conditional_edges(
    "barrier",
    ready_to_synthesize,
    {"synth": "agent_synth", "wait": END}
)

agentic_graph = workflow.compile()

g = agentic_graph.get_graph()
# print(g.draw_mermaid())