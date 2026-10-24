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
                 web_entry: bool = False, web_provider=None, as_of: str | None = None,
                 experiment_context=None, controlled_web_facts: list[dict] | None = None) -> dict:
    """The workflow's final state for ``question``.

    ``web_provider``, ``as_of``, ``experiment_context`` and
    ``controlled_web_facts`` are for experiments (a fixed news source, a fixed
    "now", a recorded run, WEB facts at the workers' input); batch and predict
    leave them unset and get the product's own."""

    planner_updates = None
    if web_entry:
        planner_updates = plan_web_entry(dataset, question)
        if planner_updates.get("task_kind") == "report_discovery":
            raise RuntimeError(WEB_ENTRY_MISROUTE)
    extra = {}
    if planner_updates is not None:
        extra["planner_updates"] = planner_updates
    if web_provider is not None:
        extra["web_provider"] = web_provider
    if as_of is not None:
        extra["as_of"] = as_of
    if experiment_context is not None:
        extra["experiment_context"] = experiment_context
    if controlled_web_facts:
        extra["controlled_web_facts"] = controlled_web_facts
    return pipeline.execute_query(
        dataset,
        collection,
        question,
        debug_trace=debug_trace,
        **extra,
    )
