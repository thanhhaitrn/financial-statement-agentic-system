"""Attack cases: question, clean and attack articles, gold facts, attacker target.

Two file formats load into one case shape (:meth:`CaseFile.case`):

- schema 1, the pilot (``benchmarks/security/pilot_cases.json``): one dataset,
  ticker and ``as_of`` for the whole file; each case's article is a template
  whose ``{span}`` is the attack or the clean span.
- schema 2, the attack set (``benchmarks/security/attack_set.json``, built by
  ``builder``): every case names its own dataset, ticker, company, ``as_of``
  and period cues, carries its gold facts with their report source, and holds
  both conditions' articles in full, so a clean twin needs nothing else to be
  rebuilt.

The case file is the only place the gold facts and the attacker target exist;
:func:`condition_articles` hands the runner the articles of one condition and
nothing else, so neither ever reaches a prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agentfinx.evaluation import benchmarks

DEFAULT_CASES = "security/pilot_cases.json"
ATTACK_SET = "benchmarks/security/attack_set.json"
CONDITIONS = ("clean", "attack")
CONFIGS = ("A", "B", "C", "D")
REQUIRED_CASES = ("I1", "P2", "I3")
OPTIONAL_CASES = ("P3", "P4")


@dataclass
class CaseFile:
    path: Path
    data: dict
    sha256: str
    _cases: dict = field(default_factory=dict, repr=False)

    @property
    def schema(self) -> int:
        return int(self.data.get("schema_version", 1))

    # Pilot-wide values (schema 1); schema 2 keeps them per case.
    @property
    def as_of(self) -> str:
        return str(self.data["as_of"])

    @property
    def ticker(self) -> str:
        return str(self.data["ticker"])

    def case(self, case_id: str) -> dict:
        if not self._cases:
            self._cases = {case["case_id"]: case for case in self._normalized()}
        try:
            return self._cases[case_id]
        except KeyError:
            raise KeyError(f"unknown case {case_id!r}") from None

    def cases(self) -> list[dict]:
        return [self.case(case_id) for case_id in self.all_case_ids()]

    def all_case_ids(self) -> list[str]:
        return [case.get("case_id") or case.get("id") for case in self.data["cases"]]

    def case_ids(self, *, include_optional: bool = False, split: str | None = None) -> list[str]:
        if self.schema == 1:
            return [case["case_id"] for case in self.data["cases"] if case.get("required") or include_optional]
        return [case["case_id"] for case in self.data["cases"] if split is None or case["split"] == split]

    def dataset_ids(self, case_ids=None) -> list[str]:
        wanted = set(case_ids or self.all_case_ids())
        return sorted({self.case(cid)["dataset_id"] for cid in wanted})

    def _normalized(self) -> list[dict]:
        if self.schema == 1:
            return [_pilot_case(self.data, case) for case in self.data["cases"]]
        datasets = self.data["datasets"]
        return [_attack_case(datasets[case["dataset_id"]], case) for case in self.data["cases"]]


def load_cases(path: str | Path | None = None) -> CaseFile:
    resolved = benchmarks.resolve(path or DEFAULT_CASES)
    raw = resolved.read_bytes()
    return CaseFile(path=resolved, data=json.loads(raw.decode("utf-8")),
                    sha256=hashlib.sha256(raw).hexdigest())


def _pilot_article(data: dict, spec: dict, case_id: str, content: str) -> dict:
    """An article in the provider's own input form (``WebEvidence`` fields).
    ``source_kind``/``content_type`` are never set here: the app's schema adds
    them when it turns the article into a fact."""

    published = datetime.fromisoformat(spec["published_at"])
    return {
        "article_key": spec["article_key"],
        "ticker": data["ticker"],
        "company": data["company"],
        "title": spec["title"],
        "summary": spec["summary"],
        "content": content,
        "publisher": data["publisher"],
        "source_url": f"{data['url_base']}{case_id.lower()}-{spec['article_key']}",
        "published_at": published.isoformat(),
        "content_status": "full",
    }


def _pilot_case(data: dict, case: dict) -> dict:
    case_id = case["case_id"]
    articles = {}
    for condition in CONDITIONS:
        span = case["attack_span"] if condition == "attack" else case["clean_span"]
        main = case["article"]
        items = [_pilot_article(data, main, case_id, main["template"].replace("{span}", span))]
        items += [_pilot_article(data, shared, case_id, shared["content"]) for shared in data.get("shared_articles", [])]
        articles[condition] = items
    return {
        **case,
        "split": "dev",  # every pilot case and template is dev material
        "subtype": case_id,
        "dataset_id": data["dataset_id"],
        "ticker": data["ticker"],
        "company": data["company"],
        "as_of": data["as_of"],
        "report": data["report"],
        "period_cues": None,  # the scorer's pilot default (Q4/2024 vs 2023)
        "articles": articles,
    }


def _attack_case(dataset: dict, case: dict) -> dict:
    gold = case["gold"]
    return {
        **case,
        "ticker": dataset["ticker"],
        "company": dataset["company"],
        "report": dataset["report"],
        "period_cues": dataset["period_cues"],
        "answer_key": {
            "facts": gold["facts"],
            "derived": gold.get("derived") or [],
            "derived_required": bool(gold.get("derived_required")),
            "operands": gold.get("operands") or [],
            "rounding": gold.get("rounding") or {"min_significant_digits": 3},
        },
        "payload_markers": [marker for marker in [case.get("canary"), *case.get("payload_markers", [])] if marker],
    }


def condition_articles(cases: CaseFile, case_id: str, condition: str) -> list[dict]:
    """The articles the news source holds for ``case_id`` under ``condition``."""

    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}")
    return [dict(article) for article in cases.case(case_id)["articles"][condition]]


def decimal(value: str | int | float) -> Decimal:
    return Decimal(str(value))
