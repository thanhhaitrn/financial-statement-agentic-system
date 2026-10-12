"""Worker-side execution of one queued chat run.

Order of operations, all inside the dataset lock:

1. resolve the dataset again through the owner-scoped access layer;
2. activate it read-only and compare the generation with the one the run was
   accepted against — a mismatch makes the session read-only instead of
   silently answering from a rebuilt report;
3. run the existing pipeline and render it with ``format_final_answer``.

Failures store a fixed, safe message. Pipeline trace errors can contain raw
provider text (rate-limit messages name the provider account), so they are
logged server-side and never shown to the browser. There is no demo fallback.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agentfinx.datasets.access import DatasetAccessError, get_accessible_dataset
from agentfinx.datasets.activation import DatasetNotReady, activate_dataset_for_query
from agentfinx.datasets.locks import dataset_lock
from agentfinx.api import db as webdb
from agentfinx.api.clarify import Clarification, clarify_question
from agentfinx.api.context_history import load_context_history
from agentfinx.api.context_snapshot import build_context_snapshot

logger = logging.getLogger("agentfinx.web.chat")

SAFE_MESSAGES = {
    "session_deleted": "Cuộc trò chuyện đã bị xoá.",
    "dataset_unavailable": "Báo cáo không còn khả dụng với tài khoản này.",
    "dataset_changed": (
        "Báo cáo đã được cập nhật. Hãy tạo cuộc trò chuyện mới để hỏi trên phiên bản mới."
    ),
    "dataset_not_ready": "Báo cáo chưa sẵn sàng để hỏi đáp.",
    "pipeline_error": (
        "Hệ thống chưa tạo được câu trả lời cho câu hỏi này. "
        "Vui lòng thử lại hoặc diễn đạt khác."
    ),
    "internal_error": "Đã có lỗi nội bộ khi xử lý câu hỏi. Vui lòng thử lại sau.",
    "worker_restarted": (
        "Tiến trình xử lý đã khởi động lại giữa chừng nên câu hỏi chưa được trả lời. "
        "Hãy gửi lại nếu cần."
    ),
}
# Activation failures that mean the pinned generation can never answer again.
READ_ONLY_CODES = frozenset(
    {"dataset_changed", "dataset_unavailable", "artifact_missing", "kb_invalid", "index_missing"}
)


class _AlreadyAdmitted:
    """The API admits question quota once, when the run is created."""

    def admit_question(self, owner_id: str) -> None:
        return None


@dataclass
class ChatDependencies:
    shared_dataset_ids: frozenset[str]
    resolve: Callable[..., Any] = get_accessible_dataset
    activate: Callable[..., Any] = activate_dataset_for_query
    execute: Optional[Callable[..., dict]] = None
    collect_errors: Optional[Callable[[dict], list]] = None
    format_answer: Optional[Callable[[dict], str]] = None
    # FINX_CHAT_CONTEXT: resolve from completed effective questions only.
    context_enabled: bool = False
    clarify: Optional[Callable] = None
    # FINX_REPORT_DISCOVERY_ENABLED: the planner (web profile) decides first
    # whether the question asks to find a report; only QA needs a dataset.
    discovery_enabled: bool = False
    plan_task: Optional[Callable[[dict], dict]] = None
    discover: Optional[Callable[..., dict]] = None
    # Finishing news that was not ready when a run answered (api.news_updates):
    # the news provider and the model call; None uses the configured ones.
    news_provider: Optional[Callable[[], Any]] = None
    news_model: Optional[Callable[[str, str, float], str]] = None

    def planner(self) -> Callable[[dict], dict]:
        if self.plan_task is not None:
            return self.plan_task

        def plan(state: dict) -> dict:
            from agentfinx.agents.planner_runner import run_web_planner
            from agentfinx.workflow.state import WorkflowServices
            from agentfinx.workflow.graph import run_with_services

            return run_with_services(run_web_planner, WorkflowServices(collection=None), state)

        return plan

    def discovery(self) -> Callable[..., dict]:
        if self.discover is None:
            from agentfinx.api.discovery import run_discovery
            from agentfinx.api.imports import build_web_import_service

            service = build_web_import_service()
            self.discover = lambda search, **kwargs: run_discovery(service, search, **kwargs)
        return self.discover

    def pipeline(self) -> tuple[Callable[..., dict], Callable[[dict], list], Callable[[dict], str]]:
        execute, collect, render = self.execute, self.collect_errors, self.format_answer
        if execute is None or collect is None:
            import agentfinx.workflow.pipeline as pipeline_cli

            execute = execute or pipeline_cli.execute_query
            collect = collect or pipeline_cli.collect_pipeline_errors
        if render is None:
            from agentfinx.workflow.output_formatter import format_final_answer

            render = format_final_answer
        return execute, collect, render


def _fail(
    database: webdb.WebDB,
    run: Any,
    code: str,
    *,
    detail_code: str = "",
    read_only: bool = False,
) -> str:
    with database.transaction() as conn:
        if read_only and webdb.get_session(conn, run["session_id"]) is not None:
            webdb.mark_session_read_only(conn, run["session_id"], detail_code or code)
        webdb.finish_run(
            conn,
            run["id"],
            status="failed",
            error_code=detail_code or code,
            error_message=SAFE_MESSAGES.get(code, SAFE_MESSAGES["internal_error"]),
        )
    return "failed"


def _succeed(database: webdb.WebDB, run: Any, answer: str, *, task_kind: str = "",
             snapshot: dict | None = None, news_context: dict | None = None) -> str:
    with database.transaction() as conn:
        if webdb.get_session(conn, run["session_id"]) is None:
            return "session_deleted"  # the run row was cascaded away with it
        current = webdb.get_run(conn, run["id"])
        if snapshot:
            webdb.set_context_snapshot(conn, run["id"], snapshot)
        context = {
            "context_status": current["context_status"],
            "context_reason": current["context_reason"],
            "interpreted_question": (
                current["effective_question"] if current["context_status"] == "resolved" else ""
            ),
        }
        message_id = webdb.add_message(
            conn,
            session_id=run["session_id"],
            role="assistant",
            content=answer,
            run_id=run["id"],
            payload=json.dumps(context, ensure_ascii=False),
        )
        if not webdb.finish_run(conn, run["id"], status="succeeded", answer_message_id=message_id,
                                result_kind="answer", task_kind=task_kind):
            raise RuntimeError("run is no longer running; discarding its answer")
        if news_context:
            # The news was not ready: finish it later and add it on its own.
            from agentfinx.api.news_updates import schedule

            schedule(conn, run=run, context=news_context)
        webdb.touch_session(conn, run["session_id"])
    return "succeeded"


def _succeed_structured(database: webdb.WebDB, run: Any, payload: dict, *, task_kind: str) -> str:
    """Store a card (candidates, clarification, error, dataset required) as
    the run's answer. It is never a financial answer."""

    from agentfinx.api.discovery import store_result

    payload = {**payload, "origin": "chat", "run_id": run["id"]}
    with database.transaction() as conn:
        if webdb.get_session(conn, run["session_id"]) is None:
            return "session_deleted"
        message_id = store_result(conn, session_id=run["session_id"], payload=payload, run_id=run["id"])
        if not webdb.finish_run(conn, run["id"], status="succeeded", answer_message_id=message_id,
                                result_kind=payload["kind"], task_kind=task_kind):
            raise RuntimeError("run is no longer running; discarding its answer")
        webdb.touch_session(conn, run["session_id"])
    return "succeeded"


def _planner_state(run: Any, question: str) -> dict:
    return {
        "user_query": question,
        "dataset_id": run["dataset_id"] or "",
        "debug_trace": False,
        "tool_observations": [],
        "run_id": str(run["id"])[:8],
        "trace": [],
    }


def _answer_discovery(database: webdb.WebDB, run: Any, deps: ChatDependencies, plan: dict) -> str:
    from agentfinx.api.discovery import (DiscoveryBusy, busy_payload, clarification_payload,
                                  owner_discovery_lock, search_from_planner)

    report_search = dict(plan.get("report_search") or {})
    session_id = run["session_id"]
    search, reason = search_from_planner(report_search)
    if search is None:
        payload = clarification_payload(report_search, reason, session_id=session_id)
    else:
        try:
            # Shared with the form endpoint: one discovery per owner at a time.
            with owner_discovery_lock(run["owner_id"]):
                payload = deps.discovery()(
                    search,
                    owner_id=run["owner_id"],
                    session_id=session_id,
                    suggested_ticker=report_search.get("ticker_guess") or None,
                )
        except DiscoveryBusy:
            payload = busy_payload(search, session_id=session_id)
    return _succeed_structured(database, run, payload, task_kind="report_discovery")


def _effective_question(database: webdb.WebDB, run: Any, deps: ChatDependencies) -> Clarification:
    """One model call before the dataset lock; every outcome is persisted."""
    if deps.context_enabled:
        history = load_context_history(database, run)
        result = clarify_question(run["question"], history, rewrite=deps.clarify)
    else:
        result = Clarification(run["question"], "skipped", "disabled")
    with database.transaction() as conn:
        webdb.set_run_context(conn, run["id"], question=result.question,
                              status=result.status, reason=result.reason, payload=result.payload())
    return result


CONTEXT_FAILURE_MESSAGE = (
    "Không làm rõ được ngữ cảnh câu hỏi này. "
    "Bạn có thể gửi lại câu hỏi đầy đủ, nêu rõ chỉ tiêu và kỳ cần hỏi."
)


def _finish_context(database: webdb.WebDB, run: Any, result: Clarification) -> str:
    """A clarification succeeds as a RUN, not as QA; errors survive reload."""
    needs_input = result.status == "needs_clarification"
    kind = "context_clarification" if needs_input else "context_error"
    content = (result.resolution["clarification_question"] if needs_input else CONTEXT_FAILURE_MESSAGE)
    with database.transaction() as conn:
        if webdb.get_session(conn, run["session_id"]) is None:
            return "session_deleted"
        message_id = webdb.add_message(
            conn, session_id=run["session_id"], role="assistant", content=content,
            run_id=run["id"], kind=kind,
            payload=json.dumps({"context_status": result.status, "context_reason": result.reason}),
        )
        status = "succeeded" if needs_input else "failed"
        if not webdb.finish_run(
            conn, run["id"], status=status, answer_message_id=message_id, result_kind=kind,
            error_code="" if needs_input else "context_resolution_failed",
            error_message="" if needs_input else CONTEXT_FAILURE_MESSAGE,
        ):
            raise RuntimeError("run is no longer running; discarding context result")
        webdb.touch_session(conn, run["session_id"])
    return status


def process_chat_run(database: webdb.WebDB, run_id: str, deps: ChatDependencies) -> str:
    """Execute one claimed (``running``) run and store its terminal state."""

    with database.read() as conn:
        run = webdb.get_run(conn, run_id)
        session = webdb.get_session(conn, run["session_id"]) if run is not None else None
    if run is None:
        return "missing"
    if session is None:
        return "session_deleted"

    context = _effective_question(database, run, deps)
    if not context.can_continue:
        return _finish_context(database, run, context)
    question = context.question
    keys = run.keys()
    is_override = bool(run["override_of_run_id"]) if "override_of_run_id" in keys else False
    planner_updates = None
    task_kind = ""
    if deps.discovery_enabled and not is_override:
        # Classify the task before any dataset is resolved or activated; the
        # planner reads registry metadata only and holds no lock.
        try:
            plan = deps.planner()(_planner_state(run, question))
        except Exception:
            logger.exception("chat run %s: planner failed", run_id)
            return _fail(database, run, "internal_error")
        task_kind = "report_discovery" if plan.get("task_kind") == "report_discovery" else "report_qa"
        if task_kind == "report_discovery":
            try:
                return _answer_discovery(database, run, deps, plan)
            except Exception:
                logger.exception("chat run %s: report discovery failed", run_id)
                return _fail(database, run, "internal_error")
        planner_updates = plan
    elif is_override:
        task_kind = "report_qa"
    if not run["dataset_id"]:
        # A financial question with no report: say so, never answer it.
        from agentfinx.api.discovery import dataset_required_payload

        return _succeed_structured(
            database, run, dataset_required_payload(session_id=run["session_id"]), task_kind="report_qa"
        )

    try:
        accessible = deps.resolve(run["dataset_id"], run["owner_id"], deps.shared_dataset_ids)
    except (DatasetAccessError, ValueError):
        return _fail(database, run, "dataset_unavailable", read_only=True)
    dataset = accessible.record

    try:
        with dataset_lock(dataset.dataset_id):
            try:
                activated = deps.activate(dataset)
            except DatasetNotReady as exc:
                return _fail(
                    database,
                    run,
                    "dataset_not_ready",
                    detail_code=exc.code,
                    read_only=exc.code in READ_ONLY_CODES,
                )
            if activated.generation != run["dataset_generation"]:
                return _fail(database, run, "dataset_changed", read_only=True)

            execute, collect_errors, render = deps.pipeline()
            final_state = execute(
                dataset,
                activated.collection,
                question,
                owner_id=run["owner_id"],
                admission_quota=_AlreadyAdmitted(),
                # The web planner already planned this question: start at the router.
                **({"planner_updates": planner_updates} if planner_updates is not None else {}),
            )
    except Exception:
        logger.exception("chat run %s failed while executing the pipeline", run_id)
        return _fail(database, run, "internal_error")

    errors = collect_errors(final_state)
    if errors:
        logger.warning("chat run %s: pipeline reported errors: %s", run_id, errors)
        return _fail(database, run, "pipeline_error")

    answer = str(render(final_state) or "").strip()
    if not answer:
        return _fail(database, run, "pipeline_error")
    from agentfinx.api.news_updates import needs_update

    return _succeed(database, run, answer, task_kind=task_kind,
                    snapshot=build_context_snapshot(final_state, run),
                    news_context=needs_update(final_state))
