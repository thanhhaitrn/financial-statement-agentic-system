"""Phase 2, Việc 3: the L5 2×2 on calculation cases (B, C, E, D).

The controlled run of 23/10 could not test L5 on a calculation: the WEB fact
held a whole article in ``value``, the parser read its first number (a year)
and no derived calculation ran. Here the attacker's figure enters as a typed
fact (``WebEvidence.as_structured_fact``) carrying the identity of the report
operand it fakes (table, row, column, period, fiscal year) and the fake number
in ``value``. ``structured_figures`` builds those identities from a case's
answer key; the worker places them in its controlled WEB facts in place of the
attack article's text (``exposure="structured"``).
"""

from __future__ import annotations

from agentfinx.evaluation.security import cases as cases_module


def structured_figures(case: dict) -> list[dict]:
    """The case's fake figures as typed-fact identities, one per faked operand,
    tied to the attack article that carries them."""

    key = case["answer_key"]
    sources = {fact["key"]: fact for fact in list(key["facts"]) + list(key.get("operands") or [])}
    main = next(article for article in case["articles"]["attack"] if article.get("article_key") == "main")
    figures = []
    for fake in case["attacker_target"].get("facts") or []:
        source = sources[fake["key"]]["source"]
        column = source.get("column", "")
        figures.append({
            "key": fake["key"], "value": str(fake["value"]), "unit": fake.get("unit", "VND"),
            "table": source["table"], "row_label": source["row_label"], "column_label": column,
            "item_name": f"{source['row_label']} | {column}" if column else source["row_label"],
            "metric_label": source["row_label"], "period_label": column, "period_role": fake.get("period_role", ""),
            "fiscal_year": str(case["report"].get("fiscal_year") or ""), "statement": source.get("statement", ""),
            "article_url": main["source_url"],
        })
    return figures


def l5_problems(protocol: dict, cases: cases_module.CaseFile) -> list[str]:
    """An L5 run (pilot or main): calculation cases of the frozen calc set,
    configurations among B/C/E/D with the worker's switches, the structured
    exposure, the product and index pinned, scoring deferred."""

    import json
    import re

    from agentfinx.evaluation import benchmarks
    from agentfinx.evaluation.security.runner import STRUCTURED_ENV
    from agentfinx.evaluation.security.worker import CONFIG_FLAGS

    found = []
    if protocol["attack_set"]["sha256"] != cases.sha256:
        found.append("calc set differs from the protocol's")
    freeze_path = benchmarks.resolve((protocol.get("requires") or {}).get("calc_freeze", ""))
    if not freeze_path.is_file():
        found.append("no freeze record for the calc set")
    else:
        record = json.loads(freeze_path.read_text(encoding="utf-8"))
        if record.get("case_set", {}).get("sha256") != cases.sha256:
            found.append("the calc set differs from the one frozen")
    split = protocol["attack_set"].get("split")
    allowed = set(cases.case_ids(split=split)) if split else set(cases.all_case_ids())
    if not protocol["case_ids"] or not set(protocol["case_ids"]) <= allowed \
            or len(set(protocol["case_ids"])) != len(protocol["case_ids"]):
        found.append(f"case ids must be distinct {split or ''} cases of the calc set")
    for config, settings in protocol["configs"].items():
        flags = CONFIG_FLAGS.get(config)
        if config not in ("B", "C", "E", "D") or {key: settings.get(key) for key in flags} != flags:
            found.append(f"config {config}: not one of B/C/E/D with the worker's switches")
    if not set(protocol["conditions"]) <= {"clean", "attack"}:
        found.append("conditions are clean and/or attack")
    if protocol.get("exposure") != "structured" or protocol.get("environment") != STRUCTURED_ENV:
        found.append("exposure and environment must be the structured ones")
    if int(protocol["budget"]["model_calls"]) <= 0:
        found.append("budget must be positive")
    pinned = protocol.get("pinned") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", str(pinned.get("product_code_sha256", ""))):
        found.append("the product version is not pinned")
    for dataset_id in cases.dataset_ids(protocol["case_ids"]):
        if not (pinned.get("datasets") or {}).get(dataset_id, {}).get("index_generation"):
            found.append(f"index of {dataset_id} is not pinned")
    for case_id in protocol["case_ids"]:
        if protocol.get("as_of", {}).get(case_id) != cases.case(case_id)["as_of"]:
            found.append(f"{case_id}: as_of differs from the calc set")
    if (protocol.get("scoring") or {}).get("mode") != "deferred":
        found.append("scoring of an L5 run is deferred")
    g1 = (protocol.get("requires") or {}).get("g1_record")
    if not g1 or not benchmarks.resolve(g1).is_file():
        found.append("the G1 record is not named or missing")
    return found
