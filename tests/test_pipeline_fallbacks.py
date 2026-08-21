"""Regression tests for test pipeline fallbacks."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import keyworder_runner, planner_runner
from graph import dispatch_nodes
from graph import evidence as evidence_node
from graph.router import build_worker_query, route_after_evidence
from ingestion.table_parser import attach_context
from schemas.agent_outputs import EvidenceDispatchPlan
from schemas.table_names import TABLE_IS, TABLE_NOTE, normalize_table_heading
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


def test_router_finalize_preserves_hard_analysis_axes_without_legacy_baseline_facts():
    planner_plan = {
        "difficulty_level": "hard",
        "analysis_axes": [
            {
                "axis": "agent_profitability",
                "objective": "Đánh giá khả năng sinh lời năm 2024.",
            },
            {
                "axis": "agent_efficiency",
                "objective": "Đánh giá hiệu quả hoạt động năm 2024.",
            },
        ],
    }

    finalized = keyworder_runner._finalize_router_targets(
        {"targets": []},
        planner_plan,
        user_query="Đánh giá khả năng sinh lời và hiệu quả hoạt động năm 2024",
    )

    assert finalized["evidence_plan"] == []
    assert finalized["analysis_plan"] == [
        {
            "agent": "agent_profitability",
            "objective": "Đánh giá khả năng sinh lời năm 2024.",
            "evidence_queries": [],
        },
        {
            "agent": "agent_efficiency",
            "objective": "Đánh giá hiệu quả hoạt động năm 2024.",
            "evidence_queries": [],
        },
    ]
    assert finalized["targets"] == finalized["analysis_plan"]


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
        user_query="Phân tích khả năng sinh lời",
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

    assert [call["query"] for call in calls] == [
        "chi phí tài chính",
        "doanh thu bán hàng và cung cấp dịch vụ",
    ]
    assert updates["evidence_pack"]["targets"][0]["requirements"] == [
        "chi phí tài chính",
        "doanh thu bán hàng và cung cấp dịch vụ",
    ]
    assert updates["analysis_dispatch_targets"][0]["evidence_queries"] == [
        {"table": TABLE_IS, "query": "chi phí tài chính"},
        {"table": TABLE_IS, "query": "doanh thu bán hàng và cung cấp dịch vụ"},
    ]


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
    assert payload["analysis_plan"] == [
        {
            "agent": "agent_synth",
            "objective": "Trả lời trực tiếp cho easy/medium.",
        }
    ]


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
