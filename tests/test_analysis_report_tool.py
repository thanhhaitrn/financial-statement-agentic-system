"""Regression tests for test analysis report tool."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import agent_runner
from tools.langchain_tools import get_tool_names_for_agent
from schemas.table_names import TABLE_BS, TABLE_IS
from tools import tool_runner


def _tool_call_payload(tool_name: str, query: str) -> dict:
    tool_call = {
        "name": tool_name,
        "args": {"query": query},
        "id": "test-tool-call",
        "type": "tool_call",
    }
    return {
        "kind": "tool_calls",
        "tool_calls": [tool_call],
    }


def _analysis_tool_state(tool_name: str, action_query: str, requirements: list[str]) -> dict:
    target = {
        "agent": "agent_profitability",
        "requirements": requirements,
    }
    payload = _tool_call_payload(tool_name, action_query)
    return {
        "debug_trace": True,
        "followup_rounds": 0,
        "worker_plan": {"targets": [target]},
        "dispatch_target": target,
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "response": json.dumps(payload, ensure_ascii=False),
                "parsed_output": payload,
                "tool_calls": payload["tool_calls"],
            }
        ],
        "tool_call_counts": {},
        "tool_results": [],
    }


def test_analysis_prompt_payload_avoids_duplicate_evidence_facts_when_inputs_exist():
    state = {
        "analysis_input_results": {
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "facts": [
                    {
                        "item_name": "Doanh thu thuần",
                        "value": "100",
                        "source": "report.md",
                    }
                ],
            }
        },
        "evidence_pack": {
            "items": [
                {
                    "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    "query": "doanh thu thuần",
                    "facts_n": 1,
                    "facts_preview": [
                        {
                            "item_name": "Doanh thu thuần",
                            "value": "100",
                            "source": "report.md",
                        }
                    ],
                }
            ],
            "facts_by_table": {
                "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                    "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    "facts": [
                        {
                            "item_name": "Doanh thu thuần",
                            "value": "100",
                            "source": "report.md",
                        }
                    ],
                }
            },
            "stats": {"facts_n": 1},
        },
    }

    payload = agent_runner._evidence_pack_payload_for_prompt(state)

    assert "facts_by_table" not in payload
    assert "facts_preview" not in payload["items"][0]
    assert payload["items"][0] == {
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "query": "doanh thu thuần",
        "facts_n": 1,
    }
    assert payload["stats"]["facts_n"] == 1


def test_analysis_prompt_treats_empty_analysis_inputs_as_explicit_scope():
    state = {
        "analysis_input_results": {},
        "worker_results": {
            TABLE_IS: {
                "table": TABLE_IS,
                "facts": [
                    {
                        "table": TABLE_IS,
                        "item_name": "Doanh thu thuần",
                        "value": "100",
                    }
                ],
            }
        },
        "evidence_pack": {
            "facts_by_table": {
                TABLE_IS: {
                    "table": TABLE_IS,
                    "facts": [
                        {
                            "table": TABLE_IS,
                            "item_name": "Doanh thu thuần",
                            "value": "100",
                        }
                    ],
                }
            },
            "stats": {"facts_n": 1},
        },
        "tool_results": [],
    }

    assert agent_runner._analysis_input_results_payload_for_prompt(state) == {}
    assert "facts_by_table" not in agent_runner._evidence_pack_payload_for_prompt(state)
    assert agent_runner._analysis_evidence_facts(state, "agent_profitability") == []


def test_analysis_tool_instruction_allows_note_refetch_for_more_note_facts():
    instruction = agent_runner._force_analysis_tool_call_instruction(
        "Base instruction.",
        "thuyết minh 23 chi phí tài chính",
    )

    assert "NOTE ban đầu giữ tối đa 12 facts" in instruction
    assert "Phần đầu báo cáo chỉ được cung cấp qua evidence stage" in instruction
    assert "dùng get_note_info" in instruction
    assert "chủ đề/số thuyết minh ngắn" in instruction


def test_analysis_evidence_check_counts_tool_result_facts_as_satisfied():
    target = {
        "agent": "agent_efficiency",
        "evidence_queries": [
            {"table": TABLE_IS, "query": "chi phí bán hàng"},
            {"table": TABLE_BS, "query": "các khoản phải trả ngắn hạn"},
        ],
    }
    state = {
        "followup_rounds": 0,
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_results": [
            {
                "agent": "agent_efficiency",
                "round": 0,
                "tool": "get_income_statement_info",
                "args": {"query": "chi phí bán hàng", "table": TABLE_IS},
                "results": {
                    "table": TABLE_IS,
                    "facts": [
                        {
                            "table": TABLE_IS,
                            "item_name": "chi phí bán hàng",
                            "value": "5",
                            "status": "found",
                        }
                    ],
                },
            }
        ],
    }

    missing = agent_runner._missing_requirements_after_evidence_check(
        state,
        "agent_efficiency",
    )

    assert missing == ["các khoản phải trả ngắn hạn"]


def test_analysis_evidence_check_does_not_requery_not_found_fact():
    target = {
        "agent": "agent_efficiency",
        "evidence_queries": [
            {"table": TABLE_IS, "query": "chi phí bán hàng"},
        ],
    }
    state = {
        "followup_rounds": 0,
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "analysis_input_results": {
            TABLE_IS: {
                "table": TABLE_IS,
                "facts": [
                    {
                        "table": TABLE_IS,
                        "item_name": "chi phí bán hàng",
                        "value": "",
                        "status": "not_found_after_search",
                        "interpretation_hint": "Không tìm thấy dòng chi phí bán hàng.",
                    }
                ],
            }
        },
        "worker_results": {},
        "tool_results": [],
    }

    missing = agent_runner._missing_requirements_after_evidence_check(
        state,
        "agent_efficiency",
    )

    assert missing == []


def test_analysis_uses_deterministic_tool_call_for_statement_requirement(monkeypatch):
    target = {
        "agent": "agent_efficiency",
        "evidence_queries": [
            {"table": TABLE_IS, "query": "chi phí bán hàng"},
            {"table": TABLE_BS, "query": "các khoản phải trả ngắn hạn"},
        ],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá hiệu quả hoạt động",
        "worker_query": "",
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_observations": [],
        "tool_results": [],
        "tool_call_counts": {},
        "web_summary": "",
        "followup_rounds": 0,
    }

    def fail_if_llm_tool_call_runs(*_args, **_kwargs):
        raise AssertionError("deterministic line-item tool call should not invoke LLM")

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fail_if_llm_tool_call_runs)

    updates = agent_runner.call_analysis_agent(state, "agent_efficiency")
    item = updates["worker_messages"][0]

    assert item["parsed_output"]["kind"] == "tool_calls"
    assert item["parsed_output"]["tool_calls"][0]["name"] == "get_income_statement_info"
    assert item["parsed_output"]["tool_calls"][0]["args"]["query"] == "chi phí bán hàng"
    assert any(
        log["event"] == "analysis:deterministic_tool_call"
        for log in updates["trace"]
    )


def test_analysis_moves_to_next_missing_requirement_after_cache_hit(monkeypatch):
    target = {
        "agent": "agent_efficiency",
        "evidence_queries": [
            {"table": TABLE_IS, "query": "chi phí bán hàng"},
            {"table": TABLE_BS, "query": "các khoản phải trả ngắn hạn"},
        ],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá hiệu quả hoạt động",
        "worker_query": "",
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_observations": [
            {
                "agent": "agent_efficiency",
                "round": 0,
                "text": f"[get_income_statement_info source=cache table={TABLE_IS} query=chi phí bán hàng]\nChi phí bán hàng: 5",
            }
        ],
        "tool_results": [
            {
                "agent": "agent_efficiency",
                "kind": "cache_hit",
                "round": 0,
                "tool": "get_income_statement_info",
                "args": {"query": "chi phí bán hàng", "table": TABLE_IS},
                "results": {
                    "table": TABLE_IS,
                    "facts": [
                        {
                            "table": TABLE_IS,
                            "item_name": "chi phí bán hàng",
                            "value": "5",
                            "status": "found",
                        }
                    ],
                },
            }
        ],
        "tool_call_counts": {"agent_efficiency": {"round": 0, "count": 1}},
        "web_summary": "",
        "followup_rounds": 0,
    }

    def fail_if_llm_tool_call_runs(*_args, **_kwargs):
        raise AssertionError("next line-item tool call should not invoke LLM")

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fail_if_llm_tool_call_runs)

    updates = agent_runner.call_analysis_agent(state, "agent_efficiency")
    item = updates["worker_messages"][0]

    assert item["current_requirement"] == "các khoản phải trả ngắn hạn"
    assert item["parsed_output"]["kind"] == "tool_calls"
    assert item["parsed_output"]["tool_calls"][0]["name"] == "get_balance_sheet_info"
    assert item["parsed_output"]["tool_calls"][0]["args"]["query"] == "các khoản phải trả ngắn hạn"


def test_tool_allowlists_expose_only_scoped_analysis_tools():
    assert get_tool_names_for_agent("unknown_agent") == set()
    assert get_tool_names_for_agent("agent_profitability") == {
        "get_balance_sheet_info",
        "get_income_statement_info",
        "get_cashflow_info",
        "get_note_info",
    }
    assert get_tool_names_for_agent("agent_cashflow_analysis") == {
        "get_balance_sheet_info",
        "get_income_statement_info",
        "get_cashflow_info",
        "get_note_info",
    }


def test_analysis_scoped_tool_adds_table_filter_and_normalizes_query(monkeypatch):
    captured = {}

    def fake_get_income_statement_info(**kwargs):
        captured.update(kwargs)
        return {
            "context": "Lợi nhuận sau thuế 2024: 12. Tổng cộng tài sản 2024: 100.",
            "source": "kb",
        }

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_get_income_statement_info,
    )

    updates = tool_runner.call_tool_for_agent(
        _analysis_tool_state(
            "get_income_statement_info",
            "lợi nhuận sau thuế năm 2024",
            ["đánh giá khả năng sinh lời năm 2024 từ lợi nhuận và tổng tài sản"],
        ),
        "agent_profitability",
    )

    assert captured["query"] == "lợi nhuận sau thuế thu nhập doanh nghiệp"
    assert "collection" in captured
    assert captured["table"] == "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
    assert updates["tool_results"][0]["tool"] == "get_income_statement_info"
    assert updates["tool_results"][0]["args"]["table"] == "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
    assert "table=BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH" in updates["tool_observations"][0]["text"]


def test_scoped_info_normalizes_query_from_tool_call(monkeypatch):
    captured = {}
    table = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
    current_target = {
        "agent": "agent_profitability",
        "objective": "Đánh giá doanh thu.",
        "evidence_queries": [{"table": table, "query": "chi phí bán hàng"}],
    }
    payload = _tool_call_payload("get_income_statement_info", "doanh thu thuần")
    state = {
        "debug_trace": True,
        "followup_rounds": 0,
        "worker_plan": {
            "analysis_plan": [
                {
                    "agent": "agent_profitability",
                    "objective": "Đánh giá doanh thu.",
                    "evidence_queries": [
                        {
                            "table": table,
                            "query": "doanh thu thuần về bán hàng và cung cấp dịch vụ",
                        },
                        {"table": table, "query": "chi phí bán hàng"},
                    ],
                },
            ]
        },
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "response": json.dumps(payload, ensure_ascii=False),
                "parsed_output": payload,
                "tool_calls": payload["tool_calls"],
                "dispatch_target": current_target,
                "current_requirement": "chi phí bán hàng",
                "requirement_index": 0,
            }
        ],
        "tool_call_counts": {},
        "tool_results": [],
    }

    def fake_get_income_statement_info(**kwargs):
        captured.update(kwargs)
        return {"context": "Chi phí bán hàng: 10", "source": "kb"}

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_get_income_statement_info,
    )

    tool_runner.call_tool_for_agent(state, "agent_profitability")

    assert captured["query"] == "doanh thu thuần về bán hàng và cung cấp dịch vụ"


def test_analysis_agent_can_answer_in_tool_choice_mode_when_input_facts_are_sufficient(monkeypatch):
    target = {
        "agent": "agent_profitability",
        "requirements": ["đánh giá khả năng sinh lời năm 2024"],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá khả năng sinh lời năm 2024",
        "worker_query": "",
        "worker_plan": {"targets": [target]},
        "dispatch_target": target,
        "analysis_input_results": {
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "facts": [
                    {
                        "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
                        "value": "12",
                        "status": "found",
                    }
                ],
            }
        },
        "worker_results": {},
        "tool_observations": [],
        "tool_call_counts": {},
        "web_summary": "",
        "followup_rounds": 0,
    }

    captured_payload = {}

    def fake_run_analysis_tool_call_once(payload, _agent_name):
        captured_payload.update(payload)
        parsed = {"answer": "Đủ dữ liệu để phân tích.", "requirements": []}
        return parsed, json.dumps(parsed, ensure_ascii=False), "", "native_tool_call", {}

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fake_run_analysis_tool_call_once)

    updates = agent_runner.call_analysis_agent(state, "agent_profitability")
    item = updates["worker_messages"][0]

    assert item["parsed_output"]["answer"] == "Đủ dữ liệu để phân tích."
    assert item["parsed_output"]["requirements"] == []
    assert "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH" in captured_payload["allowed_keywords_json"]
    plan_payload = json.loads(captured_payload["plan_json"])
    assert plan_payload == {
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "requirements": ["đánh giá khả năng sinh lời năm 2024"],
            }
        ]
    }
    worker_results_payload = json.loads(captured_payload["worker_results_json"])
    fact_payload = worker_results_payload["BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"]["facts"][0]
    assert fact_payload == {
        "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
        "value": "12",
    }
    evidence_pack_payload = json.loads(captured_payload["evidence_pack_json"])
    assert evidence_pack_payload == {"items": [], "stats": {}}
    assert item["parsed_output"].get("kind") != "tool_calls"


def test_analysis_agent_calls_report_tool_when_input_fact_is_ambiguous(monkeypatch):
    target = {
        "agent": "agent_profitability",
        "requirements": ["đánh giá khả năng sinh lời năm 2024"],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá khả năng sinh lời năm 2024",
        "worker_query": "",
        "worker_plan": {"targets": [target]},
        "dispatch_target": target,
        "analysis_input_results": {
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "facts": [
                    {
                        "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
                        "value": "12 hoặc 13",
                        "status": "ambiguous",
                    }
                ],
            }
        },
        "worker_results": {},
        "tool_observations": [],
        "tool_call_counts": {},
        "web_summary": "",
        "followup_rounds": 0,
    }

    def fake_run_analysis_tool_call_once(_payload, _agent_name):
        parsed = _tool_call_payload("get_income_statement_info", "kiểm tra lợi nhuận sau thuế")
        return parsed, json.dumps(parsed, ensure_ascii=False), "", "native_tool_call", {}

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fake_run_analysis_tool_call_once)

    updates = agent_runner.call_analysis_agent(state, "agent_profitability")
    item = updates["worker_messages"][0]

    assert item["parsed_output"]["kind"] == "tool_calls"
    assert item["parsed_output"]["tool_calls"][0]["name"] == "get_income_statement_info"


def test_analysis_agent_synthesizes_report_tool_call_when_objective_is_pending(monkeypatch):
    target = {
        "agent": "agent_profitability",
        "requirements": ["đánh giá khả năng sinh lời năm 2024"],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá khả năng sinh lời năm 2024",
        "worker_query": "",
        "worker_plan": {"targets": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_observations": [],
        "tool_call_counts": {},
        "web_summary": "",
        "followup_rounds": 0,
    }

    def fake_run_analysis_tool_call_once(_payload, _agent_name):
        parsed = {
            "answer": "Chưa đủ dữ liệu.",
            "requirements": ["lợi nhuận sau thuế năm 2024"],
        }
        return parsed, json.dumps(parsed, ensure_ascii=False), "", "", {}

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fake_run_analysis_tool_call_once)

    updates = agent_runner.call_analysis_agent(state, "agent_profitability")
    item = updates["worker_messages"][0]

    assert item["parsed_output"]["kind"] == "tool_calls"
    assert item["parsed_output"]["tool_calls"][0]["name"] == "get_income_statement_info"
    assert item["parsed_output"]["tool_calls"][0]["args"]["query"] == "lợi nhuận sau thuế thu nhập doanh nghiệp"
    assert any(
        log["event"] == "analysis:native_tool_call_synthesized"
        for log in updates["trace"]
    )


def test_analysis_agent_falls_back_to_nonempty_answer_when_model_returns_empty(monkeypatch):
    target = {
        "agent": "agent_profitability",
        "requirements": ["đánh giá khả năng sinh lời năm 2024"],
    }
    state = {
        "debug_trace": True,
        "user_query": "Đánh giá khả năng sinh lời năm 2024",
        "worker_query": "",
        "worker_plan": {"targets": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_observations": [],
        "tool_call_counts": {},
        "web_summary": "",
        "followup_rounds": 0,
    }

    def fake_run_analysis_tool_call_once(_payload, _agent_name):
        parsed = {"answer": "", "requirements": []}
        return parsed, json.dumps(parsed, ensure_ascii=False), "", "native_tool_call", {}

    monkeypatch.setattr(agent_runner, "_run_analysis_tool_call_once", fake_run_analysis_tool_call_once)

    updates = agent_runner.call_analysis_agent(state, "agent_profitability")
    item = updates["worker_messages"][0]

    assert item["parsed_output"]["answer"]
    assert item["parsed_output"]["requirements"] == []
