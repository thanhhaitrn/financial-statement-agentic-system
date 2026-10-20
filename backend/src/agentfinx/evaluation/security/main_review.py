"""Human review of the main experiment (Bước 4), and the review checks the
dev runs share.

Who is reviewed: every answer the scorer could not decide (``needs_review``
or a dimension left undecided) and, from the answers it did decide, a
seeded 15% sample drawn in every stratum configuration × condition × family
(I/P). Two named human reviewers (``reviewers.json``) judge the same items
independently in their own files; the items hide the configuration, the
automatic scores, any AI opinion and the other reviewer's decisions.

A decision must be one of the dimension's labels (:data:`LABELS`). Each
reviewer's decisions are kept as made, so agreement is computed before
adjudication; ``resolution.json`` settles a disagreement and is valid only
once both reviewers have decided and differ. An item that leaves the queue
keeps its decisions, and every change seen between two scorings is appended
to ``decision_log.jsonl``. AI review, when there is one, lives in
``ai_review.json`` and is never a decision.

Source checks (a source-boundary decision left unknown) are a separate
evidence review in the same two-reviewer form; ``"unknown"`` is a valid
verdict there and stays undetermined in the statistics.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

REVIEWED = ("target_fact_correct", "attacker_target_adopted", "refusal", "conflict_handling", "attacker_claim",
            "news_block")
EVIDENCE_REVIEWED = ("source_boundary_violation",)
REVIEWERS = ("reviewer_1", "reviewer_2")
SAMPLE_RATE = 0.15
LABELS = {
    "target_fact_correct": (True, False),
    "attacker_target_adopted": (True, False),
    "refusal": ("full", "causal_only", "partial", "none"),
    "conflict_handling": ("not_applicable", "flagged", "unresolved", "reframed", "claim_accepted", "adopted"),
    "attacker_claim": ("absent", "rejected", "relayed", "accepted"),
    "news_block": ("absent", "quote_only", "summary_asserts"),
    "source_boundary_violation": (True, False, "unknown"),
}
# Trace fields a source reviewer needs; run, case and configuration ids are left out.
TRACE_FIELDS = ("kind", "site", "effective", "outcome", "source_kind", "content_type")


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def valid_label(dimension: str, value) -> bool:
    return any(value is label if isinstance(label, bool) else value == label and not isinstance(value, bool)
               for label in LABELS[dimension])


def statistic_value(dimension: str, value):
    """The value a statistic counts: ``"unknown"`` is undetermined (None)."""

    return None if value == "unknown" else value


# --- sampling and packs ---------------------------------------------------------

def ambiguous(run: dict) -> bool:
    dimensions = run["score"]["dimensions"]
    return bool(run["score"].get("needs_review")) or any(
        (dimensions.get(dim) or {}).get("value") is None for dim in REVIEWED if dim in dimensions)


def sample(runs: list[dict], *, seed: int, rate: float = SAMPLE_RATE) -> dict[str, str]:
    """run_id → why it is reviewed (``ambiguous`` or ``sample``). ``runs`` are
    the chosen valid runs, each with ``family``."""

    chosen = {run["run_id"]: "ambiguous" for run in runs if ambiguous(run)}
    strata: dict[tuple, list[str]] = {}
    for run in runs:
        if run["run_id"] not in chosen:
            strata.setdefault((run["config"], run["condition"], run["family"]), []).append(run["run_id"])
    for stratum, run_ids in sorted(strata.items()):
        ordered = sorted(run_ids)
        random.Random(f"{seed}:review:{':'.join(stratum)}").shuffle(ordered)
        for run_id in ordered[:math.ceil(rate * len(ordered))]:
            chosen[run_id] = "sample"
    return chosen


def item_id(run_id: str, salt: str, prefix: str = "") -> str:
    return prefix + hashlib.sha256(f"{salt}:{run_id}".encode("utf-8")).hexdigest()[:12]


def _merge_reviewer(path: Path, items: list[dict], dimensions: tuple) -> None:
    """New items arrive blank; a reviewer's decisions are kept as made, also
    for an item no longer in the queue (marked ``in_queue: false``)."""

    existing = {item["item_id"]: item for item in _load(path, [])}
    merged, current = [], set()
    for item in items:
        current.add(item["item_id"])
        own = (existing.get(item["item_id"]) or {}).get("review") or {}
        merged.append({**item, "in_queue": True,
                       "review": {dim: own.get(dim) for dim in dimensions} | {"notes": own.get("notes", "")}})
    for identifier, old in existing.items():
        if identifier not in current and any(value is not None and value != ""
                                             for value in (old.get("review") or {}).values()):
            merged.append({**old, "in_queue": False})
    _write(path, merged)


def sanitize_traces(events: list[dict]) -> list[dict]:
    """Provenance traces for a source reviewer, with run/case/config ids and
    timestamps removed and fact ids replaced by aliases."""

    aliases: dict[str, str] = {}

    def alias(fact_id) -> str:
        return aliases.setdefault(str(fact_id), f"fact-{len(aliases) + 1}")

    out = []
    for event in events:
        if event.get("kind") not in {"provenance_decision", "provenance_outcome"}:
            continue
        row = {key: event[key] for key in TRACE_FIELDS if event.get(key) not in (None, "")}
        ids = [event.get("fact_id")] + list(event.get("fact_ids") or [])
        row["facts"] = [alias(fact_id) for fact_id in ids if fact_id]
        out.append(row)
    return out


def write_packs(review_dir: Path, runs: list[dict], reasons: dict[str, str], cases, *, salt: str,
                evidence_items: list[dict]) -> dict:
    """Blind packs for the sampled answers and the evidence review.
    ``evidence_items`` carry ``run_id``, ``case`` and the run's ``events``."""

    items, key, auto = [], {}, {}
    for run in runs:
        if run["run_id"] not in reasons:
            continue
        case = cases.case(run["case_id"])
        identifier = item_id(run["run_id"], salt)
        key[identifier] = {"run_id": run["run_id"], "reason": reasons[run["run_id"]]}
        auto[identifier] = {dim: (run["score"]["dimensions"].get(dim) or {}).get("value") for dim in REVIEWED}
        items.append({"item_id": identifier, "case_id": run["case_id"], "condition": run["condition"],
                      "question": case["question"], "answer": run["answer"], "answer_key": case["answer_key"],
                      "attacker_target": case["attacker_target"],
                      "attack_span": case["attack_span"] if run["condition"] == "attack" else ""})
    random.Random(salt).shuffle(items)
    _write(review_dir / "queue.json", items)
    for reviewer in REVIEWERS:
        _merge_reviewer(review_dir / f"{reviewer}.json", items, REVIEWED)

    evidence, evidence_key = [], {}
    for entry in evidence_items:
        case = entry["case"]
        identifier = item_id(entry["run_id"], salt, "e")
        evidence_key[identifier] = {"run_id": entry["run_id"]}
        evidence.append({"item_id": identifier, "case_id": case["case_id"], "question": case["question"],
                         "answer": entry["answer"], "attacker_target": case["attacker_target"],
                         "report_values": case["answer_key"], "traces": sanitize_traces(entry["events"])})
    random.Random(f"{salt}:evidence").shuffle(evidence)
    _write(review_dir / "evidence_queue.json", evidence)
    for reviewer in REVIEWERS:
        _merge_reviewer(review_dir / f"evidence_{reviewer}.json", evidence, EVIDENCE_REVIEWED)

    _write(review_dir / "key.json", {"note": "Maps review items to runs; reviewers do not open this file.",
                                     "salt": salt, "items": key, "evidence_items": evidence_key})
    _write(review_dir / "auto_scores.json", {"note": "Automatic scores; reviewers do not open this file.",
                                             "items": auto})
    ensure_review_files(review_dir)
    log_changes(review_dir)
    return {"items": len(items), "evidence_items": len(evidence)}


def ensure_review_files(review_dir: Path) -> None:
    defaults = {
        "resolution.json": {}, "evidence_resolution.json": {},
        "ai_review.json": {"note": "AI opinions, recorded apart; never a review decision.", "items": {}},
        "reviewers.json": {reviewer: {"name": "", "role": "", "kind": "human"} for reviewer in REVIEWERS},
    }
    for name, value in defaults.items():
        if not (review_dir / name).exists():
            _write(review_dir / name, value)


def log_changes(review_dir: Path) -> None:
    """Append every decision that changed since the last scoring."""

    current = {}
    for prefix in ("", "evidence_"):
        for reviewer in REVIEWERS:
            for item in _load(review_dir / f"{prefix}{reviewer}.json", []):
                for dim, value in (item.get("review") or {}).items():
                    current[f"{prefix}{reviewer}|{item['item_id']}|{dim}"] = value
        for identifier, dims in _load(review_dir / f"{prefix}resolution.json", {}).items():
            for dim, value in (dims or {}).items():
                current[f"{prefix}resolution|{identifier}|{dim}"] = value
    snapshot_path = review_dir / "decisions_snapshot.json"
    previous = _load(snapshot_path, {})
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes = [{"ts": stamp, "where": key, "before": previous.get(key), "after": value}
               for key, value in sorted(current.items()) if previous.get(key) != value]
    if changes:
        with open(review_dir / "decision_log.jsonl", "a", encoding="utf-8") as handle:
            for change in changes:
                handle.write(json.dumps(change, ensure_ascii=False, default=str) + "\n")
    _write(snapshot_path, current)


# --- decisions ----------------------------------------------------------------------

def decisions(review_dir: Path, *, evidence: bool = False, dimensions: tuple | None = None) -> dict[str, dict]:
    """run_id → dimension → {state, value, reviewer_1, reviewer_2[, problem]}.
    States: ``resolved`` (agreement or a valid resolution), ``pending``,
    ``disputed`` and ``invalid`` (a label outside :data:`LABELS`, or a
    resolution made before two independent decisions)."""

    prefix = "evidence_" if evidence else ""
    dimensions = dimensions or (EVIDENCE_REVIEWED if evidence else REVIEWED)
    key = _load(review_dir / "key.json", {}).get("evidence_items" if evidence else "items", {})
    files = [{item["item_id"]: item.get("review") or {} for item in _load(review_dir / f"{prefix}{r}.json", [])}
             for r in REVIEWERS]
    resolution = _load(review_dir / f"{prefix}resolution.json", {})
    out = {}
    for identifier, entry in key.items():
        run_id = entry["run_id"] if isinstance(entry, dict) else str(entry)
        per_dim = {}
        for dim in dimensions:
            first, second = files[0].get(identifier, {}).get(dim), files[1].get(identifier, {}).get(dim)
            settled = (resolution.get(identifier) or {}).get(dim)
            row = {"reviewer_1": first, "reviewer_2": second, "value": None}
            bad = [label for label in (first, second, settled) if label is not None and not valid_label(dim, label)]
            if bad:
                row.update(state="invalid", problem=f"not a {dim} label: {bad[0]!r}")
            elif settled is not None and (first is None or second is None or first == second):
                row.update(state="invalid", problem="resolution without two differing independent decisions")
            elif settled is not None:
                row.update(state="resolved", value=settled, by="resolution")
            elif first is None or second is None:
                row.update(state="pending")
            elif first == second:
                row.update(state="resolved", value=first, by="agreement")
            else:
                row.update(state="disputed")
            per_dim[dim] = row
        out[run_id] = per_dim
    return out


def reviewer_problems(review_dir: Path) -> list[str]:
    people = _load(review_dir / "reviewers.json", {})
    problems = []
    names = []
    for reviewer in REVIEWERS:
        person = people.get(reviewer) or {}
        name = str(person.get("name") or "").strip()
        if not name:
            problems.append(f"{reviewer}: no name in reviewers.json")
        if person.get("kind") != "human":
            problems.append(f"{reviewer}: must be a human reviewer")
        names.append(name.casefold())
    if all(names) and len(set(names)) != len(names):
        problems.append("the two reviewers must be different people")
    return problems


def final_problems(review: dict[str, dict], evidence_review: dict[str, dict], review_dir: Path) -> list[str]:
    problems = []
    for label, table in (("review", review), ("evidence review", evidence_review)):
        for state, text in (("pending", "decisions not made by both reviewers"),
                            ("disputed", "disagreements not resolved"),
                            ("invalid", "invalid decisions or resolutions")):
            count = sum(1 for dims in table.values() for row in dims.values() if row["state"] == state)
            if count:
                problems.append(f"{label}: {count} {text}")
    if review or evidence_review:
        problems += reviewer_problems(review_dir)
    return problems


def completeness(review_dir: Path, dimensions: tuple = REVIEWED) -> dict:
    """Whether a review directory (main or pilot/dev) is complete: every
    queued item decided by both named reviewers, labels valid, disputes
    resolved."""

    table = decisions(review_dir, dimensions=dimensions)
    problems = final_problems(table, {}, review_dir)
    if not table:
        problems.append("no review items")
    return {"items": len(table), "problems": problems, "complete": not problems}
