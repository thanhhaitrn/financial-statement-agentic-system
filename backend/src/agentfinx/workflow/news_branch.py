"""The company-news branch run beside the report branch.

LangGraph advances in synchronous steps: a slow node in a step holds back
every other node of that step, including the report agents' tool rounds. So
the news analysis does not run as a graph node beside them. ``start_web``
hands it to this runner (a thread outside the graph steps) and the report
branch goes on; ``join_web`` waits for it before Synth, never past the web
branch's time budget. One runner belongs to one run.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextvars import copy_context
from threading import Lock
from typing import Callable, Optional


class WebBranchRunner:
    def __init__(self, *, max_workers: int = 1, clock: Callable[[], float] = time.monotonic):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="web-branch")
        self._jobs: dict[str, tuple[Future, float]] = {}
        self._lock = Lock()
        self._clock = clock

    def start(self, key: str, job: Callable[[], dict], *, budget_seconds: float) -> bool:
        """Start ``job`` once per ``key``; it must finish within the budget."""

        with self._lock:
            if key in self._jobs:
                return False
            context = copy_context()  # runtime policy and collection of this run
            future = self._executor.submit(context.run, job)
            self._jobs[key] = (future, self._clock() + max(budget_seconds, 0.0))
        return True

    def join(self, key: str, *, grace_seconds: float = 2.0) -> tuple[Optional[dict], bool]:
        """(result, finished): waits at most until the job's deadline."""

        with self._lock:
            entry = self._jobs.get(key)
        if entry is None:
            return None, False
        future, deadline = entry
        try:
            return future.result(timeout=max(deadline + grace_seconds - self._clock(), 0.0)), True
        except TimeoutError:
            return None, False
        except Exception:
            return {"error": "job_failed"}, True

    def close(self) -> None:
        # An unfinished analysis ends on its own HTTP and model timeouts.
        self._executor.shutdown(wait=False, cancel_futures=True)
