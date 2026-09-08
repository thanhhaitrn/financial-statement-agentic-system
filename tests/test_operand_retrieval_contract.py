"""End-to-end contracts for independently retrieved calculation operands."""

from decimal import Decimal

from agents import keyworder_runner, synth_runner
from schemas.table_names import TABLE_BS
from tools.evidence import result_to_facts
from tools.query_routing import parse_query_slots
from tools.tools import get_related_info


class StructuredBalanceCollection:
    """Small exact-slot collection; dense fallback is a test failure."""

    name = "structured-balance-operands"

    def __init__(self, rows):
        self.rows = list(rows)
        self.query_calls = 0
        self.get_calls = []

    def get(self, where=None, include=None):
        where = dict(where or {})
        self.get_calls.append(where)
        selected = [
            (document, metadata)
            for document, metadata in self.rows
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
        raise AssertionError(
            "complete typed operands must resolve before dense fallback"
        )


def _statement_fact(
    *,
    fact_id,
    item_code,
    item_name,
    value,
    aggregation_level,
    section_key,
):
    metadata = {
        "company": "Công ty kiểm thử",
        "fiscal_year": "2025",
        "heading": TABLE_BS,
        "fact_id": fact_id,
        "item_code": item_code,
        "item_name": f"{item_name} | 31/12/2025 VND",
        "row_label": item_name,
        "column_label": "31/12/2025 VND",
        "metric_label": item_name,
        "period": "cuối",
        "period_label": "31/12/2025 VND",
        "period_role": "current",
        "aggregation_level": aggregation_level,
        "section_key": section_key,
        "raw_value": value,
        "normalized_value": value,
        "parsed_value": value.replace(".", ""),
        "unit": "VND",
        "source": "report.md#page=8",
    }
    return (
        f"{metadata['item_name']}: {value} VND",
        metadata,
    )


def test_debt_equity_operands_retrieve_code_300_and_400_before_binding():
    query = (
        "Tính hệ số nợ trên vốn chủ sở hữu (D/E) của Công ty "
        "tại 31/12/2025."
    )
    contract = keyworder_runner._typed_calculation_metadata(query, TABLE_BS)
    collection = StructuredBalanceCollection(
        [
            _statement_fact(
                fact_id="code-300",
                item_code="300",
                item_name="Tổng nợ phải trả",
                value="400",
                aggregation_level="total",
                section_key="no_phai_tra",
            ),
            _statement_fact(
                fact_id="code-310",
                item_code="310",
                item_name="Tổng nợ ngắn hạn",
                value="350",
                aggregation_level="total",
                section_key="no_ngan_han",
            ),
            _statement_fact(
                fact_id="code-400",
                item_code="400",
                item_name="Tổng vốn chủ sở hữu",
                value="800",
                aggregation_level="total",
                section_key="von_chu",
            ),
            # Same displayed value and a closer bare lexical label: this is the
            # regression distractor that used to replace code 400.
            _statement_fact(
                fact_id="code-410",
                item_code="410",
                item_name="Vốn chủ sở hữu",
                value="800",
                aggregation_level="component",
                section_key="",
            ),
        ]
    )

    facts = []
    for operand in contract["operands"]:
        operand_slots = parse_query_slots(operand["query"])
        assert operand_slots.aggregation == "total"
        assert operand_slots.section_key == operand["section_key"]

        result = get_related_info(
            query=operand["query"],
            table=TABLE_BS,
            collection=collection,
            limit=5,
            intent=operand["query"],
        )
        assert result["retrieval_mode"] == "structured_slots"
        facts.extend(
            result_to_facts(
                result,
                table=TABLE_BS,
                query=operand["query"],
                limit=5,
            )
        )

    assert collection.query_calls == 0
    assert [fact["fact_id"] for fact in facts] == ["code-300", "code-400"]

    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": query,
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "table": TABLE_BS,
                        "query": query,
                        **contract,
                    }
                ]
            },
        },
        {"balance-sheet": {"facts": facts}},
    )

    assert calculation is not None
    assert [
        operand["fact_id"] for operand in calculation["operands"]
    ] == ["code-300", "code-400"]
    assert Decimal(calculation["result"]) == Decimal("0.5")
