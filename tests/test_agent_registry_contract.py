"""AgentSpec remains the only capability registry."""

from agents.agent_registry import AGENT_METADATA, AGENT_SPECS, AgentSpec
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
