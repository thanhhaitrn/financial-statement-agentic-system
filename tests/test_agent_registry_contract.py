"""AgentSpec remains the only capability registry."""

import agents.agent_runner as agent_runner
import agents.keyworder_runner as keyworder_runner
import graph.dispatch_nodes as dispatch_nodes
import tools.evidence as evidence
from agents.agent_registry import (
    AGENT_METADATA,
    AGENT_SPECS,
    ANALYSIS_AGENT_ORDER,
    AgentSpec,
    analysis_aspect_headings,
)
from schemas.agent_outputs import AnalysisAgentName
from tools.langchain_tools import get_tool_names_for_agent


def test_agent_specs_drive_tool_declarations_and_compatibility_view():
    assert AGENT_SPECS
    for name, spec in AGENT_SPECS.items():
        assert isinstance(spec, AgentSpec)
        assert AGENT_METADATA[name]["kind"] == spec.kind
        assert AGENT_METADATA[name]["supports_tools"] == spec.supports_tools
        assert get_tool_names_for_agent(name) == set(spec.tool_names)


def test_report_section_tool_is_not_bound_to_analysis_agents():
    for spec in AGENT_SPECS.values():
        if spec.kind == "analysis":
            assert "get_report_section_info" not in spec.tool_names


def test_every_analysis_spec_declares_the_fields_downstream_views_read():
    for name in ANALYSIS_AGENT_ORDER:
        spec = AGENT_SPECS[name]
        assert spec.aspect_label
        assert spec.display_order > 0
        assert spec.allowed_tables
        assert spec.keyword_tables
        assert spec.default_tool
        # Keyword expansion is a strict subset of what may be dispatched.
        assert set(spec.keyword_tables) <= set(spec.allowed_tables)


def test_registry_is_the_only_place_analysis_agents_are_enumerated():
    assert tuple(member.value for member in AnalysisAgentName) == ANALYSIS_AGENT_ORDER
    assert set(dispatch_nodes.ANALYSIS_TABLE_ALLOWLIST) == set(ANALYSIS_AGENT_ORDER)
    assert keyworder_runner.ANALYSIS_TABLE_ALLOWLIST is dispatch_nodes.ANALYSIS_TABLE_ALLOWLIST
    assert set(agent_runner.ANALYSIS_ALLOWED_KEYWORD_TABLES) == set(ANALYSIS_AGENT_ORDER)
    assert set(evidence.ANALYSIS_DEFAULT_TOOL) == set(ANALYSIS_AGENT_ORDER)


def test_aspect_headings_are_numbered_in_registry_display_order():
    assert analysis_aspect_headings() == (
        "**1. Khả năng sinh lời**",
        "**2. Thanh khoản và an toàn tài chính**",
        "**3. Dòng tiền**",
        "**4. Hiệu quả hoạt động**",
    )


def test_display_header_is_derived_from_the_agent_name():
    assert AGENT_SPECS["agent_liquidity_solvency"].display_header == (
        "Agent Liquidity Solvency"
    )
