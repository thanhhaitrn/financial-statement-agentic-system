from agents.agent_runner import call_agent
from graph.router import parse_plan
from tools.tool_runner import call_tool
import re, json

# --- Planner ---
def agent_planner(state: dict) -> dict:
    # agent_main = planner JSON-only
    state = call_agent(state, agent_name="agent_planner")
    return parse_plan(state)

def agent_bs_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_bs")

def agent_is_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_is")

def agent_cf_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_cf")

def agent_web_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_web")

def tools_node(state: dict) -> dict:
    return call_tool(state)

def agent_synth_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_synth")

def collect_worker_answer(state: dict) -> dict:
    agent = state.get("last_agent", "")
    text_obj = state.get("last_agent_response", "")
    text = text_obj.content if hasattr(text_obj, "content") else str(text_obj or "")

    m = re.search(r"^\s*ANSWER:\s*(.*)$", text, flags=re.MULTILINE | re.DOTALL)

    if m:
        payload = m.group(1).strip()
    else:
        # Fallback: store something useful for synth (no hallucination)
        obs = state.get("tool_observations") or []
        obs_tail = obs[-2:] if len(obs) >= 2 else obs[:]

        payload = json.dumps(
            {
                "error": "worker did not return ANSWER",
                "raw": text[:300],
                "observations": [o[:1200] for o in obs_tail],  
            },
            ensure_ascii=False
        )

    done = set(state.get("done_workers", []))
    done.add(agent)
    state["done_workers"] = list(done)

    if agent == "agent_web":
        state["web_summary"] = payload
    else:
        state.setdefault("worker_results", {})[agent] = payload

    #print("DONE:", state.get("done_workers"))
    return state