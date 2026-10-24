"""Income-statement lines named differently by a report, and margin operands.

Regressions from the security dev check (18/10/2026): a service-only company
(Hòa Phát's separate report: "Doanh thu thuần về cung cấp dịch vụ") lost its
revenue and gross-profit lines; a gross-margin question was answered on gross
revenue (line 01) or without any revenue at all.
"""

import agentfinx.agents.tools.tools as tools_module
from agentfinx.agents import keyworder_runner
from agentfinx.agents.tools.query_routing import (
    fact_matches_required_slots,
    metric_slot_compatibility,
    parse_query_slots,
)
from agentfinx.core.config.allowed_keywords import TABLE_IS
from agentfinx.core.schemas.requirements import requirement_evidence_state

NET_REVENUE = "doanh thu thuần về bán hàng và cung cấp dịch vụ"
GROSS_PROFIT = "lợi nhuận gộp về bán hàng và cung cấp dịch vụ"
GROSS_REVENUE = "doanh thu bán hàng và cung cấp dịch vụ"
MARGIN_QUESTION = "Biên lợi nhuận gộp năm 2025 của Geru là bao nhiêu phần trăm?"


def _service_only_fact(label: str, value: str) -> dict:
    return {
        "table": TABLE_IS,
        "item_name": f"{label} | Kỳ này",
        "metric_label": label,
        "row_label": label,
        "column_label": "Kỳ này",
        "period_label": "Kỳ này",
        "scope_label": TABLE_IS,
        "item_code": "10",
        "value": value,
        "status": "found",
    }


def test_statement_line_names_carry_no_sale_direction():
    assert parse_query_slots(NET_REVENUE).transaction_type == ""
    assert parse_query_slots(GROSS_PROFIT).transaction_type == ""
    # A real direction is kept.
    assert parse_query_slots(f"{NET_REVENUE} cho Công ty Cổ phần Beta").transaction_type == "sale"
    assert parse_query_slots(GROSS_REVENUE).transaction_type == "sale"
    assert parse_query_slots("doanh thu bán thành phẩm 2025").transaction_type == "sale"


def test_service_only_revenue_and_gross_profit_lines_match_the_standard_keywords():
    revenue = _service_only_fact("Doanh thu thuần về cung cấp dịch vụ (10=01-02)", "177.661.419.641")
    gross_profit = _service_only_fact("Lợi nhuận gộp về cung cấp dịch vụ (20=10-11)", "51.136.046.880")

    assert fact_matches_required_slots(parse_query_slots(NET_REVENUE), revenue)
    assert metric_slot_compatibility(GROSS_PROFIT, gross_profit["metric_label"])[0]
    assert requirement_evidence_state(NET_REVENUE, [revenue], table=TABLE_IS) == "matched"
    assert requirement_evidence_state(GROSS_PROFIT, [gross_profit], table=TABLE_IS) == "matched"


def test_margin_operand_retrieval_binds_to_the_operand_metric(monkeypatch):
    seen = []

    def capture(collection, slots, table):
        seen.append(slots.metric)
        return [], []

    class Empty:
        name = "empty"

        def query(self, query_embeddings, n_results, where=None):
            return {"documents": [[]], "metadatas": [[]]}

        def get(self, where=None, include=None):
            return {"documents": [], "metadatas": []}

    monkeypatch.setattr(tools_module, "_structured_slot_candidates", capture)
    monkeypatch.setattr(tools_module, "embed_query_text", lambda _q: [0.0])

    tools_module.get_related_info(NET_REVENUE, TABLE_IS, Empty(), intent=MARGIN_QUESTION)
    tools_module.get_related_info(GROSS_PROFIT, TABLE_IS, Empty(), intent=MARGIN_QUESTION)
    tools_module.get_related_info(
        NET_REVENUE, TABLE_IS, Empty(), intent="Doanh thu thuần năm trước của Geru là bao nhiêu?"
    )

    # Revenue keeps its own metric; gross profit and a same-metric intent keep
    # the intent's slots (with its period words).
    assert seen == [NET_REVENUE, "lợi nhuận gộp", NET_REVENUE]


def test_margin_question_asks_for_net_revenue_not_gross_revenue():
    items = [{"table": TABLE_IS, "queries": ["lợi nhuận gộp", GROSS_REVENUE],
              "canonical_queries": {GROSS_REVENUE: GROSS_REVENUE}}]

    rewritten = keyworder_runner._margin_revenue_as_net(items, MARGIN_QUESTION)
    assert rewritten[0]["queries"] == ["lợi nhuận gộp", NET_REVENUE]
    assert rewritten[0]["canonical_queries"] == {NET_REVENUE: NET_REVENUE}

    explicit = "Biên lợi nhuận gộp tính trên doanh thu bán hàng và cung cấp dịch vụ là bao nhiêu?"
    assert keyworder_runner._margin_revenue_as_net(items, explicit) == items
    assert keyworder_runner._margin_revenue_as_net(items, "Doanh thu bán hàng năm 2025 là bao nhiêu?") == items


def test_router_finalize_applies_the_net_revenue_rule():
    plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [{"table": TABLE_IS, "queries": ["lợi nhuận gộp", GROSS_REVENUE]}]},
        {"difficulty_level": "medium"},
        user_query=MARGIN_QUESTION,
    )
    queries = [q for item in plan["evidence_plan"] for q in (item.get("queries") or [item.get("query")])]
    assert NET_REVENUE in queries
    assert GROSS_REVENUE not in queries
