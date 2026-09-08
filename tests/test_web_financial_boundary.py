"""External prose remains visible but cannot become report arithmetic evidence."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents import synth_runner as sr
from agents.agent_runner import _compact_analysis_fact_for_prompt
from schemas.evidence_origin import is_report_fact
from schemas.web_evidence import WebEvidence


@pytest.fixture
def web_fact():
    return WebEvidence(title="VNM lãi 2.500.000.000.000 đồng", content="Tin doanh nghiệp.",
                       source_url="https://vietstock.vn/2026/news.htm",
                       publisher="Vietstock", ticker="VNM").as_fact()


@pytest.mark.parametrize("compact", [False, True])
def test_web_cannot_supply_calculation_or_grounding_but_keeps_provenance(web_fact, compact):
    wr = {"WEB": {"facts": [web_fact]}}
    if compact:
        normalized, _ = sr._normalize_all_worker_results(wr)
        wr, _ = sr._build_compact_worker_results(normalized)
        wr = {"retrieval_facts": wr}
    assert sr._calculation_facts(wr) == []
    assert sr._grounded_valid_facts(wr) == []
    facts = list(sr._iter_retrieval_facts(wr))
    assert len(facts) == 1
    assert facts[0]["content_type"] == "web_fact"
    assert facts[0]["source_url"] == web_fact["source_url"]
    assert facts[0]["retrieved_at"] == web_fact["retrieved_at"]
    projected = _compact_analysis_fact_for_prompt(facts[0])
    assert projected["source_url"] == web_fact["source_url"]


def test_web_cannot_supply_canonical_report_lookup(web_fact, monkeypatch):
    monkeypatch.setattr(sr, "_difficulty_level_from_state", lambda _: "easy")
    monkeypatch.setattr(sr, "parse_query_slots", lambda _: SimpleNamespace(operation="lookup", metric="profit", entity=""))
    monkeypatch.setattr(sr, "fact_matches_required_slots", lambda *_: True)
    monkeypatch.setattr(sr, "fact_slot_score", lambda *_: 1)
    assert sr._canonical_lookup_candidate({}, {"WEB": {"facts": [web_fact]}}) is None
    report = {"fact_id": "report-1", "value": "123", "source": "BCTC"}
    assert sr._canonical_lookup_candidate({}, {"BCTC": {"facts": [report]}})["value"] == "123"


def test_mixed_web_and_report_preserve_exact_report_operands(web_fact):
    from schemas.financial_validation import iter_financial_facts
    report = {"fact_id": "report-1", "value": "123", "source": "BCTC", "content_type": "table_fact"}
    base = {"BCTC": {"facts": [report]}}
    mixed = {**base, "WEB": {"facts": [web_fact]}}
    assert sr._calculation_facts(base) == sr._calculation_facts(mixed) == [(report, Decimal(123))]
    assert sr._grounded_valid_facts(base) == sr._grounded_valid_facts(mixed)
    assert list(iter_financial_facts(mixed)) == [report]


def test_legacy_web_container_remains_external_after_metadata_loss():
    stripped = {"fact_id": "news-1", "source": "Vietstock", "value": "2500000"}
    assert sr._calculation_facts({"WEB": {"facts": [stripped]}}) == []
    assert not is_report_fact({**stripped, "source_kind": "unknown"})
    assert not is_report_fact({**stripped, "content_type": "unknown"})


def test_analysis_empty_scope_does_not_fall_back_to_global_web(web_fact):
    from agents.agent_runner import _analysis_input_results
    fact = {**web_fact, "needby": ["agent_profitability"]}
    state = {"worker_results": {"WEB": {"facts": [fact]}}}
    assert "WEB" in _analysis_input_results(state, "agent_profitability")
    assert _analysis_input_results(state, "agent_cashflow_analysis") == {}
    state["analysis_input_results"] = {}
    assert _analysis_input_results(state, "agent_profitability") == {}
