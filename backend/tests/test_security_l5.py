"""Phase 2, Việc 3: structured WEB facts, configuration E and the L5 protocols."""

from __future__ import annotations

from agentfinx.core.schemas.financial_validation import parse_financial_decimal
from agentfinx.core.schemas.web_evidence import WebEvidence
from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import l5, runner, worker
from agentfinx.evaluation.security import protocol as protocol_module

CALC = cases_module.load_cases("benchmarks/security/calc_set.json")


def test_calc_set_needs_a_calculation_and_fakes_one_operand():
    ids = CALC.all_case_ids()
    assert len(ids) == 40 and len(CALC.case_ids(split="dev")) == 10 and len(CALC.case_ids(split="test")) == 30
    main_ids = set(cases_module.load_cases("benchmarks/security/attack_set.json").all_case_ids())
    assert not set(ids) & main_ids
    for case_id in ids:
        case = CALC.case(case_id)
        assert case["answer_key"]["derived_required"] and case["answer_key"]["derived"]
        assert len(case["attacker_target"]["facts"]) == 1


def test_structured_fact_keeps_web_provenance_and_only_the_number():
    article = WebEvidence(ticker="S72", company="Sông Đà 7.02", title="Tin thử",
                          content="Ngày 15/10/2026, lợi nhuận gộp 18.554.529.218 đồng.",
                          source_url="https://tin.invalid/x-main", publisher="Bản tin")
    figure = {"key": "gross_profit_current", "value": "18554529218", "table": "KQKD", "period_role": "current",
              "fiscal_year": "2024", "source_kind": "report", "content_type": "table_fact", "source_url": "x"}
    fact = article.as_structured_fact(figure)
    assert (fact["source_kind"], fact["content_type"], fact["source_url"]) == ("web", "web_fact",
                                                                               "https://tin.invalid/x-main")
    assert fact["value"] == fact["parsed_value"] == "18554529218" and "15/10" not in fact["evidence_text"]
    assert parse_financial_decimal(fact["parsed_value"]) == 18554529218
    assert (fact["table"], fact["period_role"], fact["fiscal_year"]) == ("KQKD", "current", "2024")


def test_structured_figures_copy_the_faked_operand_identity():
    case = CALC.case("CI1_S72_001")
    (figure,) = l5.structured_figures(case)
    operand = next(o for o in case["answer_key"]["operands"] if o["key"] == figure["key"])
    assert figure["value"] == case["attacker_target"]["facts"][0]["value"] != operand["value"]
    assert (figure["table"], figure["row_label"], figure["column_label"]) == (
        operand["source"]["table"], operand["source"]["row_label"], operand["source"]["column"])
    assert figure["article_url"].endswith("-main")


def test_config_e_and_structured_exposure():
    assert worker.CONFIG_FLAGS["E"] == {"prompt_guards_off": True, "provenance_off": False}
    assert runner.config_env("E") == {"AGENTFINX_ABLATE_PROMPT_GUARDS": "1", "AGENTFINX_ABLATE_PROVENANCE": "0"}
    assert runner.EXPOSURES["structured"]["WEB_ANALYSIS_ENABLED"] == "0"
    main, _, _ = protocol_module.load("benchmarks/security/main_protocol.json")
    assert "configurations differ from A/B/C/D" not in protocol_module.problems(main, protocol_module.attack_set(main))


def test_l5_pilot_protocol_is_valid_and_guards_its_inputs():
    pilot, _, _ = protocol_module.load("benchmarks/security/l5_pilot_protocol.json")
    cases = protocol_module.attack_set(pilot)
    assert protocol_module.exposure(pilot) == "structured"
    assert protocol_module.problems(pilot, cases) == []
    assert set(pilot["case_ids"]) == set(cases.case_ids(split="dev"))
    broken = {**pilot, "configs": {**pilot["configs"], "A": {"prompt_guards_off": False, "provenance_off": False}},
              "case_ids": pilot["case_ids"] + cases.case_ids(split="test")[:1]}
    problems = " | ".join(protocol_module.problems(broken, cases))
    assert "config A" in problems and "case ids" in problems
