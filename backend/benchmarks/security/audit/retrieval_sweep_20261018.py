"""Net revenue / gross profit retrieval on every dataset (no LLM call).

For each dataset, retrieve the income-statement line with the standard keyword
and check that the attack set's gold value is among the matched facts.
"""

import json
from decimal import Decimal

from agentfinx.agents.tools.evidence import filter_facts_for_query, result_to_facts
from agentfinx.agents.tools.tools import get_related_info
from agentfinx.core.schemas.requirements import requirement_evidence_state
from agentfinx.datasets.registry import get_dataset
from agentfinx.workflow.pipeline import ensure_built

SET = "/Users/thanhhai/itec/backend/benchmarks/security/attack_set.json"
TABLE = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
KEYWORDS = {"revenue_current": "doanh thu thuần về bán hàng và cung cấp dịch vụ",
            "gross_profit_current": "lợi nhuận gộp về bán hàng và cung cấp dịch vụ"}

data = json.load(open(SET, encoding="utf-8"))
gold = {}
questions = {}
METRIC_OF = {"revenue_current": "revenue", "gross_profit_current": "gross_profit"}
for case in data["cases"]:
    for fact in case["gold"]["facts"] + case["gold"]["operands"]:
        if fact["key"] in KEYWORDS:
            gold[(case["dataset_id"], fact["key"])] = Decimal(fact["value"])
            if case["metric"] in (METRIC_OF[fact["key"]], "gross_margin"):
                kind = "direct" if case["metric"] == METRIC_OF[fact["key"]] else "margin"
                questions.setdefault((case["dataset_id"], fact["key"], kind), case["question"])


def parsed(value):
    text = str(value or "").replace(".", "").replace(",", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        return -Decimal(text) if negative else Decimal(text)
    except Exception:
        return None


bad = 0
for dataset_id in sorted({d for d, _ in gold}):
    dataset, conn, collection = ensure_built(get_dataset(dataset_id))
    for key, keyword in KEYWORDS.items():
        for kind in ("direct", "margin"):
            if (dataset_id, key, kind) not in questions:
                continue
            raw = get_related_info(query=keyword, table=TABLE, collection=collection, strict_table=False,
                                   limit=8, intent=questions[(dataset_id, key, kind)], structured_slots=True)
            facts = result_to_facts(raw, table=TABLE, query=keyword, limit=8)
            state = requirement_evidence_state(keyword, facts, table=TABLE)
            values = {parsed(f.get("value")) for f in facts if f.get("status") == "found"}
            ok = state == "matched" and gold[(dataset_id, key)] in values
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {dataset_id:12s} {key:22s} {kind:6s} state={state:16s} gold={gold[(dataset_id, key)]}")
print("failures:", bad)
