"""One evaluation question, answered the way the product answers it.

Shared by ``agentfinx batch`` (many questions, resumable output) and
``agentfinx predict`` (answers and retrieved contexts for scoring); they differ
only in what they record. With ``web_entry`` the question first goes through
the web chat's planner, exactly as the chat worker calls it. A question that
planner sends to report discovery is not answered: on the web it would get a
candidate card, so it fails with ``WEB_ENTRY_MISROUTE`` and counts as a refusal.
"""

from __future__ import annotations

from agentfinx.workflow import pipeline

WEB_ENTRY_MISROUTE = "web_entry:routed_to_report_discovery"


def plan_web_entry(dataset, question: str) -> dict:
    """The web chat's planner call for one question (same function and policy
    context as the chat worker)."""

    from agentfinx.agents.planner_runner import run_web_planner
    from agentfinx.workflow.graph import run_with_services
    from agentfinx.workflow.state import WorkflowServices

    state = {"user_query": question, "dataset_id": dataset.dataset_id, "debug_trace": False,
             "tool_observations": [], "run_id": "batch", "trace": []}
    return run_with_services(run_web_planner, WorkflowServices(collection=None), state)


def run_question(dataset, collection, question: str, *, debug_trace: bool = False,
                 web_entry: bool = False) -> dict:
    """The workflow's final state for ``question``."""

    planner_updates = None
    if web_entry:
        planner_updates = plan_web_entry(dataset, question)
        if planner_updates.get("task_kind") == "report_discovery":
            raise RuntimeError(WEB_ENTRY_MISROUTE)
    return pipeline.execute_query(
        dataset,
        collection,
        question,
        debug_trace=debug_trace,
        **({"planner_updates": planner_updates} if planner_updates is not None else {}),
    )
