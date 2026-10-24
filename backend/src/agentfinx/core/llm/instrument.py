"""Prompt log and call budget for experiment runs, at the model boundary.

Every chat model built by ``core.llm.client`` carries :data:`CALLBACK`. With
no experiment run active and no ``AGENTFINX_PROMPT_LOG`` set it does nothing.
Otherwise, right before each request it reserves one call in the run's
budget (raising ``BudgetExhausted`` so the request is never sent) and writes
the messages exactly as the model receives them; after it, the reply, token
usage and latency, or the error. Direct calls, structured output, tool
binding, JSON repair and answer repair all pass through here, retries
included, because each is a separate request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from threading import Lock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from agentfinx.core import experiment

# Only these request parameters are logged; credentials and headers never are.
_PARAMS = ("model", "temperature", "format", "num_ctx", "seed", "stop", "tool_choice", "_type")
# Response metadata naming the model or backend that served a call.
_SERVED_BY_KEYS = ("model", "model_name", "model_version", "system_fingerprint")


def _message(message: Any) -> dict:
    record = {"role": getattr(message, "type", ""), "content": getattr(message, "content", "")}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        record["tool_calls"] = tool_calls
    return record


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tools(params: dict) -> list[dict]:
    output = []
    for tool in params.get("tools") or []:
        function = tool.get("function", tool) if isinstance(tool, dict) else {}
        output.append({"name": function.get("name", ""), "schema_sha256": _digest(tool)})
    return output


_SECRET = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:\s\"']+)[A-Za-z0-9._\-]+")


def _mask(text: str) -> str:
    return _SECRET.sub(lambda match: match.group(1) + "***", text)


def _call_kind(structured: bool, params: dict) -> str:
    kind = experiment.current_call_kind()
    if kind:
        return kind
    if structured:
        return "structured"
    if params.get("tools"):
        return "tool_binding"
    return "direct"


class ExperimentCallback(BaseCallbackHandler):
    raise_error = True  # a blocked budget or an unwritable log stops the call
    run_inline = True

    def __init__(self) -> None:
        self._started: dict[str, float] = {}
        self._lock = Lock()

    @staticmethod
    def _target() -> tuple[experiment.ExperimentContext | None, str]:
        context = experiment.active()
        path = (context.prompt_log_path if context else "") or os.getenv(experiment.PROMPT_LOG_ENV, "")
        return context, path

    def _write(self, path: str, context, record: dict) -> None:
        if path:
            experiment.append_jsonl(path, {"ts": experiment._now(),
                                           **(context.ids() if context else {}), **record})

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None,
                            metadata=None, **kwargs) -> None:
        context, path = self._target()
        if context is None and not path:
            return
        metadata = dict(metadata or {})
        params = dict(kwargs.get("invocation_params") or {})
        call_id = str(run_id)
        # with_structured_output passes its schema in ``options``.
        structured = bool((kwargs.get("options") or {}).get("ls_structured_output_format")
                          or metadata.get("ls_structured_output_format"))
        step = experiment.current_step() or str(metadata.get("langgraph_node", "") or "unknown")
        record = {
            "event": "request",
            "call_id": call_id,
            "step": step,
            "call_kind": _call_kind(structured, params),
            "langgraph_node": metadata.get("langgraph_node", ""),
            "params": {key: params[key] for key in _PARAMS if key in params},
            "tools": _tools(params),
            "structured_output": structured,
            "messages": [_message(message) for batch in messages for message in batch],
        }
        if context is not None and context.budget_path:
            try:
                record["budget_call_number"] = experiment.reserve_call(
                    run_id=context.run_id, budget_path=context.budget_path)
            except experiment.BudgetExhausted:
                self._write(path, context, {**record, "event": "blocked_budget_exhausted"})
                experiment.emit("budget_exhausted", call_id=call_id, call_step=step)
                raise
        record["messages_sha256"] = _digest(record["messages"])
        self._write(path, context, record)
        with self._lock:
            self._started[call_id] = time.perf_counter()

    def _elapsed(self, run_id) -> int | None:
        with self._lock:
            started = self._started.pop(str(run_id), None)
        return None if started is None else int((time.perf_counter() - started) * 1000)

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
        context, path = self._target()
        if context is None and not path:
            return
        texts, tool_calls, usage, served_by = [], [], {}, {}
        for batch in getattr(response, "generations", []) or []:
            for generation in batch:
                message = getattr(generation, "message", None)
                texts.append(str(getattr(message, "content", "") if message is not None
                                 else getattr(generation, "text", "")))
                tool_calls.extend(getattr(message, "tool_calls", None) or [])
                usage = dict(getattr(message, "usage_metadata", None) or usage)
                metadata = getattr(message, "response_metadata", None) or {}
                # What the provider says served the call; nothing is inferred when it says nothing.
                served_by.update({key: str(metadata[key]) for key in _SERVED_BY_KEYS if metadata.get(key)})
        self._write(path, context, {"event": "response", "call_id": str(run_id),
                                    "latency_ms": self._elapsed(run_id), "output": "\n".join(texts),
                                    "tool_calls": tool_calls, "usage": usage, "served_by": served_by})

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        context, path = self._target()
        if context is None and not path:
            return
        self._write(path, context, {"event": "error", "call_id": str(run_id),
                                    "latency_ms": self._elapsed(run_id),
                                    "error_type": type(error).__name__,
                                    "error": _mask(str(error))[:500]})


CALLBACK = ExperimentCallback()
