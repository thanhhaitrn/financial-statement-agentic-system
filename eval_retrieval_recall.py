"""Deterministic gold-number recall harness for the retrieval layer.

Bypasses the stochastic planner/keyworder: for each seed question it issues
fixed get_related_info calls over the four statement tables, replays the
production post-filter chain (result_to_facts -> per-table fact cap), and
measures how many ground-truth numbers survive into the fact texts. No LLM in
the loop, so an A/B between two env configs isolates the mechanism under test
from the RAGAS batch variance floor (±0.05-0.08).

Usage:
  python eval_retrieval_recall.py --ids 185,186,198,200,201,209
  EVIDENCE_FACTS_LIMIT=10 NOTE_FACTS_LIMIT=12 python eval_retrieval_recall.py

Env knobs are read at pipeline-module import time; run one process per config.
"""

import argparse
import json
import re
import sys

GOLD_NUMBER_RE = re.compile(r"\d{1,3}(?:\.\d{3}){2,}")


def gold_numbers(text: str) -> set[str]:
    return {match.replace(".", "") for match in GOLD_NUMBER_RE.findall(str(text or ""))}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--predictions-file",
        default="ragas_runs/apec_q181_210_v2_scored.json",
        help="Report JSON providing question + ground_truth per id.",
    )
    parser.add_argument("--dataset-id", default="apec")
    parser.add_argument("--ids", default="", help="Comma-separated id subset; default all.")
    parser.add_argument("--json-out", default="", help="Optional path for the JSON summary.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    from datasets.registry import get_dataset
    from test import ensure_built
    from config.allowed_keywords import TABLE_BS, TABLE_CF, TABLE_IS, TABLE_NOTE
    import graph.evidence as graph_evidence
    from tools.evidence import result_to_facts
    from tools.tools import get_related_info

    dataset = get_dataset(args.dataset_id)
    if dataset is None:
        raise SystemExit(f"Dataset not found: {args.dataset_id}")
    _dataset, _conn, collection = ensure_built(dataset)

    report = json.load(open(args.predictions_file, encoding="utf-8"))
    records = report.get("predictions", []) or []
    wanted = {int(x) for x in args.ids.split(",") if x.strip()} if args.ids.strip() else None

    tables = [TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE]
    rows = []
    for record in records:
        record_id = int(record.get("id", 0))
        if wanted is not None and record_id not in wanted:
            continue
        question = str(record.get("question", "") or "")
        gold = gold_numbers(record.get("ground_truth", ""))

        state = {"user_query": question}
        surviving_facts = []
        for table in tables:
            facts_limit = graph_evidence._facts_limit_for_table(state, {}, table)
            retrieval_limit = (
                max(graph_evidence.NOTE_REF_FACTS_SCAN_LIMIT, facts_limit)
                if table == TABLE_NOTE
                else facts_limit
            )
            raw_result = get_related_info(
                query=question,
                table=table,
                collection=collection,
                strict_table=(table == TABLE_NOTE),
                limit=retrieval_limit,
                intent=question,
            )
            facts = result_to_facts(raw_result, table=table, query=question, limit=retrieval_limit)
            facts = graph_evidence._limit_evidence_facts_for_table(
                table, facts, state=state, worker_plan={}
            )
            surviving_facts.extend(facts)

        fact_texts = [
            " ".join(
                str(fact.get(field, "") or "")
                for field in ("evidence_text", "value", "item_name", "subheading")
            )
            for fact in surviving_facts
        ]
        found_numbers = set()
        gold_bearing_facts = 0
        for text in fact_texts:
            numbers_in_fact = gold_numbers(text) & gold
            if numbers_in_fact:
                gold_bearing_facts += 1
            found_numbers |= numbers_in_fact

        recall = (len(found_numbers) / len(gold)) if gold else None
        rows.append(
            {
                "id": record_id,
                "gold_n": len(gold),
                "found_n": len(found_numbers),
                "recall": recall,
                "facts_n": len(surviving_facts),
                "gold_bearing_facts_n": gold_bearing_facts,
                "missing": sorted(gold - found_numbers),
            }
        )
        recall_text = f"{recall:.2f}" if recall is not None else "n/a "
        print(
            f"id {record_id} | gold {len(found_numbers)}/{len(gold)} recall={recall_text}"
            f" | facts={len(surviving_facts)} gold_bearing={gold_bearing_facts}",
            flush=True,
        )

    scored = [row for row in rows if row["recall"] is not None]
    summary = {
        "ids_n": len(rows),
        "scored_n": len(scored),
        "macro_recall": round(sum(row["recall"] for row in scored) / len(scored), 4) if scored else None,
        "total_facts": sum(row["facts_n"] for row in rows),
        "avg_facts_per_question": round(sum(row["facts_n"] for row in rows) / len(rows), 2) if rows else 0,
        "gold_density": round(
            sum(row["gold_bearing_facts_n"] for row in rows) / max(1, sum(row["facts_n"] for row in rows)),
            4,
        ),
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "rows": rows}, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
