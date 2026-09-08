import pytest

from graph import evidence as evidence_node
from dataset_batch_result import (
    evidence_ledger_contract_errors,
    extract_evidence_ledger,
)
from schemas.requirements import (
    REQUIREMENT_AMBIGUOUS,
    REQUIREMENT_MATCHED,
    REQUIREMENT_UNMATCHED_TOPK,
    requirement_evidence_state,
    requirement_name_matches_fact,
)
from schemas.table_names import TABLE_IS, TABLE_NOTE
from tools.evidence import (
    clear_runtime_evidence_cache,
    filter_facts_for_query,
    narrative_atom_gap_retry_query,
    narrative_requirement_evidence_state,
    result_to_facts,
)
from tools.query_routing import parse_query_slots, targeted_retry_query


SEMANTIC_FIELDS = (
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
)


@pytest.mark.parametrize(
    "query",
    [
        "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS là bao nhiêu?",
        "Bán hàng hóa cho bên liên quan Công ty Cổ phần APIS là bao nhiêu?",
        "Khoản hoàn nhập trong năm là bao nhiêu?",
        "Doanh thu tại khu vực nước ngoài?",
        "Thời gian khấu hao máy móc và thiết bị là bao nhiêu?",
    ],
)
def test_targeted_retry_roundtrips_every_explicit_semantic_dimension(query):
    original = parse_query_slots(query)
    retry_query = targeted_retry_query(query)
    retried = parse_query_slots(retry_query)

    assert retry_query
    for field in SEMANTIC_FIELDS:
        assert getattr(retried, field) == getattr(original, field), (
            query,
            retry_query,
            field,
        )


@pytest.mark.parametrize(
    ("query", "fact"),
    [
        (
            "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS là bao nhiêu?",
            {
                "item_name": (
                    "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS"
                ),
                "metric_label": "Mua hàng hóa",
                "entity_label": "Công ty Cổ phần APIS",
                "counterparty": "Công ty Cổ phần APIS",
                "transaction_type": "sale",
                "aggregation_level": "component",
            },
        ),
        (
            "Khoản hoàn nhập trong năm là bao nhiêu?",
            {
                "item_name": "Khoản hoàn nhập trong năm",
                "row_label": "Khoản hoàn nhập trong năm",
                "movement_type": "provision_charge",
            },
        ),
        (
            "Doanh thu tại khu vực nước ngoài?",
            {
                "item_name": "Doanh thu tại khu vực nước ngoài",
                "metric_label": "Doanh thu",
                "entity_label": "Nước ngoài",
                "geography": "Trong nước",
                "aggregation_level": "component",
            },
        ),
        (
            "Thời gian khấu hao máy móc là bao nhiêu?",
            {
                "item_name": "Thời gian khấu hao máy móc",
                "metric_label": "Thời gian khấu hao",
                "entity_label": "máy móc",
                "policy_topic": "depreciation_method",
                "aggregation_level": "component",
            },
        ),
    ],
)
def test_semantic_slot_conflict_cannot_close_requirement_lexically(
    query,
    fact,
):
    candidate = {
        "table": TABLE_NOTE,
        "value": "100",
        "status": "found",
        **fact,
    }

    assert not requirement_name_matches_fact(
        query,
        candidate,
        table=TABLE_NOTE,
    )
    assert (
        requirement_evidence_state(query, [candidate], table=TABLE_NOTE)
        == REQUIREMENT_UNMATCHED_TOPK
    )
    filtered = filter_facts_for_query(
        [candidate],
        table=TABLE_NOTE,
        query=query,
    )
    assert filtered[0]["status"] == "not_found_after_search"
    assert filtered[0]["evidence_state"] == "unmatched_topk"


def test_document_placeholder_without_canonical_value_never_becomes_fact():
    facts = result_to_facts(
        {
            "documents": ["Doanh thu tại khu vực nước ngoài: 100"],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Doanh thu tại khu vực nước ngoài",
                    "geography": "Nước ngoài",
                }
            ],
        },
        table=TABLE_NOTE,
        query="Doanh thu tại khu vực nước ngoài?",
    )

    assert facts[0]["value"] == ""
    assert facts[0]["status"] == "not_found_after_search"
    assert facts[0]["evidence_state"] == "unmatched_topk"


def test_legacy_fact_with_explicit_value_remains_usable_without_fact_id():
    facts = result_to_facts(
        {
            "documents": ["Doanh thu tại khu vực nước ngoài: 100"],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Doanh thu tại khu vực nước ngoài",
                    "metric_label": "Doanh thu",
                    "entity_label": "Nước ngoài",
                    "geography": "Nước ngoài",
                    "raw_value": "100",
                }
            ],
        },
        table=TABLE_NOTE,
        query="Doanh thu tại khu vực nước ngoài?",
    )

    assert facts[0]["value"] == "100"
    assert facts[0]["status"] == "found"
    assert facts[0]["evidence_state"] == "matched"


def test_result_to_facts_keeps_topk_listing_events_for_narrative_premise():
    query = "sự kiện cấp phép và niêm yết cổ phiếu"
    license_event = (
        "Ngày 28/12/2005: Ủy Ban Chứng khoán Nhà nước cấp Giấy phép "
        "niêm yết số 42/UBCK-GPNY."
    )
    listing_event = (
        "Ngày 19/1/2006: Cổ phiếu của Công ty được niêm yết trên "
        "Sở Giao dịch Chứng khoán Thành phố Hồ Chí Minh."
    )
    valuation_policy = (
        "Đối với chứng khoán niêm yết, giá đóng cửa được dùng để xác "
        "định giá trị hợp lý."
    )

    facts = result_to_facts(
        {
            "documents": [
                license_event,
                listing_event,
                valuation_policy,
            ],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Niêm yết",
                    "metric_label": "Niêm yết",
                    "raw_value": license_event,
                    "fact_id": "listing-license",
                    "source": "report.md#page=12",
                },
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Niêm yết",
                    "metric_label": "Niêm yết",
                    "raw_value": listing_event,
                    "fact_id": "listing-event",
                    "source": "report.md#page=12",
                },
                {
                    # Even an exact route-like label is not proof of an event.
                    "heading": TABLE_NOTE,
                    "item_name": query,
                    "metric_label": query,
                    "raw_value": valuation_policy,
                    "fact_id": "listing-valuation-policy",
                    "source": "report.md#page=18",
                },
            ],
        },
        table=TABLE_NOTE,
        query=query,
        limit=10,
        semantic_requirement=query,
    )

    assert [fact["fact_id"] for fact in facts] == [
        "listing-license",
        "listing-event",
    ]
    assert all(fact["status"] == "found" for fact in facts)


def test_narrative_requirement_needs_usable_full_atom_coverage():
    query = "sự kiện cấp phép và niêm yết cổ phiếu"
    license_fact = {
        "item_name": query,
        "value": (
            "Ngày 28/12/2005: Ủy Ban Chứng khoán Nhà nước cấp Giấy "
            "phép niêm yết số 42/UBCK-GPNY."
        ),
        "fact_id": "listing-license",
        "source": "report.md#page=12",
        "status": "found",
    }
    listing_fact = {
        "item_name": query,
        "value": (
            "Ngày 19/1/2006: Cổ phiếu của Công ty được niêm yết trên "
            "Sở Giao dịch Chứng khoán."
        ),
        "fact_id": "listing-event",
        "source": "report.md#page=12",
        "status": "found",
    }

    # An exact route-like label cannot close a two-atom premise when the actual
    # payload proves only that a licence was issued.
    assert (
        narrative_requirement_evidence_state(query, [license_fact])
        == REQUIREMENT_UNMATCHED_TOPK
    )
    assert narrative_atom_gap_retry_query(query, [license_fact]) == (
        "cổ phiếu chính thức niêm yết sở giao dịch chứng khoán"
    )
    assert (
        narrative_requirement_evidence_state(
            query,
            [{**listing_fact, "status": "ambiguous"}, license_fact],
        )
        == REQUIREMENT_AMBIGUOUS
    )
    assert (
        narrative_requirement_evidence_state(
            query,
            [license_fact, listing_fact],
        )
        == REQUIREMENT_MATCHED
    )


def test_explicit_grounded_internal_control_premise_uses_semantic_state():
    query = "mô tả hệ thống kiểm soát nội bộ"
    fact = {
        "value": (
            "Hệ thống kiểm soát nội bộ được thiết lập để giám sát các "
            "quy trình tài chính trọng yếu."
        ),
        "fact_id": "internal-control-description",
        "source": "report.md#page=20",
        "status": "found",
    }

    assert (
        narrative_requirement_evidence_state(query, [fact])
        == REQUIREMENT_MATCHED
    )


def test_partial_narrative_atoms_trigger_unstructured_targeted_retry(
    monkeypatch,
):
    clear_runtime_evidence_cache()
    query = "sự kiện cấp phép và niêm yết cổ phiếu"
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            value = (
                "Ngày 28/12/2005: Ủy Ban Chứng khoán Nhà nước cấp "
                "Giấy phép niêm yết số 42/UBCK-GPNY."
            )
            fact_id = "listing-license"
        else:
            value = (
                "Ngày 19/1/2006: Cổ phiếu của Công ty được niêm yết "
                "trên Sở Giao dịch Chứng khoán."
            )
            fact_id = "listing-event"
        return {
            "documents": [value],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": query,
                    "raw_value": value,
                    "fact_id": fact_id,
                    "source": "report.md#page=12",
                }
            ],
            "source": "report.md",
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "dataset_id": "narrative-partial-retry",
            "user_query": "Ý nghĩa của quá trình chuyển đổi là gì?",
            "planner_plan": {
                "response_mode": "grounded_interpretation",
                "premise_requirements": [query],
            },
            "worker_plan": {
                "response_mode": "grounded_interpretation",
                "premise_requirements": [query],
                "evidence_plan": [
                    {"table": TABLE_NOTE, "query": query, "needby": []}
                ],
                "analysis_plan": [],
            },
        }
    )

    assert len(calls) == 2
    assert all(call["structured_slots"] is False for call in calls)
    assert calls[1]["query"] == (
        "cổ phiếu chính thức niêm yết sở giao dịch chứng khoán"
    )
    assert calls[1]["intent"] == calls[1]["query"]
    entry = updates["evidence_ledger"]["entries"][0]
    assert entry["requirement_state"] == {
        "before_retry": REQUIREMENT_UNMATCHED_TOPK,
        "after_retry": REQUIREMENT_MATCHED,
    }
    assert {
        fact["fact_id"]
        for fact in updates["worker_results"][TABLE_NOTE]["facts"]
    } == {"listing-license", "listing-event"}


def test_missing_registration_atom_drives_exact_narrative_retry(monkeypatch):
    clear_runtime_evidence_cache()
    query = "sự kiện cổ phần hóa và đăng ký công ty cổ phần"
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            value = (
                "Ngày 01/10/2003: Công ty được cổ phần hóa từ doanh "
                "nghiệp nhà nước."
            )
            fact_id = "corporatization"
        else:
            value = (
                "Ngày 20/11/2003: Công ty đăng ký trở thành một công "
                "ty cổ phần."
            )
            fact_id = "joint-stock-registration"
        return {
            "documents": [value],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": query,
                    "raw_value": value,
                    "fact_id": fact_id,
                    "source": "report.md#page=12",
                }
            ],
            "source": "report.md",
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "dataset_id": "narrative-registration-gap-retry",
            "user_query": "Ý nghĩa của quá trình chuyển đổi là gì?",
            "planner_plan": {
                "response_mode": "grounded_interpretation",
                "premise_requirements": [query],
            },
            "worker_plan": {
                "response_mode": "grounded_interpretation",
                "premise_requirements": [query],
                "evidence_plan": [
                    {"table": TABLE_NOTE, "query": query, "needby": []}
                ],
                "analysis_plan": [],
            },
        }
    )

    assert len(calls) == 2
    assert calls[1]["query"] == "đăng ký doanh nghiệp công ty cổ phần"
    assert calls[1]["intent"] == calls[1]["query"]
    assert calls[1]["structured_slots"] is False
    entry = updates["evidence_ledger"]["entries"][0]
    assert entry["requirement_state"] == {
        "before_retry": REQUIREMENT_UNMATCHED_TOPK,
        "after_retry": REQUIREMENT_MATCHED,
    }
    assert {
        fact["fact_id"]
        for fact in updates["worker_results"][TABLE_NOTE]["facts"]
    } == {"corporatization", "joint-stock-registration"}


def test_listing_price_lookup_preserves_policy_without_grounded_opt_in():
    query = (
        "Giá đóng cửa của chứng khoán niêm yết dùng để xác định giá trị "
        "hợp lý như thế nào?"
    )
    listing_event = (
        "Ngày 19/1/2006: Cổ phiếu của Công ty được niêm yết trên "
        "Sở Giao dịch Chứng khoán."
    )
    valuation_policy = (
        "Giá đóng cửa của chứng khoán niêm yết được dùng để xác định "
        "giá trị hợp lý."
    )

    facts = result_to_facts(
        {
            "documents": [listing_event, valuation_policy],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Niêm yết",
                    "metric_label": "Niêm yết",
                    "raw_value": listing_event,
                    "fact_id": "listing-event",
                    "source": "report.md#page=12",
                },
                {
                    "heading": TABLE_NOTE,
                    "item_name": query,
                    "metric_label": query,
                    "raw_value": valuation_policy,
                    "fact_id": "listing-valuation-policy",
                    "source": "report.md#page=18",
                },
            ],
        },
        table=TABLE_NOTE,
        query=query,
        limit=10,
    )

    assert "listing-valuation-policy" in {
        fact["fact_id"] for fact in facts
    }


def test_requirement_normalization_cannot_erase_specific_policy_entity():
    query = (
        "bất động sản đầu tư - quyền sử dụng đất lâu dài được xử lý "
        "khấu hao như thế nào"
    )
    facts = filter_facts_for_query(
        [
            {
                "table": TABLE_NOTE,
                "item_name": "Chính sách không khấu hao",
                "metric_label": "Chính sách không khấu hao",
                "entity_label": "Quyền sử dụng đất lâu dài",
                "scope_label": "(a) Quyền sử dụng đất",
                "policy_topic": "non_depreciation",
                "aggregation_level": "component",
                "value": "Không tính khấu hao.",
                "status": "found",
            },
            {
                "table": TABLE_NOTE,
                "item_name": "Khấu hao bất động sản đầu tư",
                "metric_label": "Phương pháp khấu hao",
                "entity_label": "Bất động sản đầu tư",
                "scope_label": "Bất động sản đầu tư",
                "policy_topic": "depreciation_method",
                "aggregation_level": "component",
                "value": "Đường thẳng trong 49 năm.",
                "status": "found",
            },
        ],
        table=TABLE_NOTE,
        query=query,
    )

    assert len(facts) == 1
    assert facts[0]["entity_label"] == "Quyền sử dụng đất lâu dài"
    assert facts[0]["policy_topic"] == "non_depreciation"


def test_targeted_retry_uses_original_intent_and_typed_matching(monkeypatch):
    clear_runtime_evidence_cache()
    calls = []
    query = (
        "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS là bao nhiêu?"
    )

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        transaction_type = "sale" if len(calls) == 1 else "purchase"
        return {
            "documents": ["Mua hàng hóa từ bên liên quan APIS: 100"],
            "metadatas": [
                {
                    "heading": TABLE_NOTE,
                    "item_name": "Mua hàng hóa | Công ty Cổ phần APIS",
                    "metric_label": "Mua hàng hóa",
                    "entity_label": "Công ty Cổ phần APIS",
                    "scope_label": "Bên liên quan",
                    "counterparty": "Công ty Cổ phần APIS",
                    "transaction_type": transaction_type,
                    "aggregation_level": "component",
                    "raw_value": "100",
                    "fact_id": (
                        "transaction-sale"
                        if transaction_type == "sale"
                        else "transaction-purchase"
                        ),
                        "source": "report.md",
                        "rerank_score": 100.0 + len(calls),
                        "score_query": kwargs["query"],
                        "score_intent": kwargs.get("intent") or kwargs["query"],
                        "retrieval_origin": "hybrid",
                }
            ],
            "source": "report.md",
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [{"table": TABLE_NOTE, "query": query}],
            },
            "dataset_id": "semantic-targeted-retry-intent",
            "user_query": query,
        }
    )

    assert len(calls) == 2
    assert calls[1]["query"] == targeted_retry_query(query)
    assert calls[1]["intent"] == query
    facts = updates["worker_results"][TABLE_NOTE]["facts"]
    assert any(
        fact.get("transaction_type") == "purchase"
        and fact.get("status") == "found"
        for fact in facts
    )
    ledger = updates["evidence_ledger"]
    assert extract_evidence_ledger(updates) == ledger
    assert evidence_ledger_contract_errors(ledger) == []
    assert len(ledger["entries"]) == 1
    entry = ledger["entries"][0]
    assert entry["kind"] == "retrieval_requirement"
    assert entry["parsed_query_slots"]["transaction_type"] == "purchase"
    assert entry["parsed_query_slots"]["counterparty"]
    assert [candidate["rank"] for candidate in entry["route_candidates"]] == list(
        range(1, len(entry["route_candidates"]) + 1)
    )
    assert all(
        candidate["reason"] and candidate["confidence"] is not None
        for candidate in entry["route_candidates"]
    )
    assert entry["requirement_state"] == {
        "before_retry": "unmatched_topk",
        "after_retry": "matched",
    }
    assert entry["targeted_retry"] == {
        "performed": True,
        "query": targeted_retry_query(query),
    }
    assert entry["selected_facts"][0]["fact_id"] == "transaction-purchase"
    assert entry["selected_facts"][0]["rank"] == 1
    assert entry["selected_facts"][0]["rerank_score"] == 102.0
    assert entry["selected_facts"][0]["score_query"] == targeted_retry_query(
        query
    )
    assert entry["selected_facts"][0]["retrieval_origin"] == "hybrid"
    assert "score" not in entry["selected_facts"][0]


def test_no_retry_requirement_persists_stable_ledger_transition(monkeypatch):
    clear_runtime_evidence_cache()
    calls = []
    query = "Chi phí bán hàng là bao nhiêu?"

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        return {
            "documents": ["Chi phí bán hàng: 20"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": "Chi phí bán hàng",
                    "metric_label": "Chi phí bán hàng",
                    "raw_value": "20",
                        "fact_id": "selling-expense-current",
                        "source": "report.md",
                        "rerank_score": 88.0,
                        "score_query": kwargs["query"],
                        "score_intent": kwargs.get("intent") or kwargs["query"],
                        "retrieval_origin": "hybrid",
                }
            ],
            "source": "report.md",
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    updates = evidence_node.build_evidence_pack(
        {
            "worker_plan": {
                "evidence_plan": [{"table": TABLE_IS, "query": query}],
            },
            "dataset_id": "ledger-no-retry",
            "user_query": query,
        }
    )

    assert len(calls) == 1
    ledger = extract_evidence_ledger(updates)
    assert evidence_ledger_contract_errors(ledger) == []
    entry = ledger["entries"][0]
    assert entry["parsed_query_slots"]["metric"] == "chi phí bán hàng"
    assert entry["requirement_state"] == {
        "before_retry": "matched",
        "after_retry": "matched",
    }
    assert entry["targeted_retry"] == {"performed": False, "query": ""}
    assert entry["selected_route"]["table"] == TABLE_IS
    assert entry["selected_facts"] == [
        {
            "fact_id": "selling-expense-current",
            "rank": 1,
            "status": "found",
            "evidence_state": "matched",
            "source": "report.md",
            "rerank_score": 88.0,
            "score_query": query,
            "score_intent": query,
            "retrieval_origin": "hybrid",
        }
    ]
