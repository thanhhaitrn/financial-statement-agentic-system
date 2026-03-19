from agents.planner_hints import infer_time_hint
from agents.profiles import AGENT_PROFILES
from datasets.registry import get_dataset
from schemas.agent_outputs import PlannerEvidencePlan
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log

DEFAULT_PLAN_TABLES = {
    "question_type": "lookup",
    "analysis_axes": [],
    "company": "",
    "time_hint": "",
    "need_web": False,
}

planner_chain = PROMPT_TEMPLATE | llm.with_structured_output(PlannerEvidencePlan)


def _enrich_plan_fields(state: dict, plan: dict) -> dict:
    enriched = dict(plan or {})
    dataset = None
    dataset_id = str((state or {}).get("dataset_id", "") or "").strip()
    if dataset_id:
        dataset = get_dataset(dataset_id)

    if not str(enriched.get("company", "") or "").strip() and dataset is not None:
        enriched["company"] = dataset.company

    if not str(enriched.get("time_hint", "") or "").strip():
        enriched["time_hint"] = infer_time_hint(
            str((state or {}).get("user_query", "") or ""),
            dataset_fiscal_year=getattr(dataset, "fiscal_year", None),
            dataset_fiscal_quarter=getattr(dataset, "fiscal_quarter", None),
        )

    return enriched


def run_planner(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_planner"]
    trace = []

    start_log = make_debug_log(
        state,
        "planner:start",
        user_query=state.get("user_query", ""),
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": "{}",
        "worker_results_json": "{}",
        "allowed_keywords_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    updates = {
        "last_agent": "agent_planner",
        "trace": trace,
    }

    try:
        plan_obj: PlannerEvidencePlan = planner_chain.invoke(payload)
        updates["plan_tables"] = _enrich_plan_fields(state, plan_obj.model_dump())
        updates["trace"].append(
            make_log(
                state,
                "planner:done",
                plan_tables=updates["plan_tables"],
            )
        )
    except Exception as e:
        updates["plan_tables"] = _enrich_plan_fields(state, DEFAULT_PLAN_TABLES)
        updates["trace"].append(
            make_log(
                state,
                "planner:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )

    return updates
