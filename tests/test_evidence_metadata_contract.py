"""Retrieval facts retain the fields required by deterministic recall."""

from tools.evidence import result_to_facts


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
