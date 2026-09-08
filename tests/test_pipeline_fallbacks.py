"""Regression tests for test pipeline fallbacks."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import agent_runner, keyworder_runner, planner_runner, synth_runner
from graph import dispatch_nodes
from graph import evidence as evidence_node
from graph.router import build_worker_query, route_after_evidence
from ingestion.table_parser import attach_context
from schemas.agent_outputs import EvidenceDispatchPlan
from schemas.table_names import (
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from tools import tools as tools_module
from tools.evidence import result_to_facts
from tools.tools import get_related_info


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"


class FakeCollection:
    def __init__(self, primary_result, fallback_result):
        self.primary_result = primary_result
        self.fallback_result = fallback_result
        self.calls = []

    def query(self, query_embeddings, n_results, where=None):
        self.calls.append(
            {
                "query_embeddings": list(query_embeddings),
                "n_results": n_results,
                "where": where,
            }
        )
        if where is not None:
            return self.primary_result
        return self.fallback_result


def test_run_planner_uses_default_plan_when_output_is_invalid(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    planner_plan = updates["planner_plan"]

    assert planner_plan["difficulty_level"] == "easy"
    assert planner_plan["company"] == "Hòa Phát"
    assert planner_plan["time_hint"] == "30/06/2025"
    assert planner_plan["analysis_axes"] == []
    assert any(log["event"] == "planner:error" for log in updates["trace"])


def test_run_planner_downgrades_direct_balance_sheet_line_item(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": {
                "difficulty_level": "hard",
                "analysis_axes": [
                    {
                        "axis": "agent_profitability",
                        "objective": "Đánh giá khả năng sinh lời dài hạn của công ty.",
                    },
                    {
                        "axis": "agent_cashflow_analysis",
                        "objective": "Đánh giá dòng tiền cho đầu tư dài hạn.",
                    },
                ],
                "company": "Công ty Cổ phần Sông Đà",
                "time_hint": "",
                "need_web": True,
            },
            "raw": None,
            "mode": "structured",
        },
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "đầu tư tài chính dài hạn",
            "dataset_id": "",
            "debug_trace": True,
        }
    )

    planner_plan = updates["planner_plan"]
    downgrade_logs = [
        log
        for log in updates["trace"]
        if log["event"] == "planner:difficulty_downgraded_for_direct_line_item"
    ]

    assert planner_plan["difficulty_level"] == "easy"
    assert planner_plan["analysis_axes"] == []
    assert planner_plan["need_web"] is False
    assert len(downgrade_logs) == 1
    assert downgrade_logs[0]["direct_line_item"] == "đầu tư tài chính dài hạn"
    assert downgrade_logs[0]["table"] == TABLE_BS


def test_run_planner_keeps_hard_for_evaluative_line_item_query(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": {
                "difficulty_level": "hard",
                "analysis_axes": [
                    {
                        "axis": "agent_liquidity_solvency",
                        "objective": "Đánh giá rủi ro liên quan đến đầu tư tài chính dài hạn.",
                    },
                ],
                "company": "",
                "time_hint": "",
                "need_web": False,
            },
            "raw": None,
            "mode": "structured",
        },
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "đánh giá rủi ro đầu tư tài chính dài hạn",
            "dataset_id": "",
            "debug_trace": True,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "hard"
    assert len(updates["planner_plan"]["analysis_axes"]) == 1


def test_direct_router_preserves_raw_line_item_query_with_date():
    raw_query = "Tài sản dài hạn tại 31/12/2024 là bao nhiêu?"

    worker_plan = keyworder_runner._direct_router_plan_from_query(
        {
            "difficulty_level": "easy",
            "analysis_axes": [],
            "need_web": False,
        },
        raw_query,
    )

    assert worker_plan["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "query": raw_query,
            "canonical_query": "tài sản dài hạn",
            "needby": [],
        }
    ]


def test_run_planner_hides_dataset_company_mismatch_when_debug_is_off(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )
    monkeypatch.setattr(
        planner_runner,
        "get_dataset",
        lambda dataset_id: SimpleNamespace(
            company="Công ty Cổ phần Sông Đà",
            fiscal_year=2024,
            fiscal_quarter=None,
        ),
    )

    updates = planner_runner.run_planner(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "dataset_id": "song-da-2024",
            "debug_trace": False,
        }
    )

    mismatch_logs = [log for log in updates["trace"] if log["event"] == "planner:dataset_company_mismatch"]

    assert len(mismatch_logs) == 0


def test_run_planner_logs_dataset_company_mismatch_in_debug_mode(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )
    monkeypatch.setattr(
        planner_runner,
        "get_dataset",
        lambda dataset_id: SimpleNamespace(
            company="Công ty Cổ phần Sông Đà",
            fiscal_year=2024,
            fiscal_quarter=None,
        ),
    )

    updates = planner_runner.run_planner(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "dataset_id": "song-da-2024",
            "debug_trace": True,
        }
    )

    mismatch_logs = [log for log in updates["trace"] if log["event"] == "planner:dataset_company_mismatch"]

    assert len(mismatch_logs) == 1
    assert mismatch_logs[0]["query_company"] == "Hòa Phát"


def test_run_keyworder_normalizes_table_keywords_to_retrieval_target(monkeypatch):
    monkeypatch.setattr(
        keyworder_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": {
                "targets": [
                    {
                        "table": TABLE_IS,
                        "keywords": [
                            "lợi nhuận thuần từ hoạt động kinh doanh",
                            "doanh thu bán hàng và cung cấp dịch vụ",
                        ],
                    }
                ]
            },
            "raw": "",
            "mode": "structured",
        },
    )

    updates = keyworder_runner.run_keyworder(
        {
            "user_query": "Biên lợi nhuận ròng của công ty là bao nhiêu?",
            "planner_plan": {
                "analysis_axes": [
                    {
                        "axis": "net_profit_margin",
                        "tables": [TABLE_IS],
                        "objective": "Lấy lợi nhuận ròng và doanh thu từ BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH, tính biên lợi nhuận ròng = (Lợi nhuận ròng / Doanh thu) * 100%",
                    }
                ],
                "tables": [TABLE_IS],
            },
        }
    )

    assert updates["worker_plan"]["evidence_plan"] == [
        {
            "table": TABLE_IS,
            "needby": [],
            "queries": [
                "lợi nhuận thuần từ hoạt động kinh doanh",
                "doanh thu bán hàng và cung cấp dịch vụ",
            ],
        }
    ]
    assert updates["worker_plan"]["targets"] == []


def test_followup_router_normalizes_main_report_requirement_to_allowed_keyword():
    worker_plan = {"targets": []}
    planner_plan = {
        "followup_mode": True,
        "followup_requirements": [
            "cần dữ liệu vốn chủ sở hữu để tính ROE",
        ],
    }

    normalized = keyworder_runner._normalize_followup_router_targets(
        worker_plan,
        planner_plan,
    )

    assert normalized["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "query": "vốn chủ sở hữu",
            "needby": [],
        }
    ]
    assert normalized["targets"] == []


def test_followup_router_prefers_main_report_allowed_keywords_over_note_hint():
    worker_plan = {
        "targets": [
            {
                "table": TABLE_NOTE,
                "requirements": ["tổng tài sản và tài sản lưu động"],
            }
        ]
    }
    planner_plan = {
        "followup_mode": True,
        "followup_requirements": [
            "tổng tài sản và tài sản lưu động",
        ],
    }

    normalized = keyworder_runner._normalize_followup_router_targets(
        worker_plan,
        planner_plan,
    )

    assert normalized["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "needby": [],
            "queries": [
                "tổng cộng tài sản",
                "tài sản ngắn hạn",
            ],
        }
    ]
    assert normalized["targets"] == []


def test_followup_router_routes_debt_detail_to_note_without_allowed_keyword_requirement():
    worker_plan = {"targets": []}
    planner_plan = {
        "followup_mode": True,
        "followup_requirements": [
            "cần dữ liệu kỳ hạn vay và tài sản bảo đảm để đánh giá rủi ro thanh khoản",
        ],
    }

    normalized = keyworder_runner._normalize_followup_router_targets(
        worker_plan,
        planner_plan,
    )

    assert normalized["evidence_plan"] == [
        {
            "table": TABLE_NOTE,
            "query": "kỳ hạn vay và tài sản bảo đảm",
            "needby": [],
        }
    ]
    assert normalized["targets"] == []


def test_router_finalize_requires_comparative_core_facts_for_broad_profitability():
    planner_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_axes": [
            {
                "axis": "agent_profitability",
                "objective": "Đánh giá khả năng sinh lời.",
            },
            {
                "axis": "agent_cashflow_analysis",
                "objective": "Đánh giá chất lượng dòng tiền.",
            },
            {
                "axis": "agent_efficiency",
                "objective": "Đánh giá hiệu quả hoạt động.",
            },
        ],
    }

    finalized = keyworder_runner._finalize_router_targets(
        {
            "evidence_plan": [
                {
                    "table": TABLE_IS,
                    "query": "doanh thu thuần",
                    # Simulate the failure mode where Router retrieved revenue
                    # for efficiency but hid it from profitability.
                    "needby": ["agent_efficiency"],
                },
                {
                    "table": TABLE_IS,
                    "query": "lợi nhuận sau thuế",
                    "needby": ["agent_profitability"],
                },
                {
                    "table": TABLE_BS,
                    "queries": ["tài sản ngắn hạn", "nợ ngắn hạn"],
                    "needby": ["agent_efficiency"],
                },
                {
                    "table": TABLE_NOTE,
                    "query": "Đánh giá khả năng sinh lời của công ty",
                    "needby": ["agent_profitability"],
                },
            ],
            "targets": [],
        },
        planner_plan,
        user_query="Đánh giá khả năng sinh lời của công ty",
    )

    core_routes = []
    for item in finalized["evidence_plan"]:
        for query in keyworder_runner._evidence_item_queries(item):
            query_metadata = dict(
                (item.get("query_metadata", {}) or {}).get(query, {}) or {}
            )
            canonical_query = str(
                (item.get("canonical_queries", {}) or {}).get(query, "")
                or item.get("canonical_query", "")
                or query
            )
            core_routes.append(
                (
                    item["table"],
                    canonical_query,
                    item["needby"],
                    query_metadata.get(
                        "period_role", item.get("period_role", "")
                    ),
                    query_metadata.get("period", item.get("period", "")),
                )
            )
    assert core_routes == [
        (
            TABLE_IS,
            "doanh thu thuần về bán hàng và cung cấp dịch vụ",
            ["agent_profitability", "agent_efficiency"],
            "both",
            "",
        ),
        (
            TABLE_IS,
            "lợi nhuận gộp về bán hàng và cung cấp dịch vụ",
            ["agent_profitability"],
            "both",
            "",
        ),
        (
            TABLE_IS,
            "lợi nhuận thuần từ hoạt động kinh doanh",
            ["agent_profitability"],
            "both",
            "",
        ),
        (
            TABLE_IS,
            "lợi nhuận sau thuế thu nhập doanh nghiệp",
            ["agent_profitability", "agent_cashflow_analysis"],
            "both",
            "",
        ),
        (
            TABLE_BS,
            "tổng cộng tài sản",
            ["agent_profitability", "agent_efficiency"],
            "",
            "both",
        ),
        (
            TABLE_BS,
            "tổng vốn chủ sở hữu",
            ["agent_profitability"],
            "",
            "both",
        ),
        (
            TABLE_CF,
            "lưu chuyển tiền thuần từ hoạt động kinh doanh",
            ["agent_cashflow_analysis"],
            "both",
            "",
        ),
    ]
    analysis_by_agent = {
        item["agent"]: item for item in finalized["analysis_plan"]
    }
    core_queries = [
        query
        for item in finalized["evidence_plan"]
        for query in keyworder_runner._evidence_item_queries(item)
    ]
    revenue_query, gross_query, operating_query, pat_query = core_queries[:4]
    assets_query, equity_query, cfo_query = core_queries[4:]
    assert [
        item["query"]
        for item in analysis_by_agent["agent_profitability"]["evidence_queries"]
    ] == [
        revenue_query,
        gross_query,
        operating_query,
        pat_query,
        assets_query,
        equity_query,
    ]
    assert [
        item["query"]
        for item in analysis_by_agent["agent_cashflow_analysis"]["evidence_queries"]
    ] == [pat_query, cfo_query]
    assert [
        item["query"]
        for item in analysis_by_agent["agent_efficiency"]["evidence_queries"]
    ] == [revenue_query, assets_query]
    assert finalized["targets"] == finalized["analysis_plan"]


def test_router_does_not_expand_standalone_roa_assessment_to_broad_core():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "evidence_plan": [
                {
                    "table": TABLE_BS,
                    "query": "tổng cộng tài sản",
                    "needby": ["agent_profitability"],
                }
            ]
        },
        {
            "difficulty_level": "hard",
            "response_mode": "extractive",
            "analysis_axes": [
                {
                    "axis": "agent_profitability",
                    "objective": "Đánh giá ROA.",
                }
            ],
        },
        user_query="Đánh giá ROA của công ty",
    )

    assert finalized["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "query": "tổng cộng tài sản",
            "needby": ["agent_profitability"],
        }
    ]


def test_comprehensive_financial_assessment_gets_core_evidence_for_all_axes():
    planner_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_axes": [
            {"axis": "agent_profitability", "objective": "Đánh giá sinh lời."},
            {"axis": "agent_liquidity_solvency", "objective": "Đánh giá thanh khoản."},
            {"axis": "agent_cashflow_analysis", "objective": "Đánh giá dòng tiền."},
            {"axis": "agent_efficiency", "objective": "Đánh giá hiệu quả."},
        ],
    }

    finalized = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [], "targets": []},
        planner_plan,
        user_query="Đánh giá tình hình tài chính công ty",
    )
    by_agent = {
        item["agent"]: {
            query["query"]
            for query in item.get("evidence_queries", [])
        }
        for item in finalized["analysis_plan"]
    }

    assert any("giá vốn hàng bán" in item for item in by_agent["agent_efficiency"])
    assert any("hàng tồn kho" in item for item in by_agent["agent_efficiency"])
    assert any("phải thu ngắn hạn" in item for item in by_agent["agent_efficiency"])
    assert any("tổng tài sản ngắn hạn" in item for item in by_agent["agent_liquidity_solvency"])
    assert any("tổng nợ ngắn hạn" in item for item in by_agent["agent_liquidity_solvency"])
    assert any("hoạt động đầu tư" in item for item in by_agent["agent_cashflow_analysis"])
    assert any("hoạt động tài chính" in item for item in by_agent["agent_cashflow_analysis"])
    assert any("trong kỳ" in item for item in by_agent["agent_cashflow_analysis"])
    assert all(by_agent[agent] for agent in by_agent)


def test_generic_financial_assessment_expands_missing_fourth_axis():
    plan, added = planner_runner._expand_broad_profitability_axes(
        {"user_query": "Đánh giá tình hình tài chính công ty"},
        {
            "difficulty_level": "hard",
            "response_mode": "extractive",
            "analysis_axes": [
                {"axis": "agent_profitability", "objective": "Đánh giá sinh lời."},
                {"axis": "agent_cashflow_analysis", "objective": "Đánh giá CFO."},
                {"axis": "agent_efficiency", "objective": "Đánh giá hiệu quả."},
            ],
        },
    )

    assert added == ["agent_liquidity_solvency"]
    assert [axis["axis"] for axis in plan["analysis_axes"]] == [
        "agent_profitability",
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    objectives = {axis["axis"]: axis["objective"] for axis in plan["analysis_axes"]}
    assert "lưu chuyển tiền thuần từ hoạt động đầu tư" in objectives[
        "agent_cashflow_analysis"
    ]
    assert "tổng tài sản ngắn hạn" in objectives["agent_liquidity_solvency"]
    assert "hàng tồn kho" in objectives["agent_efficiency"]


def test_broad_profitability_keeps_an_explicit_additional_metric():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "evidence_plan": [
                {
                    "table": TABLE_IS,
                    "query": "chi phí bán hàng",
                    "needby": ["agent_profitability"],
                }
            ]
        },
        {
            "difficulty_level": "hard",
            "response_mode": "extractive",
            "analysis_axes": [
                {
                    "axis": "agent_profitability",
                    "objective": "Đánh giá khả năng sinh lời và chi phí bán hàng.",
                }
            ],
        },
        user_query="Đánh giá khả năng sinh lời và chi phí bán hàng",
    )

    queries = [
        query
        for item in finalized["evidence_plan"]
        for query in keyworder_runner._evidence_item_queries(item)
    ]
    assert len(queries) == 8
    assert queries.count("chi phí bán hàng") == 1


def test_broad_profitability_core_pairs_reach_each_scoped_analysis_input(
    monkeypatch,
):
    planner_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_axes": [
            {"axis": "agent_profitability", "objective": "Đánh giá sinh lời."},
            {
                "axis": "agent_cashflow_analysis",
                "objective": "Đánh giá chất lượng lợi nhuận.",
            },
            {"axis": "agent_efficiency", "objective": "Đánh giá hiệu quả."},
        ],
    }
    worker_plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [], "targets": []},
        planner_plan,
        user_query="Đánh giá khả năng sinh lời của công ty",
    )
    rows_by_query_prefix = {
        "doanh thu thuần": (TABLE_IS, "Doanh thu thuần về bán hàng và cung cấp dịch vụ"),
        "lợi nhuận gộp": (TABLE_IS, "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ"),
        "lợi nhuận thuần": (TABLE_IS, "Lợi nhuận thuần từ hoạt động kinh doanh"),
        "lợi nhuận sau thuế": (TABLE_IS, "Lợi nhuận sau thuế thu nhập doanh nghiệp"),
        "tổng cộng tài sản": (TABLE_BS, "Tổng tài sản"),
        "tổng vốn chủ sở hữu": (TABLE_BS, "Tổng vốn chủ sở hữu"),
        "lưu chuyển tiền thuần": (TABLE_CF, "Lưu chuyển tiền thuần từ hoạt động kinh doanh"),
    }

    retrieval_queries = []

    def fake_get_related_info(**kwargs):
        query = str(kwargs["query"])
        retrieval_queries.append(query)
        table, metric = next(
            payload
            for prefix, payload in rows_by_query_prefix.items()
            if query.startswith(prefix)
        )
        if table == TABLE_BS:
            periods = [
                ("31/12/2025 VND", "cuối", "current", "200"),
                ("1/1/2025 VND", "đầu", "previous", "180"),
            ]
        else:
            periods = [
                ("2025 VND", "cuối", "current", "120"),
                ("2024 VND", "đầu", "previous", "100"),
            ]
        core_documents = [
            f"{metric} | {label}: {value}"
            for label, _period, _role, value in periods
        ]
        core_metadatas = [
            {
                "heading": table,
                "item_name": f"{metric} | {label}",
                "metric_label": metric,
                "raw_value": value,
                "period": period,
                "period_label": label,
                "period_role": role,
                "source": "report.md",
                "block_id": f"{table}:{metric}:core",
                "fact_id": f"{table}:{metric}:{role}",
                "note_ref": "V.99",
            }
            for label, period, role, value in periods
        ]
        # Each real core retrieval can return the full 10-row cut. These
        # same-metric sibling groups model the near-matches that used to let the
        # first route consume the table cap and evict every later core pair.
        distractor_documents = []
        distractor_metadatas = []
        for group_index in range(1, 5):
            for label, period, role, _value in periods:
                value = str(group_index * 1000 + (1 if role == "current" else 0))
                distractor_documents.append(
                    f"{metric} | Phụ {group_index} | {label}: {value}"
                )
                distractor_metadatas.append(
                    {
                        "heading": table,
                        "item_name": f"{metric} | Phụ {group_index} | {label}",
                        "metric_label": metric,
                        "raw_value": value,
                        "period": period,
                        "period_label": label,
                        "period_role": role,
                        "source": "report.md",
                        "block_id": f"{table}:{metric}:distractor:{group_index}",
                        "fact_id": f"{table}:{metric}:distractor:{group_index}:{role}",
                        "note_ref": "V.99",
                    }
                )
        documents = [*core_documents, *distractor_documents]
        metadatas = [*core_metadatas, *distractor_metadatas]
        return {
            "context": "\n".join(documents),
            "source": "report.md",
            "documents": documents,
            "metadatas": metadatas,
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "dataset_id": "broad-profitability-core-dispatch",
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": planner_plan,
            "worker_plan": worker_plan,
        }
    )
    targets = {
        target["agent"]: target
        for target in updates["analysis_dispatch_targets"]
    }

    def selected(agent, table):
        return [
            (fact["metric_label"], fact["period_role"])
            for fact in targets[agent]["analysis_input_results"][table]["facts"]
        ]

    profitability_is = selected("agent_profitability", TABLE_IS)
    assert len(profitability_is) == 8
    assert {
        role
        for metric, role in profitability_is
        if metric.startswith("Doanh thu thuần")
    } == {"current", "previous"}
    assert {
        role
        for metric, role in profitability_is
        if metric.startswith("Lợi nhuận sau thuế")
    } == {"current", "previous"}
    assert selected("agent_efficiency", TABLE_IS) == [
        ("Doanh thu thuần về bán hàng và cung cấp dịch vụ", "current"),
        ("Doanh thu thuần về bán hàng và cung cấp dịch vụ", "previous"),
    ]
    assert selected("agent_cashflow_analysis", TABLE_IS) == [
        ("Lợi nhuận sau thuế thu nhập doanh nghiệp", "current"),
        ("Lợi nhuận sau thuế thu nhập doanh nghiệp", "previous"),
    ]
    assert {
        role
        for _metric, role in selected("agent_efficiency", TABLE_BS)
    } == {"current", "previous"}
    assert selected("agent_cashflow_analysis", TABLE_CF) == [
        ("Lưu chuyển tiền thuần từ hoạt động kinh doanh", "current"),
        ("Lưu chuyển tiền thuần từ hoạt động kinh doanh", "previous"),
    ]
    assert updates["evidence_pack"]["stats"]["items_n"] == 7
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 7
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 0
    assert len(updates["evidence_ledger"]["entries"]) == 7
    assert all(
        entry["requirement_state"]["after_retry"] == "matched"
        and len(entry["selected_facts"]) == 10
        for entry in updates["evidence_ledger"]["entries"]
    )
    assert len(retrieval_queries) == 7
    assert not any(query.startswith("thuyết minh ") for query in retrieval_queries)
    for dispatch_target in updates["analysis_dispatch_targets"]:
        analysis_state = {
            "dataset_id": "broad-profitability-core-dispatch",
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": planner_plan,
            "worker_plan": worker_plan,
            **updates,
            "dispatch_target": dispatch_target,
        }
        assert agent_runner._missing_requirements_after_evidence_check(
            analysis_state,
            dispatch_target["agent"],
        ) == []


def test_attached_vnd_pat_pair_closes_evidence_and_analysis_requirement(monkeypatch):
    requirement = (
        "lợi nhuận sau thuế thu nhập doanh nghiệp năm nay và năm trước"
    )
    metric = "Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52)"
    rows = [
        (
            f"{metric} | 2025VND: 9.359.349.635.629",
            {
                "heading": TABLE_IS,
                "company": "Công ty Cổ phần Sữa Việt Nam",
                "fiscal_year": "2025",
                "block_id": "income-statement",
                "fact_id": "is-60-current",
                "item_code": "60",
                "item_name": f"{metric} | 2025VND",
                "row_label": metric,
                "column_label": "2025VND",
                "metric_label": metric,
                "period_role": "current",
                "period_label": "2025VND",
                "unit": "VND",
                "value_kind": "amount",
                "raw_value": "9.359.349.635.629",
                "normalized_value": "9359349635629",
                "source": "suavietnam.md",
            },
        ),
        (
            f"{metric} | 2024VND: 9.262.413.822.949",
            {
                "heading": TABLE_IS,
                "company": "Công ty Cổ phần Sữa Việt Nam",
                "fiscal_year": "2025",
                "block_id": "income-statement",
                "fact_id": "is-60-previous",
                "item_code": "60",
                "item_name": f"{metric} | 2024VND",
                "row_label": metric,
                "column_label": "2024VND",
                "metric_label": metric,
                "period_role": "previous",
                "period_label": "2024VND",
                "unit": "VND",
                "value_kind": "amount",
                "raw_value": "9.262.413.822.949",
                "normalized_value": "9262413822949",
                "source": "suavietnam.md",
            },
        ),
    ]

    class ProductionShapeCollection:
        name = "suavietnam-regression"
        generation = "attached-vnd-pat"

        def __init__(self):
            self.get_calls = []
            self.query_calls = 0

        def get(self, where=None, include=None):
            where = dict(where or {})
            self.get_calls.append(where)
            selected = [
                (document, metadata)
                for document, metadata in rows
                if all(
                    str(metadata.get(key, "") or "") == str(value)
                    for key, value in where.items()
                )
            ]
            return {
                "documents": [document for document, _metadata in selected],
                "metadatas": [metadata for _document, metadata in selected],
            }

        def query(self, query_embeddings, n_results, where=None):
            self.query_calls += 1
            selected = [
                (document, metadata)
                for document, metadata in rows
                if all(
                    str(metadata.get(key, "") or "") == str(value)
                    for key, value in dict(where or {}).items()
                )
            ]
            return {
                "documents": [[document for document, _metadata in selected]],
                "metadatas": [[metadata for _document, metadata in selected]],
            }

    collection = ProductionShapeCollection()
    monkeypatch.setattr(evidence_node, "get_collection", lambda: collection)
    monkeypatch.setattr(tools_module, "embed_query_text", lambda _query: [0.0])

    worker_plan = {
        "difficulty_level": "hard",
        "evidence_plan": [
            {
                "table": TABLE_IS,
                "query": requirement,
                "period_role": "both",
                "needby": ["agent_profitability"],
            }
        ],
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "Đánh giá khả năng sinh lời.",
                "requirements": [requirement],
                "evidence_queries": [
                    {"table": TABLE_IS, "query": requirement}
                ],
            }
        ],
    }
    state = {
        "dataset_id": "suavietnam-attached-vnd-pat-regression",
        "user_query": "Đánh giá khả năng sinh lời của công ty",
        "planner_plan": {"difficulty_level": "hard"},
        "worker_plan": worker_plan,
    }

    updates = evidence_node.build_evidence_pack(state)

    assert collection.query_calls == 0
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 1
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 0
    assert updates["evidence_pack"]["items"][0]["retrieval_status"] == "matched"
    ledger_entry = updates["evidence_ledger"]["entries"][0]
    assert ledger_entry["requirement_state"] == {
        "before_retry": "matched",
        "after_retry": "matched",
    }
    assert ledger_entry["targeted_retry"]["performed"] is False
    assert {
        fact["period_role"]
        for fact in updates["ragas_facts_by_table"][TABLE_IS]["facts"]
        if fact.get("item_code") == "60"
    } == {"current", "previous"}

    dispatch_target = updates["analysis_dispatch_targets"][0]
    analysis_state = {
        **state,
        **updates,
        "dispatch_target": dispatch_target,
    }
    assert agent_runner._missing_requirements_after_evidence_check(
        analysis_state,
        "agent_profitability",
    ) == []


def test_router_finalize_drops_optional_selling_expense_when_not_requested():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "targets": [
                {
                    "table": TABLE_IS,
                    "requirements": ["chi phí bán hàng"],
                }
            ]
        },
        {"difficulty_level": "medium", "analysis_axes": []},
        user_query="Đánh giá khả năng sinh lời năm 2024",
    )

    assert finalized["targets"] == []


def test_router_finalize_keeps_optional_selling_expense_when_requested():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "targets": [
                {
                    "table": TABLE_IS,
                    "requirements": ["chi phí bán hàng"],
                }
            ]
        },
        {"difficulty_level": "medium", "analysis_axes": []},
        user_query="Phân tích chi phí bán hàng năm 2024",
    )

    assert finalized["evidence_plan"] == [
        {
            "table": TABLE_IS,
            "query": "chi phí bán hàng",
            "needby": [],
        }
    ]
    assert finalized["targets"] == []


def test_router_finalize_groups_evidence_plan_by_table_and_needby():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "targets": [
                {
                    "table": TABLE_IS,
                    "requirements": [
                        "doanh thu bán hàng và cung cấp dịch vụ",
                        "chi phí tài chính",
                    ],
                },
                {
                    "table": TABLE_BS,
                    "requirements": ["tổng tài sản"],
                },
            ]
        },
        {
            "difficulty_level": "hard",
            "analysis_axes": [
                {
                    "axis": "agent_profitability",
                    "objective": "Phân tích khả năng sinh lời.",
                }
            ],
        },
        user_query="Phân tích các khoản mục đã chọn",
    )

    assert finalized["evidence_plan"] == [
        {
            "table": TABLE_IS,
            "needby": ["agent_profitability"],
            "queries": [
                "doanh thu bán hàng và cung cấp dịch vụ",
                "chi phí tài chính",
            ],
        },
        {
            "table": TABLE_BS,
            "needby": ["agent_profitability"],
            "query": "tổng cộng tài sản",
        },
    ]
    assert finalized["analysis_plan"][0]["evidence_queries"] == [
        {"table": TABLE_IS, "query": "doanh thu bán hàng và cung cấp dịch vụ"},
        {"table": TABLE_IS, "query": "chi phí tài chính"},
        {"table": TABLE_BS, "query": "tổng cộng tài sản"},
    ]

    trace_plan = keyworder_runner._router_trace_evidence_plan(finalized)
    assert trace_plan[0]["queries_n"] == 2
    assert trace_plan[0]["queries"] == [
        "doanh thu bán hàng và cung cấp dịch vụ",
        "chi phí tài chính",
    ]


def test_router_resolves_allowed_income_statement_keyword_when_table_is_missing():
    finalized = keyworder_runner._finalize_router_targets(
        {
            "evidence_plan": [
                {
                    "query": "lợi nhuận sau thuế thu nhập doanh nghiệp",
                    "needby": ["agent_profitability"],
                }
            ]
        },
        {
            "difficulty_level": "medium",
            "need_web": False,
            "analysis_axes": [],
        },
        user_query="Lợi nhuận sau thuế là bao nhiêu?",
    )

    assert finalized["evidence_plan"] == [
        {
            "table": TABLE_IS,
            "query": "lợi nhuận sau thuế thu nhập doanh nghiệp",
            "needby": ["agent_profitability"],
        }
    ]
    assert finalized["analysis_plan"] == []


def test_build_evidence_skips_web_when_router_did_not_enable_web():
    assert not hasattr(tools_module, "web_search")

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": "",
                        "query": "giá cổ phiếu hiện tại",
                    }
                ],
                "need_web": False,
            },
            "user_query": "Giá cổ phiếu hiện tại là bao nhiêu?",
        }
    )

    assert updates["evidence_pack"]["targets"] == []
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 0


def test_build_evidence_marks_web_unsupported_without_promoting_placeholder():
    assert not hasattr(tools_module, "web_search")

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": "",
                        "query": "tin tức thị trường",
                    }
                ],
                "need_web": True,
            },
            "user_query": "Cập nhật tin tức thị trường",
        }
    )

    assert updates["evidence_pack"]["targets"] == [
        {
            "mode": "web",
            "requirements": ["tin tức thị trường"],
        }
    ]
    fact = updates["worker_results"]["WEB"]["facts"][0]
    assert fact.get("value", "") == ""
    assert fact["status"] == "not_found_after_search"
    assert fact["retrieval_status"] == "unsupported"
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 0
    assert updates["evidence_pack"]["stats"]["web_unsupported_n"] == 1
    assert any(log["event"] == "evidence_tool:unsupported" for log in updates["trace"])


def test_build_evidence_expands_grouped_evidence_plan_queries(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        query = kwargs["query"]
        return {
            "context": f"{query}: 100",
            "source": "report.md",
            "documents": [f"{query}: 100"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": f"{query} | 2024",
                    "raw_value": "100",
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "queries": [
                            "chi phí tài chính",
                            "doanh thu bán hàng và cung cấp dịch vụ",
                        ],
                        "needby": ["agent_profitability"],
                    }
                ],
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": "Phân tích doanh thu và chi phí tài chính.",
                    }
                ],
            },
            "dataset_id": "test-dataset-grouped-evidence-plan",
            "user_query": "Phân tích doanh thu và chi phí tài chính",
        }
    )

    # Backend start/completion order is scheduler-dependent; the merged graph
    # outputs below remain in deterministic plan order.
    assert {call["query"] for call in calls} == {
        "chi phí tài chính",
        "doanh thu bán hàng và cung cấp dịch vụ",
    }
    assert updates["evidence_pack"]["targets"][0]["requirements"] == [
        "chi phí tài chính",
        "doanh thu bán hàng và cung cấp dịch vụ",
    ]
    assert updates["analysis_dispatch_targets"][0]["evidence_queries"] == [
        {"table": TABLE_IS, "query": "chi phí tài chính"},
        {"table": TABLE_IS, "query": "doanh thu bán hàng và cung cấp dịch vụ"},
    ]


def test_build_evidence_retries_one_targeted_query_after_topk_miss(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            item_name = "Chi phí khác"
            value = "10"
        else:
            item_name = "Chi phí bán hàng"
            value = "20"
        return {
            "context": f"{item_name}: {value}",
            "source": "report.md",
            "documents": [f"{item_name}: {value}"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": item_name,
                    "raw_value": value,
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {"table": TABLE_IS, "query": "chi phí bán hàng"}
                ],
            },
            "dataset_id": "targeted-retry-single-attempt",
            "user_query": "Chi phí bán hàng là bao nhiêu?",
        }
    )

    assert len(calls) == 2
    assert calls[1]["strict_table"] is True
    assert calls[1]["cross_table"] is False
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 1
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 2
    fact = updates["worker_results"][TABLE_IS]["facts"][0]
    assert fact["item_name"] == "Chi phí bán hàng"
    assert fact["value"] == "20"
    assert any(
        log["event"] == "evidence_tool:targeted_retry_done"
        and log["retrieval_status"] == "matched"
        for log in updates["trace"]
    )


def test_build_evidence_uses_operand_scoped_intent_for_ratio_plan(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        query = kwargs["query"]
        item_name = (
            "Tổng cộng tài sản"
            if query == "tổng tài sản"
            else "Hàng tồn kho"
        )
        return {
            "context": f"{item_name}: 100",
            "source": "report.md",
            "documents": [f"{item_name}: 100"],
            "metadatas": [
                {
                    "heading": TABLE_BS,
                    "item_name": item_name,
                    "raw_value": "100",
                    "source": "report.md",
                }
            ],
        }

    query = "Tỷ trọng hàng tồn kho trên tổng tài sản?"
    worker_plan = keyworder_runner._finalize_router_targets(
        keyworder_runner._sanitize_router_plan_payload(
            {"evidence_plan": [{"table": TABLE_BS, "query": query}]}
        ),
        {"difficulty_level": "medium"},
        user_query=query,
    )
    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": worker_plan,
            "dataset_id": "test-ratio-operand-scoped-intent",
            "user_query": query,
        }
    )

    assert [(call["query"], call["intent"]) for call in calls] == [
        ("hàng tồn kho", "hàng tồn kho"),
        ("tổng tài sản", "tổng tài sản"),
    ]
    facts = updates["worker_results"][TABLE_BS]["facts"]
    assert {
        (fact["item_name"], fact["operand_role"])
        for fact in facts
    } == {
        ("Hàng tồn kho", "numerator"),
        ("Tổng cộng tài sản", "denominator"),
    }


def test_analysis_input_results_fallback_respects_fact_needby():
    state = {
        "worker_plan": {
            "evidence_plan": [
                {
                    "table": TABLE_IS,
                    "query": "doanh thu bán hàng và cung cấp dịch vụ",
                    "needby": ["agent_profitability"],
                },
                {
                    "table": TABLE_IS,
                    "query": "chi phí bán hàng",
                    "needby": ["agent_efficiency"],
                },
            ]
        },
        "worker_results": {
            TABLE_IS: {
                "table": TABLE_IS,
                "facts": [
                    {
                        "table": TABLE_IS,
                        "item_name": "Doanh thu bán hàng và cung cấp dịch vụ",
                        "value": "100",
                        "needby": ["agent_profitability"],
                    },
                    {
                        "table": TABLE_IS,
                        "item_name": "Chi phí bán hàng",
                        "value": "10",
                        "needby": ["agent_efficiency"],
                    },
                ],
            }
        },
    }
    target = {
        "agent": "agent_profitability",
        "objective": "Đánh giá khả năng sinh lời.",
        "evidence_queries": [{"table": TABLE_BS, "query": "tổng cộng tài sản"}],
    }

    results = dispatch_nodes._analysis_input_results_for_target(state, target)

    assert [
        fact["item_name"]
        for fact in results[TABLE_IS]["facts"]
    ] == ["Doanh thu bán hàng và cung cấp dịch vụ"]


def test_build_evidence_routes_analysis_facts_by_needby(monkeypatch):
    def fake_get_related_info(**kwargs):
        query = kwargs["query"]
        if query == "doanh thu bán hàng và cung cấp dịch vụ":
            item_name = "Doanh thu bán hàng và cung cấp dịch vụ | 2024"
            value = "100"
        else:
            item_name = "Chi phí bán hàng | 2024"
            value = "10"
        return {
            "context": f"{item_name}: {value}",
            "source": "report.md",
            "documents": [f"{item_name}: {value}"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": item_name,
                    "raw_value": value,
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "doanh thu bán hàng và cung cấp dịch vụ",
                        "needby": ["agent_profitability"],
                    },
                    {
                        "table": TABLE_IS,
                        "query": "chi phí bán hàng",
                        "needby": ["agent_efficiency"],
                    },
                ],
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": "Đánh giá doanh thu.",
                        "evidence_queries": [
                            {
                                "table": TABLE_IS,
                                "query": "doanh thu bán hàng và cung cấp dịch vụ",
                            }
                        ],
                    },
                    {
                        "agent": "agent_efficiency",
                        "objective": "Đánh giá chi phí bán hàng.",
                        "evidence_queries": [
                            {
                                "table": TABLE_IS,
                                "query": "chi phí bán hàng",
                            }
                        ],
                    },
                ],
            },
            "dataset_id": "test-dataset-needby-scoped-facts",
            "user_query": "Đánh giá doanh thu và chi phí bán hàng",
        }
    )

    targets = {
        target["agent"]: target
        for target in updates["analysis_dispatch_targets"]
    }

    assert [
        fact["item_name"]
        for fact in targets["agent_profitability"]["analysis_input_results"][TABLE_IS]["facts"]
    ] == ["Doanh thu bán hàng và cung cấp dịch vụ | 2024"]
    assert [
        fact["item_name"]
        for fact in targets["agent_efficiency"]["analysis_input_results"][TABLE_IS]["facts"]
    ] == ["Chi phí bán hàng | 2024"]
    assert len(updates["worker_results"][TABLE_IS]["facts"]) == 2


def test_build_evidence_compacts_fact_payload_for_analysis_tokens(monkeypatch):
    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        lambda **_kwargs: {
            "context": "Doanh thu bán hàng và cung cấp dịch vụ: " + ("100 " * 120),
            "source": "report.md",
            "documents": ["Doanh thu bán hàng và cung cấp dịch vụ: " + ("100 " * 120)],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Doanh thu bán hàng và cung cấp dịch vụ | 2024",
                    "raw_value": "100 " * 120,
                    "normalized_value": "100",
                    "item_code": "01",
                    "source": "report.md",
                }
            ],
        },
    )

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "doanh thu bán hàng và cung cấp dịch vụ",
                    }
                ]
            },
            "dataset_id": "test-dataset",
        }
    )

    fact = updates["evidence_pack"]["facts_by_table"][TABLE_IS]["facts"][0]

    assert "raw_value" not in fact
    assert "normalized_value" not in fact
    assert "item_code" not in fact
    assert len(fact["value"]) < 230


def test_build_evidence_fetches_note_ref_context_for_hard_analysis(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if kwargs["table"] == TABLE_NOTE:
            return {
                "context": "Thuyết minh 23: Chi phí tài chính - Lãi tiền vay: 6.677.078.068",
                "source": "report.md#page=23",
                "documents": [
                    "Thuyết minh 23: Chi phí tài chính | Lãi tiền vay: 6.677.078.068"
                ],
                "metadatas": [
                    {
                        "heading": TABLE_NOTE,
                        "item_name": "Thuyết minh 23: Chi phí tài chính | Lãi tiền vay",
                        "raw_value": "Năm 2024 VND: 6.677.078.068",
                        "source": "report.md#page=23",
                    }
                ],
            }
        return {
            "context": "Chi phí tài chính | Năm 2024VND: 6.677.078.068",
            "source": "report.md",
            "documents": ["Chi phí tài chính | Năm 2024VND: 6.677.078.068"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí tài chính | Năm 2024VND",
                    "raw_value": "6.677.078.068",
                    "note_ref": "23",
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "chi phí tài chính",
                        "needby": ["agent_profitability"],
                    }
                ],
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": "Phân tích tác động của chi phí tài chính đến lợi nhuận.",
                        "evidence_queries": [
                            {
                                "table": TABLE_IS,
                                "query": "chi phí tài chính",
                            }
                        ],
                    }
                ],
            },
            "dataset_id": "test-dataset",
            "user_query": "Phân tích chi phí tài chính",
        }
    )

    note_calls = [call for call in calls if call["table"] == TABLE_NOTE]
    assert len(note_calls) == 1
    assert note_calls[0]["query"] == "thuyết minh 23 Chi phí tài chính"
    assert note_calls[0]["strict_table"] is True
    assert TABLE_NOTE in updates["worker_results"]
    assert updates["worker_results"][TABLE_NOTE]["facts"][0]["item_name"] == (
        "Thuyết minh 23: Chi phí tài chính | Lãi tiền vay"
    )
    assert any(
        item.get("scope") == "note_ref"
        for item in updates["evidence_pack"]["items"]
    )
    assert TABLE_NOTE in updates["analysis_dispatch_targets"][0]["analysis_input_results"]


def test_build_evidence_skips_note_ref_context_for_easy_and_medium(monkeypatch):
    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if kwargs["table"] == TABLE_NOTE:
            return {
                "context": "Thuyết minh 23: Chi phí tài chính",
                "source": "report.md#page=23",
                "documents": ["Thuyết minh 23: Chi phí tài chính"],
                "metadatas": [
                    {
                        "heading": TABLE_NOTE,
                        "item_name": "Thuyết minh 23: Chi phí tài chính",
                        "raw_value": "Chi tiết chi phí tài chính",
                        "source": "report.md#page=23",
                    }
                ],
            }
        return {
            "context": "Chi phí tài chính | Năm 2024VND: 6.677.078.068",
            "source": "report.md",
            "documents": ["Chi phí tài chính | Năm 2024VND: 6.677.078.068"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí tài chính | Năm 2024VND",
                    "raw_value": "6.677.078.068",
                    "note_ref": "23",
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    for difficulty in ("easy", "medium"):
        calls = []
        updates = evidence_node.build_evidence_pack(
            {
                "planner_plan": {"difficulty_level": difficulty},
                "worker_plan": {
                    "evidence_plan": [
                        {
                            "table": TABLE_IS,
                            "query": "chi phí tài chính",
                            "needby": ["agent_profitability"],
                        }
                    ],
                    "analysis_plan": [
                        {
                            "agent": "agent_profitability",
                            "objective": "Phân tích chi phí tài chính.",
                        }
                    ],
                },
                "dataset_id": f"test-dataset-note-ref-skip-{difficulty}",
                "user_query": "Chi phí tài chính là bao nhiêu?",
            }
        )

        assert [call["table"] for call in calls] == [TABLE_IS]
        assert TABLE_NOTE not in updates["worker_results"]
        assert not any(
            item.get("scope") == "note_ref"
            for item in updates["evidence_pack"]["items"]
        )
        assert updates["dispatch_phase"] == "synth"
        assert updates["collect_decision"] == "synth"


def test_build_evidence_fetches_note_ref_context_for_hard_without_analysis_plan(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if kwargs["table"] == TABLE_NOTE:
            return {
                "context": "Thuyết minh 23: Chi phí tài chính - Lãi tiền vay: 6.677.078.068",
                "source": "report.md#page=23",
                "documents": [
                    "Thuyết minh 23: Chi phí tài chính | Lãi tiền vay: 6.677.078.068"
                ],
                "metadatas": [
                    {
                        "heading": TABLE_NOTE,
                        "item_name": "Thuyết minh 23: Chi phí tài chính | Lãi tiền vay",
                        "raw_value": "Năm 2024 VND: 6.677.078.068",
                        "source": "report.md#page=23",
                    }
                ],
            }
        return {
            "context": "Chi phí tài chính | Năm 2024VND: 6.677.078.068",
            "source": "report.md",
            "documents": ["Chi phí tài chính | Năm 2024VND: 6.677.078.068"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí tài chính | Năm 2024VND",
                    "raw_value": "6.677.078.068",
                    "note_ref": "23",
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "planner_plan": {"difficulty_level": "hard"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "chi phí tài chính",
                    }
                ],
                "analysis_plan": [],
            },
            "dataset_id": "test-dataset-hard-note-ref-without-analysis-plan",
            "user_query": "Phân tích chi phí tài chính",
        }
    )

    note_calls = [call for call in calls if call["table"] == TABLE_NOTE]
    assert len(note_calls) == 1
    assert note_calls[0]["query"] == "thuyết minh 23 Chi phí tài chính"
    assert TABLE_NOTE in updates["worker_results"]
    assert any(
        item.get("scope") == "note_ref"
        for item in updates["evidence_pack"]["items"]
    )
    assert updates["dispatch_phase"] == "synth"


def test_build_evidence_keeps_router_selected_note_for_easy(monkeypatch):
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        return {
            "context": "Thuyết minh 23: Chi phí tài chính",
            "source": "report.md#page=23",
            "documents": ["Thuyết minh 23: Chi phí tài chính"],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Thuyết minh 23: Chi phí tài chính",
                    "raw_value": "Chi tiết chi phí tài chính",
                    "source": "report.md#page=23",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "planner_plan": {"difficulty_level": "easy"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_NOTE,
                        "query": "thuyết minh 23 chi phí tài chính",
                    }
                ],
            },
            "dataset_id": "test-dataset-router-selected-note-easy",
            "user_query": "Thuyết minh 23 chi phí tài chính là gì?",
        }
    )

    assert [call["table"] for call in calls] == [TABLE_NOTE]
    assert calls[0]["strict_table"] is True
    assert TABLE_NOTE in updates["worker_results"]
    assert updates["worker_results"][TABLE_NOTE]["facts"][0]["item_name"] == (
        "Thuyết minh 23: Chi phí tài chính"
    )


def test_build_evidence_limits_note_facts_sent_to_llm(monkeypatch):
    calls = []

    def note_result(note_number, title):
        docs = [
            f"Thuyết minh {note_number}: {title} | dòng {idx}: nội dung {idx}"
            for idx in range(1, 5)
        ]
        return {
            "context": "\n".join(docs),
            "source": "report.md",
            "documents": docs,
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": f"Thuyết minh {note_number}: {title} | dòng {idx}",
                    "raw_value": f"nội dung {idx}",
                    "source": "report.md",
                }
                for idx in range(1, 5)
            ],
        }

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if kwargs["table"] == TABLE_NOTE:
            if "23" in kwargs["query"]:
                return note_result("23", "Chi phí tài chính")
            return note_result("20", "Doanh thu bán hàng và cung cấp dịch vụ")
        if kwargs["query"] == "chi phí tài chính":
            return {
                "context": "Chi phí tài chính | Năm 2024VND: 6.677.078.068",
                "source": "report.md",
                "documents": ["Chi phí tài chính | Năm 2024VND: 6.677.078.068"],
                "metadatas": [
                    {
                        "heading": TABLE_IS,
                        "item_name": "Chi phí tài chính | Năm 2024VND",
                        "raw_value": "6.677.078.068",
                        "note_ref": "23",
                        "source": "report.md",
                    }
                ],
            }
        return {
            "context": "Doanh thu bán hàng và cung cấp dịch vụ | Năm 2024VND: 36.099.274.547",
            "source": "report.md",
            "documents": ["Doanh thu bán hàng và cung cấp dịch vụ | Năm 2024VND: 36.099.274.547"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Doanh thu bán hàng và cung cấp dịch vụ | Năm 2024VND",
                    "raw_value": "36.099.274.547",
                    "note_ref": "20",
                    "source": "report.md",
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "chi phí tài chính",
                        "needby": ["agent_profitability"],
                    },
                    {
                        "table": TABLE_IS,
                        "query": "doanh thu bán hàng và cung cấp dịch vụ",
                        "needby": ["agent_profitability"],
                    },
                ],
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": "Phân tích doanh thu và chi phí tài chính.",
                        "evidence_queries": [
                            {"table": TABLE_IS, "query": "chi phí tài chính"},
                            {"table": TABLE_IS, "query": "doanh thu bán hàng và cung cấp dịch vụ"},
                        ],
                    }
                ],
            },
            "dataset_id": "test-dataset-note-limit",
            "user_query": "Phân tích doanh thu và chi phí tài chính",
        }
    )

    assert len([call for call in calls if call["table"] == TABLE_NOTE]) == 2
    assert len(updates["worker_results"][TABLE_NOTE]["facts"]) == 8
    assert len(updates["evidence_pack"]["facts_by_table"][TABLE_NOTE]["facts"]) == 8
    assert sum(
        len(item.get("facts_preview", []) or [])
        for item in updates["evidence_pack"]["items"]
        if item.get("table") == TABLE_NOTE
    ) == 8
    analysis_note_facts = updates["analysis_dispatch_targets"][0]["analysis_input_results"][TABLE_NOTE]["facts"]
    assert len(analysis_note_facts) == 8
    note_cache_items = [
        item
        for item in updates["evidence_cache"].values()
        if item.get("table") == TABLE_NOTE
    ]
    assert len(note_cache_items) == 2
    assert all(len(item.get("facts", []) or []) == 4 for item in note_cache_items)


def test_twelve_note_facts_reach_worker_pack_and_analysis_llm(monkeypatch):
    docs = [f"Khoản mục thuyết minh | dòng {idx}: {idx}" for idx in range(15)]
    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        lambda **_kwargs: {
            "context": "\n".join(docs),
            "source": "report.md#page=42",
            "documents": docs,
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": f"Khoản mục thuyết minh | dòng {idx}",
                    "raw_value": str(idx),
                    "period": "Năm 2024",
                    "unit": "VND",
                    "value_type": "Số cuối kỳ",
                    "source": "report.md#page=42",
                }
                for idx in range(15)
            ],
        },
    )

    updates = evidence_node.build_evidence_pack(
        {
            "planner_plan": {
                "difficulty_level": "hard",
                "time_hint": "Năm 2024",
            },
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_NOTE,
                        "query": "khoản mục thuyết minh",
                        "needby": ["agent_profitability"],
                        "period": "Năm 2024",
                        "unit": "VND",
                        "value_type": "Số cuối kỳ",
                    }
                ],
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": "Phân tích khoản mục thuyết minh.",
                        "evidence_queries": [
                            {"table": TABLE_NOTE, "query": "khoản mục thuyết minh"}
                        ],
                    }
                ],
            },
            "dataset_id": "test-note-twelve-facts-contract",
            "user_query": "Phân tích dữ liệu được giao",
        }
    )

    worker_facts = updates["worker_results"][TABLE_NOTE]["facts"]
    pack_facts = updates["evidence_pack"]["facts_by_table"][TABLE_NOTE]["facts"]
    dispatch_target = updates["analysis_dispatch_targets"][0]
    llm_facts = dispatch_target["analysis_input_results"][TABLE_NOTE]["facts"]

    assert len(worker_facts) == len(pack_facts) == len(llm_facts) == 12
    assert dispatch_target["time_hint"] == "Năm 2024"
    assert llm_facts[-1]["value"] == "11"
    assert llm_facts[0]["time_hint"] == "Năm 2024"
    assert llm_facts[0]["period"] == "Năm 2024"
    assert llm_facts[0]["unit"] == "VND"
    assert llm_facts[0]["value_type"] == "Số cuối kỳ"
    assert llm_facts[0]["evidence_query"] == "khoản mục thuyết minh"
    assert llm_facts[0]["source"] == "report.md#page=42"


def test_grounded_cap_can_complete_atoms_from_a_sibling_query():
    premise = "sự kiện cổ phần hóa và đăng ký công ty cổ phần"
    selected = evidence_node._select_grounded_premise_facts(
        [
            {
                "fact_id": "registration",
                "value": (
                    "Ngày 20/11/2003: Công ty đăng ký trở thành một "
                    "công ty cổ phần."
                ),
                "evidence_query": premise,
                "source": "report.md#page=12",
            },
            {
                "fact_id": "full-timeline",
                "value": (
                    "Ngày 01/10/2003: Công ty được cổ phần hoá. "
                    "Ngày 20/11/2003: Công ty đăng ký trở thành một "
                    "công ty cổ phần."
                ),
                "evidence_query": "quá trình hình thành và phát triển",
                "source": "report.md#page=12",
            },
        ],
        premises=[premise],
        limit=2,
    )

    assert [fact["fact_id"] for fact in selected] == [
        "registration",
        "full-timeline",
    ]


def test_grounded_cap_does_not_treat_listing_policy_as_listing_event():
    premise = "sự kiện cấp phép và niêm yết cổ phiếu"
    selected = evidence_node._select_grounded_premise_facts(
        [
            {
                "fact_id": "valuation-policy",
                "item_name": premise,
                "value": (
                    "Đối với chứng khoán niêm yết, giá đóng cửa được dùng "
                    "để xác định giá trị hợp lý."
                ),
                "evidence_query": premise,
                "source": "report.md#page=18",
            },
            {
                "fact_id": "listing-event",
                "value": (
                    "Ngày 19/1/2006: Cổ phiếu của Công ty được cấp phép và "
                    "niêm yết trên Sở Giao dịch Chứng khoán."
                ),
                "evidence_query": "lịch sử doanh nghiệp",
                "source": "report.md#page=12",
            },
        ],
        premises=[premise],
        limit=1,
    )

    assert [fact["fact_id"] for fact in selected] == ["listing-event"]


def test_extractive_table_cap_keeps_original_rank_order():
    facts = [
        {
            "fact_id": f"fact-{index}",
            "item_name": f"Dòng {index}",
            "value": str(index),
        }
        for index in range(15)
    ]

    limited = evidence_node._limit_note_facts_for_llm(
        {TABLE_NOTE: {"table": TABLE_NOTE, "facts": facts}},
        state={
            "planner_plan": {
                "response_mode": "extractive",
                "premise_requirements": [
                    "sự kiện cấp phép và niêm yết cổ phiếu"
                ],
            }
        },
        worker_plan={"response_mode": "extractive"},
    )

    assert [
        fact["fact_id"]
        for fact in limited[TABLE_NOTE]["facts"]
    ] == [f"fact-{index}" for index in range(12)]


def test_grounded_evidence_cap_reserves_actual_fact_for_each_premise(
    monkeypatch,
):
    premises = [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    narrative_query = (
        "Ý nghĩa của việc Công ty chuyển đổi từ doanh nghiệp nhà nước sang "
        "công ty cổ phần niêm yết đối với quản trị doanh nghiệp là gì?"
    )
    retrieval_intents = []

    def fake_get_related_info(*, query, table, **_kwargs):
        retrieval_intents.append(
            (
                query,
                str(_kwargs.get("intent", "") or ""),
                _kwargs.get("structured_slots"),
            )
        )
        if "doanh nghiệp nhà nước" in query:
            event_value = (
                "Ngày 29/4/1993: Công ty được thành lập theo loại hình "
                "Doanh nghiệp Nhà Nước."
            )
            event_id = f"{table}-state-owned"
        elif "cổ phần hóa" in query:
            # Keep the source spelling that previously failed atomization.
            event_value = (
                "Ngày 01/10/2003: Công ty được cổ phần hoá từ Doanh nghiệp "
                "Nhà Nước. Ngày 20/11/2003: Công ty đăng ký trở thành một "
                "công ty cổ phần."
            )
            event_id = f"{table}-corporatization"
        else:
            event_value = (
                "Ngày 19/1/2006: Cổ phiếu của Công ty được cấp phép và "
                "niêm yết trên Sở Giao dịch Chứng khoán."
            )
            event_id = f"{table}-listing"

        rows = [
            {
                "heading": table,
                "item_name": "Lịch sử hình thành và phát triển",
                "raw_value": event_value,
                "fact_id": event_id,
                "source": "report.md#page=12",
            },
            *[
                {
                    "heading": table,
                    "item_name": f"Dòng nhiễu {index}",
                    "raw_value": (
                        f"{event_value} Chi tiết tham chiếu {index}."
                    ),
                    "fact_id": f"{table}-{event_id}-noise-{index}",
                    "source": "report.md#page=40",
                }
                for index in range(11)
            ],
        ]
        return {
            "context": "\n".join(
                f"{row['item_name']}: {row['raw_value']}"
                for row in rows
            ),
            "source": "report.md",
            "documents": [
                f"{row['item_name']}: {row['raw_value']}"
                for row in rows
            ],
            "metadatas": rows,
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    # Listing deliberately runs first and consumes a full per-query result
    # window. A positional table cap would therefore discard the later two
    # premise payloads.
    evidence_plan = [
        {"table": table, "query": premise, "needby": []}
        for table in (TABLE_NOTE, TABLE_REPORT_SECTION)
        for premise in (premises[2], premises[0], premises[1])
    ]
    state = {
        "dataset_id": "grounded-premise-cap-contract",
        "user_query": narrative_query,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
        },
        "worker_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
            "evidence_plan": evidence_plan,
            "analysis_plan": [],
        },
    }

    updates = evidence_node.build_evidence_pack(state)
    worker_results = updates["worker_results"]

    assert set(worker_results) == {TABLE_NOTE, TABLE_REPORT_SECTION}
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 0
    assert all(
        entry["requirement_state"]["after_retry"] == "matched"
        for entry in updates["evidence_ledger"]["entries"]
    )
    assert len(worker_results[TABLE_NOTE]["facts"]) == 12
    assert len(worker_results[TABLE_REPORT_SECTION]["facts"]) == 10
    for table in (TABLE_NOTE, TABLE_REPORT_SECTION):
        fact_ids = {
            fact.get("fact_id")
            for fact in worker_results[table]["facts"]
        }
        assert {
            f"{table}-state-owned",
            f"{table}-corporatization",
            f"{table}-listing",
        }.issubset(fact_ids)

        preview_values = " ".join(
            str(preview.get("value", "") or "")
            for item in updates["evidence_pack"]["items"]
            if item.get("table") == table
            for preview in item.get("facts_preview", [])
        )
        assert "Doanh nghiệp Nhà Nước" in preview_values
        assert "cổ phần hoá" in preview_values
        assert "niêm yết" in preview_values

    bindings, missing = synth_runner._grounded_premise_bindings(
        state,
        worker_results,
    )
    assert missing == []
    assert set(bindings) == set(premises)
    assert all(
        intent == query and structured_slots is False
        for query, intent, structured_slots in retrieval_intents
        if query in premises
    )


def test_analysis_dispatch_merges_same_table_payloads_before_note_cap():
    facts = [
        {
            "table": TABLE_NOTE,
            "item_name": f"Thuyết minh khoản mục | dòng {idx}",
            "value": str(idx),
            "source": "report.md",
        }
        for idx in range(14)
    ]
    state = {
        "user_query": "Phân tích dữ liệu được giao",
        "worker_plan": {
            "evidence_plan": [
                {
                    "table": TABLE_NOTE,
                    "query": "thuyết minh khoản mục",
                    "needby": ["agent_profitability"],
                }
            ]
        },
        "worker_results": {
            "note-part-a": {"table": TABLE_NOTE, "facts": facts[:7]},
            "note-part-b": {"table": TABLE_NOTE, "facts": facts[7:]},
        },
    }
    target = {
        "agent": "agent_profitability",
        "objective": "Phân tích khoản mục.",
        "evidence_queries": [
            {"table": TABLE_NOTE, "query": "thuyết minh khoản mục"}
        ],
    }

    prepared = dispatch_nodes._analysis_input_results_for_target(state, target)

    assert [fact["value"] for fact in prepared[TABLE_NOTE]["facts"]] == [
        str(idx) for idx in range(12)
    ]


def test_worker_query_preserves_company_and_time_hint():
    assert build_worker_query(
        requirements=["phân tích doanh thu"],
        company="APEC",
        time_hint="Quý 2/2025",
    ) == "phân tích doanh thu | APEC | Quý 2/2025"


def test_result_to_facts_marks_mismatched_main_report_row_as_not_found():
    facts = result_to_facts(
        {
            "context": "Chi phí khác | Năm 2024VND: 6.568.363",
            "source": "report.md",
            "documents": ["Chi phí khác | Năm 2024VND: 6.568.363"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí khác | Năm 2024VND",
                    "raw_value": "6.568.363",
                    "source": "report.md",
                }
            ],
        },
        table=TABLE_IS,
        query="chi phí bán hàng",
    )

    assert facts == [
        {
            "content_type": "table_fact",
            "item_name": "chi phí bán hàng",
            "time_hint": "",
            "value": "",
            "source": "report.md",
                "table": TABLE_IS,
                "status": "not_found_after_search",
                "evidence_state": "unmatched_topk",
                "message": (
                "Không tìm thấy dòng chi phí bán hàng trong dữ liệu hiện có. "
                f"Có thể khoản này không phát sinh/không được trình bày riêng trong {TABLE_IS}, "
                "nhưng cần xác nhận từ báo cáo gốc."
            ),
        }
    ]


def test_result_to_facts_prefers_exact_main_report_row_over_child_rows():
    facts = result_to_facts(
        {
            "context": "TÀI SẢN DÀI HẠN | 31/12/2024VND: 206.596.364.067",
            "source": "report.md",
            "documents": [
                "TÀI SẢN DÀI HẠN | 31/12/2024VND: 206.596.364.067",
                "Tài sản dài hạn khác | 31/12/2024VND: 2.594.000",
                "Tài sản dở dang dài hạn | 31/12/2024VND: 4.189.724.285",
            ],
            "metadatas": [
                {
                    "heading": TABLE_BS,
                    "item_name": "TÀI SẢN DÀI HẠN | 31/12/2024VND",
                    "raw_value": "206.596.364.067",
                    "source": "report.md",
                },
                {
                    "heading": TABLE_BS,
                    "item_name": "Tài sản dài hạn khác | 31/12/2024VND",
                    "raw_value": "2.594.000",
                    "source": "report.md",
                },
                {
                    "heading": TABLE_BS,
                    "item_name": "Tài sản dở dang dài hạn | 31/12/2024VND",
                    "raw_value": "4.189.724.285",
                    "source": "report.md",
                },
            ],
        },
        table=TABLE_BS,
        query="tài sản dài hạn",
    )

    assert len(facts) == 1
    assert facts[0]["item_name"] == "TÀI SẢN DÀI HẠN | 31/12/2024VND"
    assert facts[0]["value"] == "206.596.364.067"


def test_medium_plan_routes_from_build_evidence_directly_to_synth(monkeypatch):
    worker_plan = keyworder_runner._finalize_router_targets(
        {
            "evidence_plan": [
                {
                    "table": TABLE_IS,
                    "query": "doanh thu bán hàng và cung cấp dịch vụ",
                    "needby": ["agent_profitability"],
                }
            ],
            "analysis_plan": [
                {
                    "agent": "agent_profitability",
                    "objective": "Phân tích khả năng sinh lời.",
                }
            ],
        },
        {
            "difficulty_level": "medium",
            "analysis_axes": [
                {
                    "axis": "agent_profitability",
                    "objective": "Tính biên lợi nhuận.",
                }
            ],
        },
        user_query="Tính biên lợi nhuận",
    )

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        lambda **_kwargs: {
            "context": "Doanh thu bán hàng và cung cấp dịch vụ: 100",
            "source": "report.md",
            "documents": ["Doanh thu bán hàng và cung cấp dịch vụ: 100"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Doanh thu bán hàng và cung cấp dịch vụ | 2024",
                    "source": "report.md",
                }
            ],
        },
    )

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": worker_plan,
            "dataset_id": "test-dataset",
            "user_query": "Tính biên lợi nhuận",
        }
    )

    assert worker_plan["analysis_plan"] == []
    assert updates["dispatch_phase"] == "synth"
    assert updates["collect_decision"] == "synth"
    assert updates["analysis_dispatch_targets"] == []
    assert route_after_evidence(updates) == "agent_synth"


def test_run_planner_invalid_output_keeps_default_difficulty(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "Tính ROE của công ty là bao nhiêu?",
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "easy"


def test_run_planner_invalid_output_does_not_infer_analysis_axes(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "Phân tích hiệu quả hoạt động và rủi ro tài chính của công ty",
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "easy"
    assert updates["planner_plan"]["analysis_axes"] == []


def test_run_planner_done_trace_keeps_analysis_axes_and_hides_fallback_by_default(monkeypatch):
    analysis_axes = [
        {
            "axis": "agent_profitability",
            "objective": "Tinh ROE tu loi nhuan sau thue va von chu so huu.",
        }
    ]

    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": None,
            "raw": (
                '{"difficulty_level":"medium","analysis_axes":[{"axis":"profitability",'
                '"tables":["BẢNG CÂN ĐỐI KẾ TOÁN","BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"],'
                '"objective":"Tinh ROE tu loi nhuan sau thue va von chu so huu."}],'
                '"company":"Hòa Phát","time_hint":"quý 2/2025","need_web":false}'
            ),
            "parsing_error": ValueError("structured parse failed"),
            "mode": "plain_json_after_structured_parsing_error",
        },
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "ROE quý 2/2025 của Hòa Phát là bao nhiêu?",
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    done_logs = [log for log in updates["trace"] if log["event"] == "planner:done"]

    assert len(done_logs) == 1
    assert done_logs[0]["analysis_axes"] == analysis_axes
    assert updates["planner_plan"]["time_hint"] == "quý 2/2025"
    assert done_logs[0]["time_hint"] == "quý 2/2025"
    assert not any(log["event"] == "planner:structured_output_fallback" for log in updates["trace"])


def test_run_planner_logs_structured_fallback_in_debug_mode(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": None,
            "raw": (
                '{"difficulty_level":"easy","analysis_axes":[{"axis":"agent_liquidity_solvency",'
                '"tables":["BẢNG CÂN ĐỐI KẾ TOÁN"],'
                '"objective":"Tìm tổng tài sản."}],'
                '"company":"Hòa Phát","time_hint":"30/06/2025","need_web":false}'
            ),
            "parsing_error": ValueError("structured parse failed"),
            "mode": "plain_json_after_structured_parsing_error",
        },
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "dataset_id": "",
            "debug_trace": True,
        }
    )

    fallback_logs = [log for log in updates["trace"] if log["event"] == "planner:structured_output_fallback"]

    assert len(fallback_logs) == 1
    assert fallback_logs[0]["debug"] is True


def test_evidence_dispatch_plan_accepts_current_router_output_shape():
    plan = EvidenceDispatchPlan.model_validate(
        {
            "output": {
                "evidence": [
                    {
                        "table": TABLE_IS,
                        "query": "lợi nhuận sau thuế thu nhập doanh nghiệp",
                    }
                ],
                "analysis": [
                    {
                        "agent": "agent_synth",
                        "objective": "Trả lời trực tiếp cho easy/medium.",
                    }
                ],
            }
        }
    )

    payload = plan.model_dump()

    assert payload["evidence_plan"] == [
        {
            "table": TABLE_IS,
            "query": "lợi nhuận sau thuế thu nhập doanh nghiệp",
        }
    ]
    # ``agent_synth`` is not an analysis agent, so the typed envelope drops it
    # instead of carrying a plan entry no analysis node could ever run.  The
    # router profile already requires an empty analysis_plan for easy/medium.
    assert payload["analysis_plan"] == []


def test_keyworder_repairs_current_router_output_with_legacy_target_fields(monkeypatch):
    monkeypatch.setattr(
        keyworder_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": None,
            "raw": (
                '{"output":{"targets":[{"table":"BÁO CÁO TỔNG HỢP",'
                '"keywords":["tổng cộng tài sản"]}],'
                '"analysis":[{"agent":"agent_synth","objective":"Trả lời trực tiếp."}]}}'
            ),
            "parsing_error": ValueError("invalid"),
            "mode": "plain_json_after_structured_parsing_error",
        },
    )

    updates = keyworder_runner.run_keyworder(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "planner_plan": {
                "difficulty_level": "easy",
                "analysis_axes": [
                    {
                        "axis": "core",
                        "tables": [TABLE_BS],
                        "objective": "Tìm tổng tài sản.",
                    }
                ],
                "company": "Hòa Phát",
                "time_hint": "30/06/2025",
                "need_web": False,
            },
            "debug_trace": False,
        }
    )

    assert updates["worker_plan"]["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "query": "tổng cộng tài sản",
            "needby": [],
        }
    ]
    assert updates["worker_plan"]["analysis_plan"] == []
    assert not any(log["event"] == "router:error" for log in updates["trace"])


def test_keyworder_uses_planner_fallback_for_unparseable_router_payload(monkeypatch):
    monkeypatch.setattr(
        keyworder_runner,
        "invoke_prompt",
        lambda *args, **kwargs: {
            "parsed": None,
            "raw": "not a json payload",
            "parsing_error": ValueError("invalid"),
            "mode": "plain_json_after_structured_parsing_error",
        },
    )

    updates = keyworder_runner.run_keyworder(
        {
            "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
            "planner_plan": {
                "difficulty_level": "easy",
                "analysis_axes": [
                    {
                        "axis": "core",
                        "tables": [TABLE_BS],
                        "objective": "Tìm tổng tài sản.",
                    }
                ],
                "company": "Hòa Phát",
                "time_hint": "30/06/2025",
                "need_web": False,
            },
            "debug_trace": False,
        }
    )

    assert updates["worker_plan"]["evidence_plan"] == [
        {
            "table": TABLE_BS,
            "query": "tổng cộng tài sản",
            "needby": [],
        }
    ]
    assert updates["worker_plan"]["analysis_plan"] == []
    assert any(log["event"] == "router:heuristic_fallback" for log in updates["trace"])
    assert not any(log["event"] == "router:error" for log in updates["trace"])


def test_normalize_table_heading_maps_balance_sheet_aliases():
    assert normalize_table_heading("TÀI SẢN") == TABLE_BS
    assert normalize_table_heading("NGUỒN VỐN") == TABLE_BS
    assert (
        normalize_table_heading("**Báo cáo tình hình tài chính riêng tại ngày 31 tháng 12 năm 2025**")
        == TABLE_BS
    )
    assert (
        normalize_table_heading("**Báo cáo kết quả hoạt động kinh doanh riêng cho năm 2025**")
        == TABLE_IS
    )


def test_attach_context_ignores_signature_heading_before_report_title():
    md_text = """
Người duyệt:
**Báo cáo kết quả hoạt động kinh doanh riêng cho năm kết thúc ngày 31 tháng 12 năm 2025**
| Chỉ tiêu | 2025 |
| --- | --- |
| Lợi nhuận sau thuế TNDN | 100 |
"""

    tables = attach_context(md_text)

    assert len(tables) == 1
    assert tables[0]["heading"] != "Người duyệt:"
    assert normalize_table_heading(tables[0]["heading"]) == TABLE_IS


def test_get_related_info_does_not_fallback_to_report_wide_search_when_heading_has_no_hits(monkeypatch):
    monkeypatch.setattr(tools_module, "embed_query_text", lambda _query: [0.0])
    collection = FakeCollection(
        primary_result={"documents": [[]], "metadatas": [[]]},
        fallback_result={
            "documents": [[
                "Bảng TÀI SẢN. **TỔNG TÀI SẢN (270 = 100 + 200)** | 31/12/2025 VND. Giá trị 45.952.496.972.636.",
                "Bảng TÀI SẢN. Tài sản ngắn hạn | 31/12/2025 VND. Giá trị 27.309.234.148.199.",
            ]],
            "metadatas": [[
                {
                    "heading": "TÀI SẢN",
                    "item_name": "**TỔNG TÀI SẢN (270 = 100 + 200)** | 31/12/2025 VND",
                    "source": "report.md",
                },
                {
                    "heading": "TÀI SẢN",
                    "item_name": "Tài sản ngắn hạn | 31/12/2025 VND",
                    "source": "report.md",
                },
            ]],
        },
    )

    result = get_related_info("tổng cộng tài sản", TABLE_BS, collection)

    assert result["context"] == ""
    assert result["source"] == ""
    assert len(collection.calls) == 1
    assert collection.calls[0]["where"] == {"heading": TABLE_BS}
    assert collection.calls[0]["n_results"] == 50


def test_get_related_info_keeps_requested_heading_when_primary_match_is_weak(monkeypatch):
    monkeypatch.setattr(tools_module, "embed_query_text", lambda _query: [0.0])
    collection = FakeCollection(
        primary_result={
            "documents": [[
                "Bảng BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH. Chi phí thuế thu nhập hiện hành | 2025VND. Giá trị 2.128.415.483.304.",
            ]],
            "metadatas": [[
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí thuế thu nhập hiện hành | 2025VND",
                    "source": "report.md",
                },
            ]],
        },
        fallback_result={
            "documents": [[
                "Bảng Người duyệt:. Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52) | 2025VND. Giá trị 9.359.349.635.629.",
                "Bảng Người duyệt:. Chi phí thuế TNDN hiện hành | 2025VND. Giá trị 2.128.415.483.304.",
            ]],
            "metadatas": [[
                {
                    "heading": "Người duyệt:",
                    "item_name": "Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52) | 2025VND",
                    "source": "report.md",
                },
                {
                    "heading": "Người duyệt:",
                    "item_name": "Chi phí thuế TNDN hiện hành | 2025VND",
                    "source": "report.md",
                },
            ]],
        },
    )

    result = get_related_info("lợi nhuận sau thuế thu nhập doanh nghiệp", TABLE_IS, collection)

    assert "Lợi nhuận sau thuế TNDN" not in result["context"]
    assert "Chi phí thuế thu nhập hiện hành" in result["context"]
    assert "Chi phí thuế TNDN hiện hành" not in result["context"]
    assert len(collection.calls) == 1
    assert collection.calls[0]["where"] == {"heading": TABLE_IS}
