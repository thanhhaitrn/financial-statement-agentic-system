"""Streamlit viewer for inspecting saved batch-query outputs."""
# Code note: Streamlit viewer code keeps batch-result inspection separate from workflow execution.

import json
from pathlib import Path
from typing import Any

import streamlit as st


DEFAULT_JSON_PATH = Path("batch_test_results.json")
RETRIEVAL_TRACE_EVENTS = {
    "tool:done",
    "tool:followup_done",
    "evidence_tool:done",
    "evidence_tool:cache_hit",
}


def load_batch_results(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _runtime_label(runtime_ms: Any) -> str:
    if runtime_ms in ("", None):
        return "-"
    try:
        value = int(runtime_ms)
    except (TypeError, ValueError):
        return str(runtime_ms)
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value}ms"


def _query_key(value: Any) -> str:
    return " ".join(str(value or "").split())


def _index_query_reports(payload: dict) -> dict[str, dict]:
    index = {}
    for item in payload.get("query_reports", []) or []:
        if not isinstance(item, dict):
            continue
        query = _query_key(item.get("query", ""))
        if query:
            index[query] = item
    return index


def _primary_run(query_report: dict) -> dict:
    for result in query_report.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        run = result.get("run", {})
        if isinstance(run, dict):
            return run
    return {}


def _query_records_from_payload(payload: dict) -> list[dict]:
    records = []
    for item in payload.get("queries", []) or []:
        if isinstance(item, dict):
            query = _as_text(item.get("query", ""))
            if not query:
                continue
            records.append(dict(item))
        else:
            query = _as_text(item)
            if query:
                records.append({"query": query})

    if records:
        return records

    for query_report in payload.get("query_reports", []) or []:
        if isinstance(query_report, dict) and _as_text(query_report.get("query", "")):
            records.append(
                {
                    "query": query_report.get("query", ""),
                    "references": query_report.get("references", ""),
                }
            )
    return records


def build_query_rows(payload: dict) -> list[dict]:
    query_reports = _index_query_reports(payload)
    rows = []

    for record in _query_records_from_payload(payload):
        query = _as_text(record.get("query", ""))
        query_report = query_reports.get(_query_key(query), {})
        primary_run = _primary_run(query_report)
        final_answer = (
            _as_text(record.get("final_answer", ""))
            or _as_text(primary_run.get("final_answer", ""))
            or _as_text(primary_run.get("answer", ""))
        )
        references = (
            _as_text(record.get("references", ""))
            or _as_text(query_report.get("references", ""))
            or _as_text(primary_run.get("references", ""))
        )
        runtime = record.get("runtime")
        if runtime is None:
            runtime = primary_run.get("runtime")
        total_tokens = record.get("total_tokens")
        if total_tokens is None:
            total_tokens = primary_run.get("total_tokens", 0)

        rows.append(
            {
                "query": query,
                "final_answer": final_answer,
                "references": references,
                "runtime": runtime,
                "total_tokens": int(total_tokens or 0),
                "has_answer": bool(final_answer),
                "runs_with_errors": int(query_report.get("runs_with_errors", 0) or 0),
                "datasets_with_setup_error": int(query_report.get("datasets_with_setup_error", 0) or 0),
                "datasets_n": int(query_report.get("datasets_n", 0) or 0),
                "query_report": query_report,
            }
        )

    return rows


def build_dataset_rows(query_report: dict) -> list[dict]:
    rows = []
    for result in query_report.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        run = result.get("run", {})
        if not isinstance(run, dict):
            run = {}
        run_summary = run.get("run_summary", {}) if isinstance(run.get("run_summary", {}), dict) else {}
        rows.append(
            {
                "dataset_id": result.get("dataset_id", ""),
                "company": result.get("company", ""),
                "status": result.get("status", ""),
                "synth_status": run.get("synth_status", ""),
                "runtime": run.get("runtime", run_summary.get("duration_ms")),
                "total_tokens": int(run.get("total_tokens", run_summary.get("total_tokens", 0)) or 0),
                "errors_n": len(run.get("errors", []) or []) + (1 if result.get("setup_error") else 0),
                "has_answer": bool(run.get("final_answer", "") or run.get("answer", "")),
                "result": result,
            }
        )
    return rows


def build_retrieval_rows(trace: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in trace or []:
        if item.get("event") not in RETRIEVAL_TRACE_EVENTS:
            continue
        event = item.get("event", "")
        rows.append(
            {
                "event": event,
                "kind": "followup" if "followup" in event else item.get("scope", "primary"),
                "agent": item.get("agent", ""),
                "tool": item.get("tool", ""),
                "table": item.get("table", ""),
                "query": item.get("query", ""),
                "facts_n": item.get("facts_n", ""),
                "cache_hit": item.get("cache_hit", ""),
                "duration_ms": item.get("duration_ms", ""),
            }
        )
    return rows


def _filter_query_rows(rows: list[dict], search_text: str, status_filter: str) -> list[dict]:
    search = _as_text(search_text).lower()
    output = []
    for row in rows:
        haystack = " ".join(
            [
                _as_text(row.get("query", "")),
                _as_text(row.get("final_answer", "")),
                _as_text(row.get("references", "")),
            ]
        ).lower()
        if search and search not in haystack:
            continue
        if status_filter == "Có answer" and not row.get("has_answer"):
            continue
        if status_filter == "Chưa có answer" and row.get("has_answer"):
            continue
        if status_filter == "Có lỗi" and not (
            row.get("runs_with_errors") or row.get("datasets_with_setup_error")
        ):
            continue
        output.append(row)
    return output


st.set_page_config(page_title="Batch Test Results Viewer", layout="wide")
st.title("Batch Test Results Viewer")

json_path_text = st.text_input("JSON path", value=str(DEFAULT_JSON_PATH))
json_path = Path(json_path_text)

if not json_path.exists():
    st.error(f"Không tìm thấy file: {json_path}")
    st.stop()

payload = load_batch_results(json_path)
query_rows = build_query_rows(payload)

if not query_rows:
    st.warning("Không có query nào trong file JSON.")
    st.stop()

answered_n = sum(1 for item in query_rows if item.get("has_answer"))
error_queries_n = sum(
    1
    for item in query_rows
    if item.get("runs_with_errors") or item.get("datasets_with_setup_error")
)
total_tokens = sum(int(item.get("total_tokens", 0) or 0) for item in query_rows)
runtime_values = [
    int(item.get("runtime") or 0)
    for item in query_rows
    if item.get("runtime") not in (None, "")
]

st.caption(
    f"Updated at: {payload.get('updated_at', '')} | "
    f"File: {json_path}"
)

metric_cols = st.columns(5)
metric_cols[0].metric("Queries", len(query_rows))
metric_cols[1].metric("Answered", answered_n)
metric_cols[2].metric("With errors", error_queries_n)
metric_cols[3].metric("Total tokens", f"{total_tokens:,}")
metric_cols[4].metric(
    "Avg runtime",
    _runtime_label(sum(runtime_values) // len(runtime_values) if runtime_values else None),
)

filter_cols = st.columns([2, 1])
with filter_cols[0]:
    search_text = st.text_input("Search query / answer / reference", value="")
with filter_cols[1]:
    status_filter = st.selectbox(
        "Status",
        ["Tất cả", "Có answer", "Chưa có answer", "Có lỗi"],
    )

filtered_rows = _filter_query_rows(query_rows, search_text, status_filter)
if not filtered_rows:
    st.warning("Không có query phù hợp với bộ lọc.")
    st.stop()

overview_rows = [
    {
        "query": item["query"],
        "has_answer": item["has_answer"],
        "runtime": _runtime_label(item.get("runtime")),
        "total_tokens": item.get("total_tokens", 0),
        "runs_with_errors": item.get("runs_with_errors", 0),
        "setup_errors": item.get("datasets_with_setup_error", 0),
        "reference": item.get("references", ""),
    }
    for item in filtered_rows
]

st.subheader("Overview")
st.dataframe(overview_rows, use_container_width=True, hide_index=True)

query_options = [item["query"] for item in filtered_rows]
selected_query = st.selectbox("Chọn query", query_options)
selected = next(item for item in filtered_rows if item["query"] == selected_query)
query_report = selected.get("query_report", {}) or {}

st.subheader("Question")
st.write(selected["query"])

answer_col, reference_col = st.columns(2)
with answer_col:
    st.markdown("**Final answer**")
    if selected.get("final_answer"):
        st.markdown(selected["final_answer"])
    else:
        st.info("Query này chưa có final_answer.")

with reference_col:
    st.markdown("**Reference**")
    if selected.get("references"):
        st.markdown(selected["references"])
    else:
        st.info("Query này chưa có reference.")

detail_cols = st.columns(4)
detail_cols[0].metric("Runtime", _runtime_label(selected.get("runtime")))
detail_cols[1].metric("Tokens", f"{int(selected.get('total_tokens', 0) or 0):,}")
detail_cols[2].metric("Datasets", query_report.get("datasets_n", selected.get("datasets_n", 0)))
detail_cols[3].metric(
    "Errors",
    int(query_report.get("runs_with_errors", 0) or 0)
    + int(query_report.get("datasets_with_setup_error", 0) or 0),
)

dataset_rows = build_dataset_rows(query_report)
if dataset_rows:
    st.subheader("Dataset Details")
    st.dataframe(
        [
            {
                "dataset_id": item["dataset_id"],
                "company": item["company"],
                "status": item["status"],
                "synth_status": item["synth_status"],
                "runtime": _runtime_label(item["runtime"]),
                "total_tokens": item["total_tokens"],
                "errors_n": item["errors_n"],
                "has_answer": item["has_answer"],
            }
            for item in dataset_rows
        ],
        use_container_width=True,
        hide_index=True,
    )

    dataset_options = [
        item["dataset_id"] or f"dataset #{idx + 1}"
        for idx, item in enumerate(dataset_rows)
    ]
    selected_dataset_label = st.selectbox("Chọn dataset", dataset_options)
    selected_dataset = dataset_rows[dataset_options.index(selected_dataset_label)]
    result = selected_dataset.get("result", {}) or {}
    run = result.get("run", {}) if isinstance(result.get("run", {}), dict) else {}
    trace = run.get("trace", []) or []
    run_summary = run.get("run_summary", {}) or {}
    retrieval_rows = build_retrieval_rows(trace)

    if result.get("setup_error"):
        st.error(f"Setup error: {result.get('setup_error')}")

    st.markdown("**Dataset answer**")
    dataset_answer = run.get("final_answer", "") or run.get("answer", "")
    if dataset_answer:
        st.markdown(dataset_answer)
    elif run.get("formatted_answer"):
        st.markdown(run.get("formatted_answer"))
    else:
        st.info("Dataset này chưa có answer.")

    st.markdown("**Dataset metadata**")
    st.json(
        {
            "dataset_id": result.get("dataset_id", ""),
            "description": result.get("description", ""),
            "status": result.get("status", ""),
            "synth_status": run.get("synth_status", ""),
            "runtime": run.get("runtime", run_summary.get("duration_ms")),
            "total_tokens": run.get("total_tokens", run_summary.get("total_tokens", 0)),
            "missing": run.get("missing", []) or [],
            "errors": run.get("errors", []) or [],
            "run_summary": run_summary,
        },
        expanded=False,
    )

    st.subheader("Retrieval / Context Summary")
    if retrieval_rows:
        st.dataframe(retrieval_rows, use_container_width=True, hide_index=True)
    else:
        st.info("Không có trace retrieval trong run này.")

    with st.expander("Full trace", expanded=False):
        st.json(trace, expanded=False)

    with st.expander("Raw dataset result", expanded=False):
        st.json(result, expanded=False)
else:
    st.info("Không có query_reports/results chi tiết cho query này.")

with st.expander("Raw query report", expanded=False):
    st.json(query_report, expanded=False)
