"""Central registry for agent capabilities and their default data scopes."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from __future__ import annotations

from typing import Dict

AGENT_METADATA: Dict[str, dict] = {
    "agent_planner": {
        "kind": "planner",
        "supports_tools": False,
    },
    "agent_router": {
        "kind": "router",
        "supports_tools": False,
    },
    "agent_profitability": {
        "kind": "analysis",
        "supports_tools": True,
    },
    "agent_liquidity_solvency": {
        "kind": "analysis",
        "supports_tools": True,
    },
    "agent_cashflow_analysis": {
        "kind": "analysis",
        "supports_tools": True,
    },
    "agent_efficiency": {
        "kind": "analysis",
        "supports_tools": True,
    },
    "agent_synth": {
        "kind": "synth",
        "supports_tools": False,
    },
}

ANALYSIS_AGENTS = {
    agent_name
    for agent_name, meta in AGENT_METADATA.items()
    if str(meta.get("kind", "")).strip() == "analysis"
}
ROUTABLE_AGENTS = set(ANALYSIS_AGENTS)


def get_agent_kind(agent_name: str) -> str:
    return str((AGENT_METADATA.get(str(agent_name or "").strip(), {}) or {}).get("kind", "") or "").strip()


def is_analysis_agent(agent_name: str) -> bool:
    return get_agent_kind(agent_name) == "analysis"
