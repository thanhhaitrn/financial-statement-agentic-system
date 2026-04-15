from __future__ import annotations

from typing import Dict

from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS


AGENT_METADATA: Dict[str, dict] = {
    "agent_planner": {
        "kind": "planner",
        "default_table": "",
        "supports_tools": False,
    },
    "agent_router": {
        "kind": "router",
        "default_table": "",
        "supports_tools": False,
    },
    "agent_bs": {
        "kind": "retrieval",
        "default_table": TABLE_BS,
        "supports_tools": True,
    },
    "agent_is": {
        "kind": "retrieval",
        "default_table": TABLE_IS,
        "supports_tools": True,
    },
    "agent_cf": {
        "kind": "retrieval",
        "default_table": TABLE_CF,
        "supports_tools": True,
    },
    "agent_web": {
        "kind": "retrieval",
        "default_table": "",
        "supports_tools": True,
    },
    "agent_profitability": {
        "kind": "analysis",
        "default_table": TABLE_IS,
        "supports_tools": False,
    },
    "agent_liquidity_solvency": {
        "kind": "analysis",
        "default_table": TABLE_BS,
        "supports_tools": False,
    },
    "agent_cashflow_analysis": {
        "kind": "analysis",
        "default_table": TABLE_CF,
        "supports_tools": False,
    },
    "agent_efficiency": {
        "kind": "analysis",
        "default_table": TABLE_IS,
        "supports_tools": False,
    },
    "agent_synth": {
        "kind": "synth",
        "default_table": "",
        "supports_tools": False,
    },
}

RETRIEVAL_AGENTS = {
    agent_name
    for agent_name, meta in AGENT_METADATA.items()
    if str(meta.get("kind", "")).strip() == "retrieval"
}
ANALYSIS_AGENTS = {
    agent_name
    for agent_name, meta in AGENT_METADATA.items()
    if str(meta.get("kind", "")).strip() == "analysis"
}
ROUTABLE_AGENTS = RETRIEVAL_AGENTS | ANALYSIS_AGENTS


def get_agent_kind(agent_name: str) -> str:
    return str((AGENT_METADATA.get(str(agent_name or "").strip(), {}) or {}).get("kind", "") or "").strip()


def get_default_table(agent_name: str) -> str:
    return str((AGENT_METADATA.get(str(agent_name or "").strip(), {}) or {}).get("default_table", "") or "").strip()


def is_retrieval_agent(agent_name: str) -> bool:
    return get_agent_kind(agent_name) == "retrieval"


def is_analysis_agent(agent_name: str) -> bool:
    return get_agent_kind(agent_name) == "analysis"
