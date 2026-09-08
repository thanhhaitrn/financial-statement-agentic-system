"""Central registry for agent capabilities and their default data scopes."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
)


AgentKind = Literal["planner", "router", "analysis", "synth"]

_ANALYSIS_TOOL_NAMES = (
    "get_balance_sheet_info",
    "get_income_statement_info",
    "get_cashflow_info",
    "get_note_info",
)


@dataclass(frozen=True)
class AgentSpec:
    """Immutable source of truth for one runnable agent's capabilities.

    Every downstream view — public aspect label, section order, table
    allowlists, default tool, formatter header — is derived from this record so
    that adding an analysis agent is a single-place change.
    """

    name: str
    kind: AgentKind
    supports_tools: bool = False
    tool_names: tuple[str, ...] = ()
    # Public, user-facing name of the business aspect this agent owns. Empty for
    # non-analysis agents, which never own a section in the final answer.
    aspect_label: str = ""
    # Position of that section in the final answer; 0 for non-analysis agents.
    display_order: int = 0
    # Tables the dispatcher and keyworder may target for this agent.
    allowed_tables: tuple[str, ...] = ()
    # Tables whose keyword expansion this agent may drive (report sections are
    # narrative and never keyword-expanded from an analysis agent).
    keyword_tables: tuple[str, ...] = ()
    # Tool used when nothing more specific is derivable from the query.
    default_tool: str = ""

    @property
    def display_header(self) -> str:
        """Formatter heading, derived so it can never drift from ``name``."""

        if not self.name.startswith("agent_"):
            return self.name
        return "Agent " + self.name[len("agent_") :].replace("_", " ").title()


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
            aspect_label="Khả năng sinh lời",
            display_order=1,
            allowed_tables=(TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION),
            keyword_tables=(TABLE_BS, TABLE_IS, TABLE_NOTE),
            default_tool="get_income_statement_info",
        ),
        AgentSpec(
            "agent_liquidity_solvency",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
            aspect_label="Thanh khoản và an toàn tài chính",
            display_order=2,
            allowed_tables=(
                TABLE_BS,
                TABLE_IS,
                TABLE_CF,
                TABLE_NOTE,
                TABLE_REPORT_SECTION,
            ),
            keyword_tables=(TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE),
            default_tool="get_balance_sheet_info",
        ),
        AgentSpec(
            "agent_cashflow_analysis",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
            aspect_label="Dòng tiền",
            display_order=3,
            allowed_tables=(
                TABLE_BS,
                TABLE_IS,
                TABLE_CF,
                TABLE_NOTE,
                TABLE_REPORT_SECTION,
            ),
            keyword_tables=(TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE),
            default_tool="get_cashflow_info",
        ),
        AgentSpec(
            "agent_efficiency",
            "analysis",
            supports_tools=True,
            tool_names=_ANALYSIS_TOOL_NAMES,
            aspect_label="Hiệu quả hoạt động",
            display_order=4,
            allowed_tables=(TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION),
            keyword_tables=(TABLE_BS, TABLE_IS, TABLE_NOTE),
            default_tool="get_income_statement_info",
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


# ---------------------------------------------------------------------------
# Registry-derived views. Consume these instead of re-declaring agent tables.
# ---------------------------------------------------------------------------

ANALYSIS_AGENT_ORDER: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(
        (spec for spec in AGENT_SPECS.values() if spec.kind == "analysis"),
        key=lambda item: (item.display_order, item.name),
    )
)
ANALYSIS_ASPECT_LABELS: dict[str, str] = {
    name: AGENT_SPECS[name].aspect_label for name in ANALYSIS_AGENT_ORDER
}
ANALYSIS_AGENT_HEADERS: dict[str, str] = {
    name: AGENT_SPECS[name].display_header for name in ANALYSIS_AGENT_ORDER
}
ANALYSIS_TABLE_ALLOWLIST: dict[str, set[str]] = {
    name: set(AGENT_SPECS[name].allowed_tables) for name in ANALYSIS_AGENT_ORDER
}
ANALYSIS_ALLOWED_KEYWORD_TABLES: dict[str, set[str]] = {
    name: set(AGENT_SPECS[name].keyword_tables) for name in ANALYSIS_AGENT_ORDER
}
ANALYSIS_DEFAULT_TOOL: dict[str, str] = {
    name: AGENT_SPECS[name].default_tool for name in ANALYSIS_AGENT_ORDER
}


def analysis_aspect_headings() -> tuple[str, ...]:
    """Numbered public headings in registry order (``**1. Khả năng sinh lời**``)."""

    return tuple(
        f"**{position}. {ANALYSIS_ASPECT_LABELS[name]}**"
        for position, name in enumerate(ANALYSIS_AGENT_ORDER, start=1)
    )
