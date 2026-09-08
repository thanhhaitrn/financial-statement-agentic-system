"""Streamlit viewer for routing A/B run directories.

Reads any directory of ``<config>_q<N>.json`` blobs produced by an A/B run and
shows the routing decision, retrieval breadth and final answer side by side, so
a configuration change can be judged without opening five JSON files at once.

Run it with::

    streamlit run scripts/ab_viewer.py

The directory layout is discovered, not hardcoded: drop a new run next to the
existing ones under ``ragas_runs/`` and it appears in the picker.
"""
# Code note: Evaluation tooling is read-only; it must never mutate a run's output.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "ragas_runs"
BLOB_RE = re.compile(r"^(?P<config>.+)_q(?P<question>\d+)\.json$")

# Phrases that assert data is unavailable. Counting them is the cheapest proxy
# for the failure this A/B was built to catch: a configuration that retrieves
# less makes the model declare figures missing that are in the report.
MISSING_DATA_PHRASES = (
    "chưa được cung cấp",
    "không thể tính",
    "chưa thể",
    "thiếu dữ liệu",
    "thiếu số liệu",
    "không có dữ liệu",
    "chưa có dữ liệu",
    "không đủ dữ liệu",
    "không tìm thấy",
)

# Configurations are shown in this order when present; unknown ones follow.
CONFIG_ORDER = ("legacy", "shadow", "model_first", "default")

METRIC_COLUMNS = {
    "facts_total": "Facts",
    "evidence_items_n": "Evidence items",
    "missing_data_claims": "Câu 'thiếu dữ liệu'",
    "total_tokens": "Tokens",
    "elapsed_s": "Giây",
    "answer_chars": "Ký tự đáp án",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def count_missing_data_claims(answer: str) -> int:
    text = str(answer or "").lower()
    return sum(text.count(phrase) for phrase in MISSING_DATA_PHRASES)


@st.cache_data(show_spinner=False)
def load_run(directory: str) -> list[dict[str, Any]]:
    """Load every ``<config>_q<N>.json`` blob in one run directory."""

    rows: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        match = BLOB_RE.match(path.name)
        if not match:
            continue  # summary.json and friends are not per-question blobs
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            st.warning(f"Bỏ qua {path.name}: {exc}")
            continue

        answer = str(blob.get("answer", "") or "")
        facts_by_table = blob.get("facts_by_table", {}) or {}
        rows.append(
            {
                "config": match.group("config"),
                "question": int(match.group("question")),
                "query": blob.get("query", ""),
                "difficulty": blob.get("difficulty", ""),
                "response_mode": blob.get("response_mode", ""),
                "planner_axes": blob.get("planner_axes", []) or [],
                "analysis_plan": blob.get("analysis_plan", []) or [],
                "worker_agents": blob.get("worker_agents", []) or [],
                "router_mode": blob.get("router_mode", ""),
                "router_bypassed": bool(blob.get("router_bypassed", False)),
                "facts_by_table": facts_by_table,
                "facts_total": int(blob.get("facts_total", 0) or 0),
                "evidence_items_n": int(blob.get("evidence_items_n", 0) or 0),
                "evidence_queries": blob.get("evidence_queries", []) or [],
                "total_tokens": int(blob.get("total_tokens", 0) or 0),
                "llm_calls": int(blob.get("llm_calls", 0) or 0),
                "elapsed_s": float(blob.get("elapsed_s", 0) or 0),
                "errors": blob.get("errors", []) or [],
                "notable_events": blob.get("notable_events", []) or [],
                "answer": answer,
                "answer_chars": len(answer),
                "missing_data_claims": count_missing_data_claims(answer),
                "file": path.name,
            }
        )
    return rows


def order_configs(configs: list[str]) -> list[str]:
    known = [name for name in CONFIG_ORDER if name in configs]
    return known + sorted(name for name in configs if name not in CONFIG_ORDER)


def find_run_dirs() -> list[Path]:
    if not RUNS_DIR.is_dir():
        return []
    return sorted(
        (path for path in RUNS_DIR.iterdir() if path.is_dir() and any(path.glob("*_q*.json"))),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_overview(rows: list[dict], configs: list[str]) -> None:
    st.subheader("Tổng quan")
    metric_label = st.selectbox(
        "Chỉ số",
        list(METRIC_COLUMNS.values()),
        index=list(METRIC_COLUMNS).index("missing_data_claims"),
        help="Ô trống = lượt chạy đó không có trong thư mục.",
    )
    metric_key = next(k for k, v in METRIC_COLUMNS.items() if v == metric_label)

    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(
        index="question", columns="config", values=metric_key, aggfunc="first"
    )
    pivot = pivot.reindex(columns=[c for c in configs if c in pivot.columns])
    labels = {
        row["question"]: f"q{row['question']} · {row['difficulty']}/{row['response_mode']}"
        for row in rows
    }
    pivot.index = [labels.get(index, f"q{index}") for index in pivot.index]

    lower_is_better = metric_key in {
        "missing_data_claims",
        "total_tokens",
        "elapsed_s",
    }
    st.dataframe(
        pivot.style.background_gradient(
            cmap="RdYlGn_r" if lower_is_better else "RdYlGn", axis=1
        ).format(precision=1),
        use_container_width=True,
    )
    st.caption(
        "Xanh = tốt hơn trên cùng một câu hỏi. "
        + ("Thấp hơn là tốt hơn." if lower_is_better else "Cao hơn là tốt hơn.")
    )


def render_routing_table(rows: list[dict], configs: list[str]) -> None:
    st.subheader("Quyết định routing")
    records = []
    for row in sorted(rows, key=lambda item: (item["question"], configs.index(item["config"]))):
        records.append(
            {
                "q": row["question"],
                "config": row["config"],
                "difficulty": row["difficulty"],
                "response_mode": row["response_mode"],
                "axes": len(row["planner_axes"]),
                "agents": ", ".join(a.replace("agent_", "") for a in row["analysis_plan"]) or "—",
                "bypass": "✓" if row["router_bypassed"] else "",
                "lỗi": len(row["errors"]),
            }
        )
    st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)
    st.caption(
        "Hai config khác nhau mà cùng difficulty/response_mode/axes nghĩa là "
        "quyết định routing không đổi — khác biệt (nếu có) nằm ở retrieval."
    )


def render_facts_by_table(subset: list[dict], configs: list[str]) -> None:
    tables = sorted({name for row in subset for name in row["facts_by_table"]})
    if not tables:
        st.info("Lượt chạy này không có fact nào theo bảng.")
        return
    frame = pd.DataFrame(
        {
            row["config"]: [row["facts_by_table"].get(table, 0) for table in tables]
            for row in sorted(subset, key=lambda item: configs.index(item["config"]))
        },
        index=[table[:38] for table in tables],
    )
    frame.loc["TỔNG"] = frame.sum()
    st.dataframe(
        frame.style.background_gradient(cmap="Blues", axis=None),
        use_container_width=True,
    )


def highlight_missing_claims(answer: str) -> str:
    """Mark every 'data is missing' assertion so it is visible while skimming."""

    marked = answer
    for phrase in MISSING_DATA_PHRASES:
        marked = re.sub(
            re.escape(phrase), f":red-background[{phrase}]", marked, flags=re.IGNORECASE
        )
    return marked


def render_question_detail(rows: list[dict], configs: list[str]) -> None:
    questions = sorted({row["question"] for row in rows})
    question = st.selectbox(
        "Câu hỏi",
        questions,
        format_func=lambda q: next(
            f"q{q} · {r['difficulty']}/{r['response_mode']} — {r['query'][:60]}"
            for r in rows
            if r["question"] == q
        ),
    )
    subset = [row for row in rows if row["question"] == question]
    present = [config for config in configs if any(r["config"] == config for r in subset)]
    st.markdown(f"**Query:** {subset[0]['query']}")

    baseline = st.selectbox(
        "So sánh với", present, index=0, help="Cột delta được tính so với config này."
    )
    base_row = next(row for row in subset if row["config"] == baseline)

    columns = st.columns(len(present))
    for column, config in zip(columns, present):
        row = next(item for item in subset if item["config"] == config)
        with column:
            st.markdown(f"### {config}")
            for key, label in METRIC_COLUMNS.items():
                delta = row[key] - base_row[key]
                st.metric(
                    label,
                    f"{row[key]:,.0f}" if key != "elapsed_s" else f"{row[key]:.1f}",
                    delta=None if config == baseline or delta == 0 else f"{delta:+,.0f}",
                    delta_color="inverse"
                    if key in {"missing_data_claims", "total_tokens", "elapsed_s"}
                    else "normal",
                )
            if row["errors"]:
                st.error("\n".join(str(e)[:200] for e in row["errors"]))

    st.markdown("#### Facts theo bảng")
    render_facts_by_table(subset, configs)

    st.markdown("#### Đáp án")
    answer_columns = st.columns(len(present))
    for column, config in zip(answer_columns, present):
        row = next(item for item in subset if item["config"] == config)
        with column:
            st.markdown(f"**{config}** · {row['missing_data_claims']} câu 'thiếu dữ liệu'")
            with st.container(height=520, border=True):
                st.markdown(highlight_missing_claims(row["answer"]))

    with st.expander("Evidence queries và trace"):
        for config in present:
            row = next(item for item in subset if item["config"] == config)
            st.markdown(f"**{config}** — `{row['file']}`")
            if row["evidence_queries"]:
                st.code("\n".join(row["evidence_queries"]), language="text")
            st.json(row["notable_events"], expanded=False)


def main() -> None:
    st.set_page_config(page_title="Routing A/B viewer", layout="wide")
    st.title("Routing A/B viewer")

    run_dirs = find_run_dirs()
    with st.sidebar:
        st.header("Thư mục kết quả")
        options = [str(path.relative_to(ROOT)) for path in run_dirs]
        choice = st.selectbox("Run", options + ["(nhập đường dẫn khác)"]) if options else None
        if not options or choice == "(nhập đường dẫn khác)":
            choice = st.text_input("Đường dẫn", value=str(RUNS_DIR))
        directory = (ROOT / choice).resolve()
        st.caption(str(directory))
        if st.button("Đọc lại", use_container_width=True):
            load_run.clear()

    if not directory.is_dir():
        st.error(f"Không tìm thấy thư mục: {directory}")
        return

    rows = load_run(str(directory))
    if not rows:
        st.warning(
            "Không có file nào dạng `<config>_q<N>.json` trong thư mục này."
        )
        return

    configs = order_configs(sorted({row["config"] for row in rows}))
    with st.sidebar:
        selected = st.multiselect("Config", configs, default=configs)
        st.metric("Lượt chạy", len(rows))
        failed = sum(1 for row in rows if row["errors"])
        if failed:
            st.warning(f"{failed} lượt có lỗi — kết quả không dùng để so sánh được.")

    rows = [row for row in rows if row["config"] in selected]
    configs = [config for config in configs if config in selected]
    if not rows:
        st.info("Chọn ít nhất một config.")
        return

    overview_tab, routing_tab, detail_tab = st.tabs(
        ["Tổng quan", "Routing", "Chi tiết từng câu"]
    )
    with overview_tab:
        render_overview(rows, configs)
    with routing_tab:
        render_routing_table(rows, configs)
    with detail_tab:
        render_question_detail(rows, configs)


if __name__ == "__main__":
    main()
