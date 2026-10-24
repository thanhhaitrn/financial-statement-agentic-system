"""The context of one experiment run: ids, event log, step labels, call budget.

Inactive unless a run activates it, so product calls never write or count
anything. The security experiment runs each QA run in its own process and
activates one :class:`ExperimentContext` for it; the context is process-wide
(not a context variable), so the graph's worker threads and the news branch
thread all report under the same run without passing it along.

- **Events** (``events_path``, JSONL): provenance decisions and snapshots
  that code in the workflow emits through :func:`emit`.
- **Prompt log** (``prompt_log_path``, JSONL): every model request as sent,
  and its reply, written by ``core.llm.instrument``.
- **Budget** (``budget_path``): one JSON counter shared by all runs of an
  experiment and locked across processes. A model request reserves a call
  right before it is sent, retries included; at the limit the request is
  blocked (never sent) and the budget is marked exhausted.
- **Step**: a label for the code about to call the model
  (:func:`step`), such as ``news.select``; graph nodes are named from
  LangGraph's own metadata instead.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

RUN_CONTEXT_ENV = "AGENTFINX_RUN_CONTEXT"
PROMPT_LOG_ENV = "AGENTFINX_PROMPT_LOG"


class BudgetExhausted(RuntimeError):
    """The experiment's model-call budget is spent; the request was not sent."""


@dataclass(frozen=True)
class ExperimentContext:
    run_id: str
    case_id: str = ""
    config: str = ""
    condition: str = ""
    attempt: int = 1
    events_path: str = ""
    prompt_log_path: str = ""
    budget_path: str = ""

    def ids(self) -> dict:
        return {"run_id": self.run_id, "case_id": self.case_id, "config": self.config,
                "condition": self.condition, "attempt": self.attempt}

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ExperimentContext":
        return cls(**json.loads(text))


_active: ExperimentContext | None = None
_write_lock = Lock()
_step: ContextVar[str] = ContextVar("agentfinx_experiment_step", default="")
_call_kind: ContextVar[str] = ContextVar("agentfinx_experiment_call_kind", default="")


def active() -> ExperimentContext | None:
    return _active


@contextmanager
def activate(context: ExperimentContext | None) -> Iterator[ExperimentContext | None]:
    """Make ``context`` the run of this process for the block (no-op for None)."""

    global _active
    if context is None:
        yield None
        return
    if _active is not None and _active.run_id != context.run_id:
        raise RuntimeError("another experiment run is already active in this process")
    previous = _active
    _active = context
    try:
        yield context
    finally:
        _active = previous


def context_from_env() -> ExperimentContext | None:
    text = os.getenv(RUN_CONTEXT_ENV, "").strip()
    return ExperimentContext.from_json(text) if text else None


@contextmanager
def step(name: str) -> Iterator[None]:
    token = _step.set(name)
    try:
        yield
    finally:
        _step.reset(token)


def current_step() -> str:
    return _step.get()


@contextmanager
def call_kind(name: str) -> Iterator[None]:
    """Label the model calls of the block (``json_repair``, ``answer_repair``)."""

    token = _call_kind.set(name)
    try:
        yield
    finally:
        _call_kind.reset(token)


def current_call_kind() -> str:
    return _call_kind.get()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_jsonl(path: str | os.PathLike, record: dict) -> None:
    """Append one line, locked across threads and processes."""

    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock, open(target, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def emit(kind: str, **fields: Any) -> None:
    """Record one event for the active run; nothing when no run is active."""

    context = _active
    if context is None or not context.events_path:
        return
    append_jsonl(context.events_path, {"ts": _now(), **context.ids(), "kind": kind,
                                       "step": current_step(), **fields})


# --- call budget -----------------------------------------------------------

def create_budget(path: str | os.PathLike, limit: int) -> None:
    """A new budget file; an existing one is kept (resume)."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(json.dumps({"limit": int(limit), "used": 0, "blocked": 0,
                                      "exhausted": False, "by_run": {}}, indent=1))


@contextmanager
def _locked_budget(path: str | os.PathLike):
    with open(path, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            state = json.loads(handle.read() or "{}")
            try:
                yield state
            finally:
                # Written even when the block raised: a blocked request is counted.
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(state, indent=1, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_budget(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            return json.loads(handle.read() or "{}")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def reserve_call(*, run_id: str, budget_path: str) -> int:
    """Count one model request before it is sent; its number in the budget.
    Raises :class:`BudgetExhausted` (and counts the block) at the limit."""

    with _write_lock, _locked_budget(budget_path) as state:
        limit = int(state.get("limit", 0))
        if int(state.get("used", 0)) >= limit:
            state["blocked"] = int(state.get("blocked", 0)) + 1
            state["exhausted"] = True
            blocked_runs = state.setdefault("blocked_runs", [])
            if run_id not in blocked_runs:
                blocked_runs.append(run_id)
            raise BudgetExhausted(f"model-call budget of {limit} spent")
        state["used"] = int(state.get("used", 0)) + 1
        by_run = state.setdefault("by_run", {})
        by_run[run_id] = int(by_run.get(run_id, 0)) + 1
        return state["used"]
