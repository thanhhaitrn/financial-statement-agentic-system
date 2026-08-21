"""Central registry for agent capabilities and their default data scopes."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


AgentKind = Literal["planner", "router", "analysis", "synth"]

_ANALYSIS_TOOL_NAMES = (
    "get_balance_sheet_info",
    "get_income_statement_info",
    "get_cashflow_info",
    "get_note_info",
)


@dataclass(frozen=True)
class AgentSpec:
    """Immutable source of truth for one runnable agent's capabilities."""

    name: str
    kind: AgentKind
    supports_tools: bool = False
    tool_names: tuple[str, ...] = ()


AGENT_SPECS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec("agent_planner", "planner"),
        AgentSpec("agent_router", "router"),
        AgentSpec(
            "agent_profitability",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
        ),
        AgentSpec(
            "agent_liquidity_solvency",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
        ),
        AgentSpec(
            "agent_cashflow_analysis",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
        ),
        AgentSpec(
            "agent_efficiency",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
        ),
        AgentSpec("agent_synth", "synth"),
    )
}

# Additive compatibility view for older callers. New code should consume
# ``AgentSpec`` through ``get_agent_spec`` rather than maintaining another list.
AGENT_METADATA = {name: asdict(spec) for name, spec in AGENT_SPECS.items()}
ANALYSIS_AGENTS = frozenset(
    name for name, spec in AGENT_SPECS.items() if spec.kind == "analysis"
)
ROUTABLE_AGENTS = ANALYSIS_AGENTS


def get_agent_spec(agent_name: str) -> AgentSpec | None:
    return AGENT_SPECS.get(str(agent_name or "").strip())


def get_agent_kind(agent_name: str) -> str:
    spec = get_agent_spec(agent_name)
    return spec.kind if spec else ""


def is_analysis_agent(agent_name: str) -> bool:
    return get_agent_kind(agent_name) == "analysis"
