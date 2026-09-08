"""Retrieval facts retain the fields required by deterministic recall."""

from graph.evidence import _compact_fact_for_prompt
from tools.evidence import dedupe_facts, result_to_facts
from tools.tools import (
    _extract_docs_and_metas,
    _merge_docs_and_metas,
    _rerank_matches,
)


def test_result_to_facts_retains_entity_unit_and_reference():
    facts = result_to_facts(
        {
            "documents": ["Lãi cho vay năm nay: 100 VND"],
            "metadatas": [
                {
                    "company": "Công ty A",
                    "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "item_name": "Lãi cho vay | Năm nay",
                    "raw_value": "100",
                    "period": "năm nay",
                    "unit": "VND",
                    "note_ref": "V.4",
                }
            ],
        },
        table="THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        query="lãi cho vay năm nay",
        limit=12,
    )

    assert facts[0]["company"] == "Công ty A"
    assert facts[0]["unit"] == "VND"
    assert facts[0]["reference"] == "V.4"


def test_vector_result_roundtrip_retains_typed_fact_metadata_and_state():
    metadata = {
        "company": "Công ty A",
        "fiscal_year": "2025",
        "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        "fact_id": "fact-v4-interest-current",
        "item_code": "note_table",
        "note_ref": "V.4",
        "note_number": "4",
        "note_title": "Doanh thu hoạt động tài chính",
        "section_path": "V > 4 > Lãi cho vay",
        "block_id": "table-v4-1",
        "subheading": "4. Doanh thu hoạt động tài chính",
        "item_name": "Lãi cho vay | Năm nay",
        "row_label": "Lãi cho vay",
        "column_label": "Năm nay",
        "raw_value": "100",
        "normalized_value": "100",
        "parsed_value": "100",
        "period": "cuối",
        "period_label": "Năm nay",
        "period_role": "current",
        "value_type": "",
        "aggregation_level": "component",
        "unit": "VND",
        "value_kind": "amount",
        "source": "report.md#page=23",
        "source_page": "23",
        "rerank_score": 42.5,
        "similarity_score": 0.82,
        "distance": 0.18,
        "score_query": "lãi cho vay",
        "score_intent": "lãi cho vay năm nay",
        "retrieval_origin": "hybrid",
    }
    facts = result_to_facts(
        {
            "documents": ["Lãi cho vay | Năm nay: 100 VND"],
            "metadatas": [metadata],
            "index_generation": "index-build-abc",
        },
        table="THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        query="lãi cho vay năm nay",
        limit=12,
    )

    fact = facts[0]
    for field in (
        "fact_id",
        "row_label",
        "column_label",
        "value_kind",
        "parsed_value",
        "period",
        "period_label",
        "period_role",
        "aggregation_level",
        "note_ref",
        "section_path",
        "block_id",
        "source_page",
        "fiscal_year",
        "rerank_score",
        "similarity_score",
        "distance",
        "score_query",
        "score_intent",
        "retrieval_origin",
    ):
        assert fact[field] == metadata[field]
    assert fact["index_generation"] == "index-build-abc"
    assert fact["status"] == "found"
    assert fact["evidence_state"] == "matched"
    assert fact["search_exhaustive"] is False

    prompt_fact = _compact_fact_for_prompt(fact)
    for field in (
        "fact_id",
        "row_label",
        "column_label",
        "parsed_value",
        "period_label",
        "period_role",
        "aggregation_level",
        "section_path",
        "block_id",
        "source_page",
        "fiscal_year",
        "index_generation",
        "evidence_state",
    ):
        assert prompt_fact[field] == metadata.get(field, fact.get(field))


def test_balance_section_key_survives_retrieval_and_prompt_compaction():
    metadata = {
        "heading": "BẢNG CÂN ĐỐI KẾ TOÁN",
        "fact_id": "fact-bs-270-current",
        "item_code": "270",
        "item_name": "TỔNG TÀI SẢN | 31/12/2025",
        "row_label": "TỔNG TÀI SẢN",
        "column_label": "31/12/2025",
        "period": "cuối",
        "aggregation_level": "total",
        "section_key": "tong_tai_san",
        "parsed_value": "100",
        "source": "report.md#page=8",
    }
    facts = result_to_facts(
        {
            "documents": ["TỔNG TÀI SẢN | 31/12/2025: 100 VND"],
            "metadatas": [metadata],
        },
        table="BẢNG CÂN ĐỐI KẾ TOÁN",
        query="tổng tài sản cuối kỳ",
        limit=12,
    )

    assert facts[0]["section_key"] == "tong_tai_san"
    assert _compact_fact_for_prompt(facts[0])["section_key"] == "tong_tai_san"


def test_dense_scores_remain_aligned_without_mutating_source_metadata():
    source_metas = [{"fact_id": "f1"}, {"fact_id": "f2"}]
    docs, metas = _extract_docs_and_metas(
        {
            "documents": [["one", "two"]],
            "metadatas": [source_metas],
            "similarities": [[0.91, None]],
            "distances": [[0.09, None]],
        }
    )

    assert docs == ["one", "two"]
    assert metas[0]["similarity_score"] == 0.91
    assert metas[0]["distance"] == 0.09
    assert "similarity_score" not in metas[1]
    assert "distance" not in metas[1]
    assert source_metas == [{"fact_id": "f1"}, {"fact_id": "f2"}]


def test_dense_duplicate_enriches_structured_candidate_with_real_scores():
    structured_meta = {
        "heading": "NOTE",
        "item_code": "note_table",
        "note_ref": "V.4",
        "item_name": "Lãi cho vay | Năm nay",
        "source": "report.md",
        "raw_value": "100",
    }
    dense_meta = {
        **structured_meta,
        "similarity_score": 0.88,
        "distance": 0.12,
    }

    docs, metas = _merge_docs_and_metas(
        ["fact"],
        [structured_meta],
        ["same dense rendering"],
        [dense_meta],
    )

    assert docs == ["fact"]
    assert metas[0]["similarity_score"] == 0.88
    assert metas[0]["distance"] == 0.12
    assert "similarity_score" not in structured_meta


def test_rerank_persists_deterministic_score_without_mutating_inputs(monkeypatch):
    source_metas = [{"fact_id": "low"}, {"fact_id": "high"}]
    score_by_doc = {"low": 1.0, "high": 9.0}
    monkeypatch.setattr(
        "tools.tools._item_match_score",
        lambda _query, _meta, doc, intent=None: score_by_doc[doc],
    )

    docs, metas = _rerank_matches(
        "query",
        ["low", "high"],
        source_metas,
        limit=2,
    )

    assert docs == ["high", "low"]
    assert [meta["rerank_score"] for meta in metas] == [9.0, 1.0]
    assert source_metas == [{"fact_id": "low"}, {"fact_id": "high"}]


def test_dedupe_merge_does_not_drop_typed_metadata():
    base = {
        "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        "item_name": "Lãi cho vay | Năm nay",
        "note_ref": "V.4",
        "time_hint": "cuối",
        "value": "100",
        "source": "report.md#page=23",
        "status": "found",
        "evidence_state": "matched",
    }
    merged = dedupe_facts(
        [
            base,
            {
                **base,
                "fact_id": "fact-v4-interest-current",
                "row_label": "Lãi cho vay",
                "column_label": "Năm nay",
                "period_role": "current",
                "aggregation_level": "component",
            },
        ]
    )
    assert len(merged) == 1
    assert merged[0]["fact_id"] == "fact-v4-interest-current"
    assert merged[0]["row_label"] == "Lãi cho vay"
    assert merged[0]["period_role"] == "current"


def test_topk_miss_and_missing_metadata_never_claim_exhaustive_absence():
    empty = result_to_facts(
        {"documents": [], "metadatas": []},
        table="THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        query="cam kết thuê",
    )
    assert empty[0]["status"] == "not_found_after_search"
    assert empty[0]["evidence_state"] == "unmatched_topk"
    assert not empty[0].get("search_exhaustive", False)

    ambiguous = result_to_facts(
        {"documents": ["Một đoạn không có metadata"], "metadatas": [{}]},
        table="THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        query="cam kết thuê",
    )
    assert ambiguous[0]["status"] == "ambiguous"
    assert ambiguous[0]["evidence_state"] == "ambiguous"
    assert not ambiguous[0].get("search_exhaustive", False)
