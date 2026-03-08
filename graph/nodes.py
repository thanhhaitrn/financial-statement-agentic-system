from agents.agent_runner import call_agent
from tools.tool_runner import call_tool
import re, json
from agents.planner_runner import run_planner
from agents.synth_runner import run_synth
from agents.keyworder_runner import run_keyworder
from graph.logger import log_step
from graph.router import TABLE_TO_AGENT

# --- Planner ---
def agent_planner(state: dict) -> dict:
    return run_planner(state)

def agent_keyworder(state: dict) -> dict:
    return run_keyworder(state)  # -> state["plan"] with targets/metrics

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
    return run_synth(state)

import re, json
from graph.logger import log_step

def collect_worker_answer(state: dict) -> dict:
    agent = state.get("last_agent", "")
    text_obj = state.get("last_agent_response", "")
    text = text_obj.content if hasattr(text_obj, "content") else str(text_obj or "")

    m = re.search(r"^\s*ANSWER:\s*(.*)$", text, flags=re.MULTILINE | re.DOTALL)

    if m:
        payload = m.group(1).strip()
        kind = "answer"
        preview = payload[:140]
    else:
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
        kind = "fallback"
        preview = text[:140]

    # mark done
    done = set(state.get("done_workers", []))
    done.add(agent)
    state["done_workers"] = list(done)

    # store ordered history
    state.setdefault("worker_messages", []).append({
        "agent": agent,
        "kind": kind,
        "table": TABLE_TO_AGENT.get(agent, ""),   
        "round": state.get("followup_rounds", 0), 
        "payload": payload,                       
    })

    # store latest per agent
    if agent == "agent_web":
        state["web_summary"] = payload
    else:
        state.setdefault("worker_results", {})[agent] = payload

    # log collect event
    expected = state.get("expected_workers", []) or []
    log_step(
        state,
        "collect",
        agent=agent,
        kind=kind,
        done_n=len(state["done_workers"]),
        expected_n=len(expected),
        done=state["done_workers"],
        expected=expected,
        preview=preview,
    )

    return state