"""The model call used by the news analysis: one prompt in, raw JSON text out.

``invoke(system, user, timeout)`` must give up within ``timeout`` seconds; the
default builds an Ollama client whose HTTP timeout is exactly that, so the
request itself stops when the analysis budget runs out.
"""

from __future__ import annotations

from typing import Callable

ModelInvoker = Callable[[str, str, float], str]


def ollama_json_invoker() -> ModelInvoker:
    def invoke(system: str, user: str, timeout: float) -> str:
        from llm.client import build_llm

        message = build_llm(timeout=timeout, json_mode=True).invoke(
            [("system", system), ("human", user)]
        )
        return str(getattr(message, "content", "") or "")

    return invoke
