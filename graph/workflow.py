from langgraph.graph import StateGraph, END

from graph.state import AgentState
from graph.conditions import should_continue, which_agents
from agents.agent_runner import call_agent
from tools.tool_runner import call_tool  # wherever you put this

# === Define nodes ===
def call_agent_main(state: AgentState):
    return call_agent(state, agent_name="agent_main")

def call_agent_loan(state: AgentState):
    return call_agent(state, agent_name="agent_loan")

def call_tools(state: AgentState):
    return call_tool(state)


# === Workflow Graph ===
workflow = StateGraph(state_schema=AgentState)

workflow.add_node("agent_main", call_agent_main)
workflow.add_node("agent_loan", call_agent_loan)
workflow.add_node("tools", call_tools)

workflow.set_entry_point("agent_main")

workflow.add_conditional_edges(
    "agent_main",
    should_continue,
    {
        "continue": "tools",
        "handoff": "agent_loan",
        "end": END
    }
)

workflow.add_conditional_edges(
    "agent_loan",
    should_continue,
    {
        "continue": "tools",
        "handoff": "agent_main",
        "end": END
    }
)

workflow.add_conditional_edges(
    "tools",
    which_agents,
    {
        "agent_main": "agent_main",
        "agent_loan": "agent_loan"
    }
)

agentic_graph = workflow.compile()