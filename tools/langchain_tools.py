"""LangChain tool declarations for agent-visible tool schemas.

The production workflow still executes tools through ``tools.tool_runner`` so
it can inject internal arguments such as table, collection, and strict_table.
These declarations are the schema source of truth for prompts and bind_tools
compatibility checks.
"""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Type

from langchain.tools import tool as _tool
from pydantic import BaseModel, Field


class ScopedInfoInput(BaseModel):
    """Arguments visible to analysis agents for scoped financial-statement retrieval."""

    query: str = Field(
        ...,
        description=(
            "One short Vietnamese financial-statement line item, note topic, or front-report-section topic "
            "to retrieve from this tool's scoped section."
        ),
    )


@dataclass(frozen=True)
class AgentToolDeclaration:
    name: str
    description: str
    args_schema: Type[BaseModel]
    langchain_tool: Any


def _schema_only_runtime_error(name: str) -> RuntimeError:
    return RuntimeError(
        f"{name} is a schema-only LangChain tool declaration. "
        "Execute tools through tools.tool_runner.call_tool_for_agent so internal "
        "state, table, collection, and guardrails are applied."
    )


def _make_scoped_info_tool(name: str, description: str):
    @_tool(
        name,
        args_schema=ScopedInfoInput,
        description=description,
    )
    def scoped_info(query: str) -> dict:
        """Retrieve relevant context from one scoped financial-statement section."""
        raise _schema_only_runtime_error(name)

    return scoped_info


_SCOPED_ANALYSIS_TOOL_DESCRIPTIONS = {
    "get_balance_sheet_info": "Retrieve balance sheet evidence only.",
    "get_income_statement_info": "Retrieve income statement evidence only.",
    "get_cashflow_info": "Retrieve cash flow statement evidence only.",
    "get_note_info": "Retrieve notes-to-financial-statements evidence only.",
    "get_report_section_info": "Retrieve report-level narrative such as company profile, address/head office, accounting standards/regime applied, audit firm, management board, management report, audit/review report, signers, dates, and emphasis matters only.",
}
_ANALYSIS_AGENT_NAMES = (
    "agent_profitability",
    "agent_liquidity_solvency",
    "agent_cashflow_analysis",
    "agent_efficiency",
)


AGENT_TOOL_DECLARATIONS: dict[str, list[AgentToolDeclaration]] = {}
for _agent_name in _ANALYSIS_AGENT_NAMES:
    AGENT_TOOL_DECLARATIONS[_agent_name] = [
        AgentToolDeclaration(
            name=tool_name,
            description=description,
            args_schema=ScopedInfoInput,
            langchain_tool=_make_scoped_info_tool(tool_name, description),
        )
        for tool_name, description in _SCOPED_ANALYSIS_TOOL_DESCRIPTIONS.items()
    ]


def _args_description(args_schema: Type[BaseModel]) -> str:
    schema = args_schema.model_json_schema()
    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    parts = []

    for name, meta in properties.items():
        type_name = str(meta.get("type", "any") or "any")
        required_label = ", required" if name in required else ""
        description = str(meta.get("description", "") or "").strip()
        if description:
            parts.append(f"{name} ({type_name}{required_label}): {description}")
        else:
            parts.append(f"{name} ({type_name}{required_label})")

    return "; ".join(parts)


def get_tool_prompt_specs_for_agent(agent_name: str) -> list[dict[str, str]]:
    """Return prompt-compatible tool metadata for the existing custom runner."""
    specs = []
    for declaration in AGENT_TOOL_DECLARATIONS.get(str(agent_name or "").strip(), []):
        specs.append(
            {
                "name": declaration.name,
                "description": declaration.description,
                "args": _args_description(declaration.args_schema),
            }
        )
    return specs


def get_langchain_tools_for_agent(agent_name: str) -> list[Any]:
    """Return LangChain BaseTool objects for bind_tools probing or migration."""
    return [
        declaration.langchain_tool
        for declaration in AGENT_TOOL_DECLARATIONS.get(str(agent_name or "").strip(), [])
    ]


def get_tool_names_for_agent(agent_name: str) -> set[str]:
    return {
        declaration.name
        for declaration in AGENT_TOOL_DECLARATIONS.get(str(agent_name or "").strip(), [])
    }
