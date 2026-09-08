"""Regression tests for keeping audit evidence separate from Synth context."""

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from graph import evidence as evidence_node
from schemas.table_names import TABLE_CF, TABLE_IS, TABLE_NOTE
from tools.evidence import clear_runtime_evidence_cache


def _missing_result() -> dict:
    return {
        "context": "",
        "source": "report.md",
        "documents": [],
        "metadatas": [],
    }


def test_non_core_topk_miss_is_pruned_only_from_synth_payload(monkeypatch):
    clear_runtime_evidence_cache()
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if kwargs["query"] == "lợi nhuận sau thuế":
            return {
                "context": "Lợi nhuận sau thuế TNDN: 100",
                "source": "report.md",
                "documents": ["Lợi nhuận sau thuế TNDN: 100"],
                "metadatas": [
                    {
                        "heading": TABLE_IS,
                        "item_name": "Lợi nhuận sau thuế TNDN",
                        "raw_value": "100",
                        "fact_id": "net-income-current",
                        "source": "report.md",
                    }
                ],
            }
        return _missing_result()

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)
    monkeypatch.setattr(
        evidence_node,
        "_ensure_report_section_target",
        lambda targets, _query: targets,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "dataset_id": "synth-prune-non-core-miss",
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_IS,
                        "query": "lợi nhuận sau thuế",
                        "needby": ["agent_profitability"],
                    },
                    {
                        "table": TABLE_CF,
                        "query": "tiền thu từ phát hành cổ phiếu",
                        "needby": [],
                    },
                ],
                "analysis_plan": [],
            },
        }
    )

    assert len(calls) == 3  # one matched call plus initial and targeted miss
    assert TABLE_CF not in updates["worker_results"]
    assert TABLE_CF not in updates["evidence_pack"]["facts_by_table"]
    assert updates["worker_results"][TABLE_IS]["facts"][0]["value"] == "100"

    # Audit and evaluation branches retain the miss and its ledger transition.
    audit_fact = updates["ragas_facts_by_table"][TABLE_CF]["facts"][0]
    assert audit_fact["status"] == "not_found_after_search"
    assert audit_fact["message"]
    miss_entry = next(
        entry
        for entry in updates["evidence_ledger"]["entries"]
        if entry["requirement"] == "tiền thu từ phát hành cổ phiếu"
    )
    assert miss_entry["requirement_state"]["after_retry"] == "unmatched_topk"
    stats = updates["evidence_pack"]["stats"]
    assert stats["facts_n"] == 2
    assert stats["facts_n_for_synth"] == 1
    assert stats["facts_pruned_from_synth_n"] == 1
    assert "Không tìm thấy" not in json.dumps(
        updates["worker_results"],
        ensure_ascii=False,
    )


def test_core_topk_miss_keeps_only_message_free_diagnostic(monkeypatch):
    clear_runtime_evidence_cache()
    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        lambda **_kwargs: _missing_result(),
    )
    monkeypatch.setattr(
        evidence_node,
        "_ensure_report_section_target",
        lambda targets, _query: targets,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "dataset_id": "synth-prune-core-miss",
            "user_query": "Chi phí bán hàng là bao nhiêu?",
            "worker_plan": {
                "evidence_plan": [
                    {"table": TABLE_IS, "query": "chi phí bán hàng"}
                ]
            },
        }
    )

    prompt_fact = updates["worker_results"][TABLE_IS]["facts"][0]
    audit_fact = updates["ragas_facts_by_table"][TABLE_IS]["facts"][0]
    assert prompt_fact["status"] == "not_found_after_search"
    assert "message" not in prompt_fact
    assert audit_fact["message"]
    assert updates["evidence_pack"]["stats"]["facts_n_for_synth"] == 1


def test_found_fact_suppresses_same_requirement_table_level_diagnostic():
    results = evidence_node._drop_redundant_prompt_diagnostics(
        {
            TABLE_IS: {
                "table": TABLE_IS,
                "facts": [
                    {
                        "table": TABLE_IS,
                        "item_name": "Lợi nhuận sau thuế TNDN",
                        "evidence_query": "lợi nhuận sau thuế",
                        "value": "100",
                        "status": "found",
                    }
                ],
            },
            TABLE_NOTE: {
                "table": TABLE_NOTE,
                "facts": [
                    {
                        "table": TABLE_NOTE,
                        "item_name": "lợi nhuận sau thuế",
                        "evidence_query": "lợi nhuận sau thuế",
                        "value": "",
                        "status": "not_found_after_search",
                        "message": "Không tìm thấy trong thuyết minh.",
                    }
                ],
            },
        }
    )

    assert results[TABLE_IS]["facts"][0]["value"] == "100"
    assert TABLE_NOTE not in results


def test_route_balanced_cap_reserves_current_and_previous_for_each_metric():
    facts = []
    for metric in ("doanh thu", "lợi nhuận gộp", "lợi nhuận sau thuế"):
        for role in ("current", "previous"):
            facts.append(
                {
                    "fact_id": f"{metric}:{role}",
                    "item_name": metric,
                    "value": "1",
                    "period_role": role,
                    "evidence_query": f"{metric} năm nay và năm trước",
                }
            )

    selected = evidence_node._select_route_balanced_facts(facts, limit=6)

    assert {fact["fact_id"] for fact in selected} == {
        f"{metric}:{role}"
        for metric in ("doanh thu", "lợi nhuận gộp", "lợi nhuận sau thuế")
        for role in ("current", "previous")
    }


def test_note_ref_request_carries_parent_linkage_and_is_bounded():
    facts = []
    for index in range(12):
        label = "Phải thu ngắn hạn khác" if index == 11 else f"Khoản mục {index}"
        facts.append(
            {
                "table": TABLE_IS,
                "fact_id": f"parent-{index}",
                "item_name": f"{label} | Số cuối kỳ",
                "value": str(index + 1),
                "period_label": "Số cuối kỳ",
                "aggregation_level": "component",
                "note_ref": f"V.{index + 1}",
                "needby": ["agent_liquidity_solvency"],
            }
        )

    requests = evidence_node._note_ref_queries_from_results(
        {TABLE_IS: {"table": TABLE_IS, "facts": facts}}
    )

    assert len(requests) == evidence_node.NOTE_REF_ENRICHMENT_LIMIT
    assert requests[0]["source_fact_id"] == "parent-11"
    assert requests[0]["source_item"] == "Phải thu ngắn hạn khác"
    assert requests[0]["source_value"] == "12"
    assert requests[0]["source_period_label"] == "Số cuối kỳ"
