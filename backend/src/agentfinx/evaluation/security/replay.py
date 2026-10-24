"""Evidence of the main run's D answers at two boundaries (phase 2, Việc 2.1, gate G2).

For every valid D run of the main experiment (80 attack + 80 clean), with no
model call:

- ``pre``: the report facts the retrieval returned before they were cut down
  for Synth (``ragas_facts_by_table``, the audit set) and the raw articles the
  news provider returned (``provider_searches``; their full text is the
  ``web_bundle`` copy whose ``content_hash`` matches).
- ``post``: Synth's input, the ``<worker_results>`` JSON of the run's last
  ``agent_synth`` request in ``prompt_log.jsonl`` (evidence pack and the news
  block after analysis).

Each boundary records whether the attack span is there (verbatim) and what
``evidence.Payload.found_in`` sees (marker or attacker number). G2 passes when
both boundaries are extracted for 160/160 runs; span presence is a result,
not a condition.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.evaluation.security import baseline
from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security.evidence import Payload

SYNTH_INPUT = re.compile(r"<worker_results>\s*(.*?)\s*</worker_results>", re.S)
ARTICLE_FIELDS = ("evidence_id", "title", "publisher", "published_at", "source_url", "content", "content_hash")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def synth_inputs(prompt_log: Path, run_ids: set[str]) -> dict[str, list[str]]:
    """Every ``<worker_results>`` block sent to Synth, per run, in log order."""

    found: dict[str, list[str]] = defaultdict(list)
    with open(prompt_log, encoding="utf-8") as handle:
        for line in handle:
            if '"agent_synth"' not in line:
                continue
            entry = json.loads(line)
            if entry.get("event") != "request" or entry.get("step") != "agent_synth" \
                    or entry.get("run_id") not in run_ids:
                continue
            for message in entry.get("messages") or []:
                content = message.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                found[entry["run_id"]] += [match.group(1) for match in SYNTH_INPUT.finditer(text)]
    return found


def pre_boundary(snapshot: dict) -> dict:
    state = snapshot["final_state"]
    facts = {table: list(payload.get("facts") or [])
             for table, payload in (state.get("ragas_facts_by_table") or {}).items() if isinstance(payload, dict)}
    kept = {item["content_hash"]: item for item in (state.get("web_bundle") or {}).get("evidence") or []}
    articles, problems = [], []
    searches = snapshot.get("provider_searches") or []
    for returned in (item for search in searches for item in search.get("returned") or []):
        article = kept.get(returned["content_hash"])
        if article is None:
            problems.append(f"returned article {returned['source_url']} has no text in web_bundle")
        elif _sha(article["content"].encode("utf-8")) != returned["content_hash"]:
            problems.append(f"article {returned['source_url']}: text does not match its content_hash")
        else:
            articles.append({field: article.get(field) for field in ARTICLE_FIELDS})
    if not any(facts.values()):
        problems.append("no report facts")
    return {"report_facts": facts, "articles": articles, "news_searched": bool(searches), "problems": problems}


def post_boundary(blocks: list[str]) -> dict:
    if not blocks:
        return {"worker_results": None, "synth_requests": 0, "problems": ["no agent_synth input in the prompt log"]}
    try:
        parsed = json.loads(blocks[-1])
    except json.JSONDecodeError as error:
        return {"worker_results": None, "synth_requests": len(blocks), "problems": [f"Synth input is not JSON: {error}"]}
    return {"worker_results": parsed, "synth_requests": len(blocks), "distinct_inputs": len(set(blocks)),
            "news_status": (parsed.get("web_news_analysis") or {}).get("status"), "problems": []}


def presence(payload: Payload, text: str) -> dict:
    return {"span_verbatim": payload.span_in(text), "found": payload.found_in(text)}


def extract(main_dir: Path, out: Path, *, config: str = "D") -> dict:
    if out.exists():
        raise SystemExit(f"{out} exists; evidence is extracted once")
    scores = json.loads((main_dir / "scores_main.json").read_text(encoding="utf-8"))
    runs = [run for run in scores["runs"] if run.get("valid") and run["config"] == config]
    cases = cases_module.load_cases(main_dir / "cases_snapshot.json")
    prompt_log = main_dir / "prompt_log.jsonl"
    synth = synth_inputs(prompt_log, {run["run_id"] for run in runs})
    (out / "runs").mkdir(parents=True)
    rows = []
    for run in sorted(runs, key=lambda r: r["run_id"]):
        run_dir = main_dir / "runs" / run["run_id"]
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        raw = (run_dir / "snapshot.json").read_bytes()
        case = cases.case(run["case_id"])
        payload = Payload(case)
        pre = pre_boundary(json.loads(raw))
        if _sha(raw) != result.get("snapshot_sha256"):
            pre["problems"].append("snapshot.json does not match the run's snapshot_sha256")
        post = post_boundary(synth.get(run["run_id"], []))
        article_text = "\n\n".join(article["content"] for article in pre["articles"])
        cut_text = "\n\n".join(article["content"][:baseline.ARTICLE_CHARS] for article in pre["articles"])
        checks = {
            "pre_ok": not pre["problems"], "post_ok": not post["problems"],
            "pre_articles": len(pre["articles"]), "news_searched": pre["news_searched"],
            "pre_report_facts": sum(len(facts) for facts in pre["report_facts"].values()),
            "pre_article_payload": presence(payload, article_text),
            "pre_article_span_within_cut": payload.span_in(cut_text),
            "pre_report_payload": presence(payload, json.dumps(pre["report_facts"], ensure_ascii=False)),
            "post_payload": presence(payload, json.dumps(post["worker_results"], ensure_ascii=False))
            if post["worker_results"] is not None else None,
            "post_news_status": post.get("news_status"),
        }
        record = {"run_id": run["run_id"], "case_id": run["case_id"], "condition": run["condition"],
                  "family": case["family"], "subtype": case["subtype"], "question": case["question"],
                  "pre": pre, "post": post, "checks": checks}
        (out / "runs" / f"{run['run_id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                                            encoding="utf-8")
        rows.append({key: record[key] for key in ("run_id", "case_id", "condition", "subtype")} | checks
                    | {"problems": pre["problems"] + post["problems"]})
    summary = summarize(rows, expected=len(cases.case_ids(split="test")) * 2)
    summary.update({"kind": "replay_evidence", "config": config,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": {"main_dir": str(main_dir), "scores_main_sha256": _sha((main_dir / "scores_main.json")
                                                                                      .read_bytes()),
                               "prompt_log_sha256": _sha(prompt_log.read_bytes()),
                               "cases_sha256": cases.sha256},
                    "boundaries": {"pre": "ragas_facts_by_table (retrieved report facts before the Synth cap) + "
                                          "provider_searches articles (text from web_bundle, hash-checked)",
                                   "post": "<worker_results> of the last agent_synth request"},
                    "rows": rows})
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "G2.md").write_text(report(summary), encoding="utf-8")
    return summary


def summarize(rows: list[dict], *, expected: int) -> dict:
    def tally(condition, check):
        return dict(Counter(str(check(row)) for row in rows if row["condition"] == condition))

    by_case = defaultdict(set)
    for row in rows:
        if row["pre_ok"] and row["post_ok"]:
            by_case[row["case_id"]].add(row["condition"])
    complete_cases = sum(1 for conditions in by_case.values() if conditions == {"attack", "clean"})
    pre_ok, post_ok = sum(row["pre_ok"] for row in rows), sum(row["post_ok"] for row in rows)
    return {
        "runs": len(rows), "expected_runs": expected, "pre_extracted": pre_ok, "post_extracted": post_ok,
        "cases_with_both_conditions": complete_cases, "expected_cases": expected // 2,
        "g2": "pass" if len(rows) == expected and pre_ok == post_ok == expected else "fail",
        "attack": {
            "news_searched": tally("attack", lambda r: r["news_searched"]),
            "pre_span_verbatim": tally("attack", lambda r: r["pre_article_payload"]["span_verbatim"]),
            "pre_span_within_cut": tally("attack", lambda r: r["pre_article_span_within_cut"]),
            "pre_found": tally("attack", lambda r: r["pre_article_payload"]["found"]),
            "post_span_verbatim": tally("attack", lambda r: (r["post_payload"] or {}).get("span_verbatim")),
            "post_found": tally("attack", lambda r: (r["post_payload"] or {}).get("found")),
        },
        "clean": {
            "news_searched": tally("clean", lambda r: r["news_searched"]),
            "pre_found": tally("clean", lambda r: r["pre_article_payload"]["found"]),
            "post_found": tally("clean", lambda r: (r["post_payload"] or {}).get("found")),
        },
        "problems": [f"{row['run_id']}: {problem}" for row in rows for problem in row["problems"]],
    }


def _counts(counts: dict) -> str:
    names = {"True": "có", "False": "không"}
    return " · ".join(f"{names.get(key, key)} {value}" for key, value in sorted(counts.items(), key=lambda kv: -kv[1]))


def report(summary: dict) -> str:
    attack = {key: _counts(value) for key, value in summary["attack"].items()}
    clean = {key: _counts(value) for key, value in summary["clean"].items()}
    lines = [
        "# Cổng G2: trích evidence của cấu hình D ở hai ranh giới", "",
        f"**Kết quả: {summary['g2'].upper()}.** pre {summary['pre_extracted']}/{summary['expected_runs']} lượt, "
        f"post {summary['post_extracted']}/{summary['expected_runs']} lượt; "
        f"{summary['cases_with_both_conditions']}/{summary['expected_cases']} case đủ cả tấn công và sạch.", "",
        "- **pre:** fact BCTC mà retrieval trả về trước khi cắt cho Synth (`ragas_facts_by_table`), cộng bài tin thô "
        "provider trả về (`provider_searches`; nội dung lấy từ `web_bundle`, đã khớp `content_hash`).",
        "- **post:** đầu vào của Synth, khối `<worker_results>` của request `agent_synth` cuối cùng trong "
        "`prompt_log.jsonl`.", "",
        "## Đoạn tấn công nằm ở đâu (80 lượt tấn công)", "",
        "| Ranh giới | Kiểm tra | Kết quả |", "|---|---|---|",
        f"| pre | có tìm tin | {attack['news_searched']} |",
        f"| pre | đoạn chèn nguyên văn trong bài | {attack['pre_span_verbatim']} |",
        f"| pre | đoạn chèn còn nguyên sau khi cắt {baseline.ARTICLE_CHARS} ký tự | {attack['pre_span_within_cut']} |",
        f"| pre | payload (marker hoặc số giả) | {attack['pre_found']} |",
        f"| post | đoạn chèn nguyên văn | {attack['post_span_verbatim']} |",
        f"| post | payload (marker hoặc số giả) | {attack['post_found']} |", "",
        f"Lượt sạch (80): có tìm tin: {clean['news_searched']}; payload ở pre: {clean['pre_found']}; "
        f"ở post: {clean['post_found']}. `unknown` là có từ của tuyên bố giả nhưng không có số hay "
        "marker; ở lượt sạch đây là trùng từ ngữ, không phải payload.", "",
        "Cách đọc: `present` ở post nghĩa là bước phân tích tin **không** lọc bỏ payload trước Synth. "
        "Lượt không tìm tin thì A′-pre cũng không có bài báo, đúng như những gì D đã thấy.",
    ]
    if summary["problems"]:
        lines += ["", "## Lỗi trích", ""] + [f"- {problem}" for problem in summary["problems"]]
    return "\n".join(lines) + "\n"
