"""Queue workers: one sequential worker per queue, guarded by a process lock.

``chat`` processes queued chat runs one at a time. On startup it marks runs a
previous worker left ``running`` as ``interrupted`` (never re-running them, so
no model call is repeated or billed twice); queued runs simply continue.

``import`` runs queued PDF import jobs one at a time. On startup it requeues
jobs a previous worker left mid-flight (their next run polls any remote parse
already submitted instead of submitting again) and fails, with billing left
pending, a job whose submission was cut off before its remote ID was stored.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from typing import Callable, Optional

from dataset_catalog.locks import ProcessSingleton
from schemas.chat_context import CONTEXT_CONTRACT_VERSION
from webapp import db as webdb
from webapp.chat_runner import SAFE_MESSAGES, ChatDependencies, process_chat_run
from webapp.news_updates import run_news_update_once
from webapp.settings import WebSettings

logger = logging.getLogger("agentfinx.web.worker")

QUEUES = ("chat", "import")


class WorkerAlreadyRunning(RuntimeError):
    """Another process already owns this queue."""


def recover_chat_queue(database: webdb.WebDB) -> int:
    with database.transaction() as conn:
        return webdb.interrupt_running_runs(
            conn,
            error_code="worker_restarted",
            error_message=SAFE_MESSAGES["worker_restarted"],
        )


def run_chat_once(database: webdb.WebDB, worker_id: str, deps: ChatDependencies) -> bool:
    with database.transaction() as conn:
        run_id = webdb.claim_next_run(conn, worker_id)
    if run_id is None:
        return False
    status = process_chat_run(database, run_id, deps)
    logger.info("chat run %s finished: %s", run_id, status)
    return True


def _web_analysis_active() -> bool:
    from config.runtime_policy import DEFAULT_POLICY

    return bool(DEFAULT_POLICY.web.analysis_active)


class _Heartbeat(threading.Thread):
    def __init__(self, database, queue, worker_id, interval, stop, runtime_config=None):
        super().__init__(name=f"heartbeat-{queue}", daemon=True)
        self.database = database
        self.queue = queue
        self.worker_id = worker_id
        self.interval = interval
        self.stop = stop
        self.started_at = webdb.utc_now()
        self.runtime_config = runtime_config or {}

    def beat(self) -> None:
        with self.database.transaction() as conn:
            webdb.upsert_heartbeat(
                conn,
                queue=self.queue,
                worker_id=self.worker_id,
                pid=os.getpid(),
                started_at=self.started_at,
                runtime_config=self.runtime_config,
            )

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                self.beat()
            except Exception:
                logger.exception("heartbeat for %s failed", self.queue)


def run_worker(
    queue: str,
    settings: WebSettings,
    *,
    stop_event: Optional[threading.Event] = None,
    max_iterations: Optional[int] = None,
    deps: Optional[ChatDependencies] = None,
    import_handler: Optional[Callable[[webdb.WebDB, str], bool]] = None,
) -> int:
    if queue not in QUEUES:
        raise ValueError(f"unknown queue: {queue!r}")

    singleton = ProcessSingleton(f"worker-{queue}")
    if not singleton.try_acquire():
        raise WorkerAlreadyRunning(f"another '{queue}' worker is already running")

    stop = stop_event or threading.Event()
    heartbeat: Optional[_Heartbeat] = None
    try:
        database = webdb.WebDB(settings.db_path)
        database.migrate()
        worker_id = f"{queue}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

        runtime_config = {}
        if queue == "chat":
            runtime_config = {
                "chat_context": deps.context_enabled if deps is not None else settings.chat_context,
                "report_discovery": deps.discovery_enabled if deps is not None else settings.report_discovery,
                "context_contract_version": CONTEXT_CONTRACT_VERSION,
                # Read from the environment the pipeline itself uses.
                "web_analysis": _web_analysis_active(),
            }
        heartbeat = _Heartbeat(database, queue, worker_id, settings.heartbeat_interval_seconds,
                               stop, runtime_config)
        heartbeat.beat()
        heartbeat.start()

        if queue == "chat":
            recovered = recover_chat_queue(database)
            if recovered:
                logger.warning("marked %d interrupted chat run(s) from a previous worker", recovered)
            with database.transaction() as conn:
                webdb.recover_news_updates(conn)
            deps = deps or ChatDependencies(
                shared_dataset_ids=settings.shared_dataset_ids,
                context_enabled=settings.chat_context,
                discovery_enabled=settings.report_discovery,
            )
        elif import_handler is None:
            from webapp.imports import build_import_handler, default_catalog_path

            import_handler = build_import_handler(
                database,
                catalog_path=default_catalog_path() if settings.report_discovery else None,
            )
        logger.info("worker %s started on queue '%s'; features=%s", worker_id, queue, runtime_config)

        iterations = 0
        while not stop.is_set():
            if queue == "chat":
                # Questions first; news updates only when no question waits.
                worked = run_chat_once(database, worker_id, deps) or run_news_update_once(
                    database, worker_id, provider_factory=deps.news_provider, invoke=deps.news_model)
            elif import_handler is not None:
                worked = import_handler(database, worker_id)
            else:
                worked = False
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if not worked:
                stop.wait(settings.worker_poll_seconds)
        return 0
    finally:
        stop.set()
        if heartbeat is not None and heartbeat.is_alive():
            heartbeat.join(timeout=2)
        singleton.release()
