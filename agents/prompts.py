from __future__ import annotations

from typing import Any

from langchain_core.prompts import ChatPromptTemplate


_PROMPT_SECTIONS = [
    ("system", "You are {role}", "role", True),
    ("system", "{system_instruction}", "system_instruction", True),
    ("system", "You can access these actions:\n{tools_list}", "tools_list", False),
    ("human", "User query: {user_query}", "user_query", True),
    ("system", "Worker query:\n{worker_query}", "worker_query", False),
    ("system", "Planner plan (JSON):\n{plan_json}", "plan_json", False),
    ("system", "Worker results (JSON):\n{worker_results_json}", "worker_results_json", False),
    ("system", "Allowed keywords by table (JSON):\n{allowed_keywords_json}", "allowed_keywords_json", False),
    ("system", "Web summary:\n{web_summary}", "web_summary", False),
    ("system", "Previous agent response:\n{last_agent_response}", "last_agent_response", False),
    ("system", "Past tool observations:\n{tool_observations}", "tool_observations", False),
]

_EMPTY_TEXT_MARKERS = {"", "{}", "[]", "null", '""'}


def _has_prompt_value(value: Any) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return str(value).strip().lower() not in _EMPTY_TEXT_MARKERS

    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)

    return True


def build_prompt_template(payload: dict) -> ChatPromptTemplate:
    messages = []

    for role, template, key, required in _PROMPT_SECTIONS:
        if required or _has_prompt_value(payload.get(key)):
            messages.append((role, template))

    return ChatPromptTemplate.from_messages(messages)


PROMPT_TEMPLATE = build_prompt_template
