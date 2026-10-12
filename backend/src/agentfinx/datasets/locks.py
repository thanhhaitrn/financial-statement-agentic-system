"""Cross-process locks for registry writes and per-dataset build/query work.

The API, the chat worker, the import worker and the CLI all share one
``dataset_store``. Registry writes are read–modify–write on a JSON document;
atomic replace stops a torn file but not a lost update, so writers serialize
on ``registry.lock``. Per-dataset work — build, publish, delete, GC and a whole
chat query — takes ``dataset-<id>.lock`` so a rebuild can never swap the index
underneath a running answer.

Lock order is fixed: a dataset lock may be taken *before* the registry lock,
never while the registry lock is held. Violations raise instead of deadlocking.
"""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from agentfinx.core.schemas.datasets import validate_dataset_id


class LockOrderError(RuntimeError):
    """A dataset lock was requested while the registry lock is held."""


class _ReentrantFileLock:
    """``flock`` across processes, re-entrant for the thread that owns it."""

    def __init__(self, path: Path):
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self._thread_lock.acquire()
        if self._depth == 0:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            except BaseException:
                self._thread_lock.release()
                raise
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                self._thread_lock.release()
                raise
            self._fd = fd
        self._depth += 1

    def release(self) -> None:
        self._depth -= 1
        try:
            if self._depth == 0 and self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self._thread_lock.release()


_LOCKS: dict[str, _ReentrantFileLock] = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


def locks_dir() -> Path:
    # Resolved per call so tests that point FINX_HOME elsewhere get their own locks.
    from agentfinx.core.config import paths

    return paths.locks_dir()


def _held_kinds() -> dict[str, int]:
    kinds = getattr(_HELD, "kinds", None)
    if kinds is None:
        kinds = {}
        _HELD.kinds = kinds
    return kinds


def _lock_for_path(path: Path) -> _ReentrantFileLock:
    resolved = Path(path).resolve()
    key = str(resolved)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _ReentrantFileLock(resolved)
            _LOCKS[key] = lock
        return lock


def _lock_for(name: str) -> _ReentrantFileLock:
    return _lock_for_path(locks_dir() / name)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive cross-process lock on ``path``, re-entrant within a thread.

    For components that keep their own state directory (the acquisition store
    locks one import job at a time next to its database).
    """

    lock = _lock_for_path(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def _hold(kind: str, name: str) -> Iterator[None]:
    lock = _lock_for(name)
    lock.acquire()
    kinds = _held_kinds()
    kinds[kind] = kinds.get(kind, 0) + 1
    try:
        yield
    finally:
        kinds[kind] -= 1
        if not kinds[kind]:
            kinds.pop(kind)
        lock.release()


@contextmanager
def registry_lock() -> Iterator[None]:
    """Serialize registry read–modify–write and migrations across processes."""

    with _hold("registry", "registry.lock"):
        yield


@contextmanager
def dataset_lock(dataset_id: str) -> Iterator[None]:
    """Serialize build/publish/delete/GC and whole queries for one dataset."""

    if _held_kinds().get("registry"):
        raise LockOrderError(
            "dataset lock requested while holding the registry lock; "
            "take the dataset lock first"
        )
    dataset_key = validate_dataset_id(dataset_id)
    with _hold("dataset", f"dataset-{dataset_key}.lock"):
        yield


class ProcessSingleton:
    """Non-blocking "one process per name" guard, e.g. one worker per queue.

    Each instance opens its own file description, so a second instance fails
    even inside the same process — that is what makes it testable.
    """

    def __init__(self, name: str):
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("singleton name must not be empty")
        self.path = locks_dir() / f"singleton-{safe}.lock"
        self._fd: Optional[int] = None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
