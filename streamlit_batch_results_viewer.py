import json
from pathlib import Path

import streamlit as st


DEFAULT_JSON_PATH = Path("batch_test_results.json")
TRACE_EVENTS_WITH_CONTEXT = {"tool:done", "tool:followup_done"}


def load_batch_results(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten_runs(payload: dict) -> list[dict]:
    rows: list[dict] = []

    for query_report in payload.get("query_reports", []) or []:
        query = query_report.get("query", "")
        for result in query_report.get("results", []) or []:
            run = result.get("run", {}) or {}
            rows.append(
                {
                    "query": query,
                    "dataset_id": result.get("dataset_id", ""),
                    "company": result.get("company", ""),
                    "description": result.get("description", ""),
                    "status": result.get("status", ""),
                    "synth_status": run.get("synth_status", ""),
                    "answer": run.get("answer", "") or run.get("formatted_answer", ""),
                    "formatted_answer": run.get("formatted_answer", ""),
                    "missing": run.get("missing", []) or [],
                    "errors": run.get("errors", []) or [],
                    "trace": run.get("trace", []) or [],
                    "run_summary": run.get("run_summary", {}) or {},
                    "raw": result,
                }
            )

    return rows


def build_retrieval_rows(trace: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in trace or []:
        if item.get("event") not in TRACE_EVENTS_WITH_CONTEXT:
            continue
        event = item.get("event", "")
        rows.append(
            {
                "kind": "followup" if event == "tool:followup_done" else "primary",
                "agent": item.get("agent", ""),
                "table": item.get("table", ""),
                "query": item.get("query", ""),
                "trigger": item.get("trigger", ""),
                "followup_index": item.get("followup_index", ""),
                "context_len": item.get("context_len", 0),
                "empty": item.get("empty", False),
                "duration_ms": item.get("duration_ms", 0),
            }
        )
    return rows


st.set_page_config(page_title="Batch Test Results Viewer", layout="wide")
st.title("📊 Batch Test Results Viewer")

json_path_text = st.text_input("JSON path", value=str(DEFAULT_JSON_PATH))
json_path = Path(json_path_text)

if not json_path.exists():
    st.error(f"Không tìm thấy file: {json_path}")
    st.stop()

payload = load_batch_results(json_path)
items = flatten_runs(payload)

if not items:
    st.warning("Không có run nào trong file JSON.")
    st.stop()

all_queries = sorted({item["query"] for item in items if item["query"]})
all_datasets = sorted({item["dataset_id"] for item in items if item["dataset_id"]})

col1, col2 = st.columns(2)
with col1:
    selected_query = st.selectbox("Filter query", ["Tất cả"] + all_queries)
with col2:
    selected_dataset = st.selectbox("Filter dataset", ["Tất cả"] + all_datasets)

filtered_items = [
    item
    for item in items
    if (selected_query == "Tất cả" or item["query"] == selected_query)
    and (selected_dataset == "Tất cả" or item["dataset_id"] == selected_dataset)
]

if not filtered_items:
    st.warning("Không có sample phù hợp với bộ lọc.")
    st.stop()

idx = st.slider("Chọn sample", 0, len(filtered_items) - 1, 0)
item = filtered_items[idx]

summary = item["run_summary"]
retrieval_rows = build_retrieval_rows(item["trace"])

st.caption(
    f"Updated at: {payload.get('updated_at', '')} | "
    f"Total queries: {payload.get('queries_n', 0)} | "
    f"Filtered runs: {len(filtered_items)}"
)

meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
meta_col1.metric("Dataset", item["dataset_id"] or "-")
meta_col2.metric("Company", item["company"] or "-")
meta_col3.metric("Run status", item["synth_status"] or "-")
meta_col4.metric("Trace events", summary.get("trace_events_n", len(item["trace"])))

st.subheader("🟡 Question")
st.write(item["query"])

st.subheader("🟢 Answer")
if item["answer"]:
    st.markdown(item["answer"])
else:
    st.info("Run này không có answer.")

st.subheader("📎 Metadata")
st.json(
    {
        "description": item["description"],
        "status": item["status"],
        "synth_status": item["synth_status"],
        "missing": item["missing"],
        "errors": item["errors"],
        "run_summary": item["run_summary"],
    },
    expanded=False,
)

st.subheader("🔵 Retrieval / Context Summary")
if retrieval_rows:
    st.dataframe(retrieval_rows, use_container_width=True)
else:
    st.info("File này không lưu raw contexts; chỉ có trace và thống kê retrieval.")

with st.expander("Full trace", expanded=False):
    st.json(item["trace"], expanded=False)

with st.expander("Raw result record", expanded=False):
    st.json(item["raw"], expanded=False)
