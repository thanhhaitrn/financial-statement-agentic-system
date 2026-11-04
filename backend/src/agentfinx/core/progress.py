"""Live steps of one chat run, shown to the user while the answer is prepared.

Inactive unless the chat worker activates a sink for a run, so the CLI, tests
and experiments write nothing. Like :mod:`agentfinx.core.experiment` the sink
is process-wide (not a context variable): the worker answers one question at a
time, and the graph's worker threads and the news branch thread all report
under the same run without passing it along.

A step has a stable ``key`` (one row per key), a short Vietnamese ``label``, an
optional ``detail`` line and, once done, an ``output``: what it produced (the
plan, the facts found, an agent's analysis, news events) for the user to open.
Steps never carry prompts.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import Lock
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

Sink = Callable[[dict], None]

_sink: Optional[Sink] = None
_lock = Lock()


@contextmanager
def activate(sink: Optional[Sink]) -> Iterator[None]:
    """Report the steps of the code inside the block to ``sink``."""

    global _sink
    with _lock:
        previous, _sink = _sink, sink
    try:
        yield
    finally:
        with _lock:
            _sink = previous


def active() -> bool:
    return _sink is not None


def _emit(event: dict) -> None:
    sink = _sink
    if sink is None:
        return
    try:
        sink(event)
    except Exception:  # a progress write must never fail the answer
        logger.warning("progress sink failed for step %s", event.get("key"), exc_info=True)


def begin(key: str, label: str, detail: str = "") -> None:
    _emit({"key": key, "label": label, "status": "running", "detail": detail})


def update(key: str, detail: str) -> None:
    _emit({"key": key, "status": "running", "detail": detail})


def end(key: str, detail: Optional[str] = None, *, status: str = "done", output: Optional[str] = None) -> None:
    """Close a step; ``output`` is what it produced, shown when the user opens it."""

    event = {"key": key, "status": status}
    if detail is not None:
        event["detail"] = detail
    if output:
        event["output"] = output
    _emit(event)


@contextmanager
def step(key: str, label: str, detail: str = "") -> Iterator[None]:
    """A step around a block: running, then done (or failed if it raises)."""

    begin(key, label, detail)
    try:
        yield
    except BaseException:
        end(key, status="failed")
        raise
    end(key)
