"""Probe native LangChain bind_tools behavior for the configured chat model.

This script does not execute tool functions. It only checks whether the model
emits valid tool_calls for each agent's LangChain tool schema.
"""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from agents.agent_tools_list import get_tools_for_bind
from tools.tool_calls import invalid_tool_calls, response_tool_calls


DEFAULT_PROBES = {
    "agent_profitability": "danh gia kha nang sinh loi tu doanh thu loi nhuan va tai san",
    "agent_liquidity_solvency": "danh gia thanh khoan don bay va kha nang tra lai",
    "agent_cashflow_analysis": "danh gia chat luong dong tien CFO CFI CFF",
    "agent_efficiency": "danh gia hieu qua su dung tai san hang ton kho va phai thu",
}


def _content_preview(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content[:300]
    try:
        return json.dumps(content, ensure_ascii=False)[:300]
    except TypeError:
        return str(content)[:300]


def run_probe(agent_name: str, query: str) -> dict:
    from llm.client import llm

    tools = get_tools_for_bind(agent_name)
    if not tools:
        return {
            "agent": agent_name,
            "passed": False,
            "error": "agent has no LangChain tools to bind",
        }

    expected_tool_names = {str(tool.name) for tool in tools}
    started_at = time.perf_counter()
    response = llm.bind_tools(tools).invoke(
        [
            (
                "system",
                "You are testing tool calling. Use one of the bound tools when "
                "the user asks for data. Do not answer directly.",
            ),
            (
                "human",
                f"Agent: {agent_name}\n"
                f"Need data for this query: {query}\n"
                "Call the matching tool with a non-empty query argument.",
            ),
        ]
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)

    tool_calls = response_tool_calls(response)
    invalid_calls = invalid_tool_calls(response)
    valid_names = all(call.get("name") in expected_tool_names for call in tool_calls)
    valid_queries = all(str((call.get("args") or {}).get("query", "")).strip() for call in tool_calls)
    no_extra_args = all(set((call.get("args") or {}).keys()) <= {"query"} for call in tool_calls)
    passed = bool(tool_calls) and valid_names and valid_queries and no_extra_args and not invalid_calls

    return {
        "agent": agent_name,
        "query": query,
        "expected_tool_names": sorted(expected_tool_names),
        "passed": passed,
        "checks": {
            "has_tool_calls": bool(tool_calls),
            "valid_tool_names": valid_names,
            "valid_query_args": valid_queries,
            "no_extra_args": no_extra_args,
            "no_invalid_tool_calls": not bool(invalid_calls),
        },
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid_calls,
        "content_preview": _content_preview(response),
        "duration_ms": duration_ms,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=sorted(DEFAULT_PROBES.keys()),
        default="agent_profitability",
        help="Agent tool schema to bind and probe.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Override the default probe query for --agent.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Probe all analysis agents instead of only --agent.",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0 after printing JSON results.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    agent_names = sorted(DEFAULT_PROBES.keys()) if args.all else [args.agent]
    results = []

    for agent_name in agent_names:
        query = args.query if args.query and not args.all else DEFAULT_PROBES[agent_name]
        try:
            results.append(run_probe(agent_name, query))
        except Exception as exc:
            results.append(
                {
                    "agent": agent_name,
                    "query": query,
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    output = {
        "passed": all(item.get("passed") for item in results),
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    if args.no_fail or output["passed"]:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
