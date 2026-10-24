"""Independent re-check of the 15 sampled test cases against the reports.

Written separately from agentfinx.evaluation.security (gold/checks/builder),
so a bug there is not repeated here. Reads only; writes nothing.
"""

import json
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

HOME = Path("/Users/thanhhai/itec")
SET = HOME / "backend/benchmarks/security/attack_set.json"
SIGNOFF = HOME / "backend/benchmarks/security/review/test_case_signoff.json"

data = json.loads(SET.read_text(encoding="utf-8"))
cases = {c["case_id"]: c for c in data["cases"]}
ids = [item["case_id"] for item in json.loads(SIGNOFF.read_text(encoding="utf-8"))["cases"]]
NUM = re.compile(r"\(?-?\d{1,3}(?:\.\d{3})+\)?")


def parse(cell: str):
    cell = cell.strip().replace("*", "")
    m = re.fullmatch(r"(\()?(-)?(\d{1,3}(?:\.\d{3})+|\d+)(\))?", cell)
    if not m:
        return None
    v = Decimal(m.group(3).replace(".", ""))
    return -v if (m.group(1) and m.group(4)) or m.group(2) else v


def table_context(lines, n):
    i = n - 1
    start = i
    while start > 0 and lines[start - 1].lstrip().startswith("|"):
        start -= 1
    rows = [l for l in lines[start:i + 1] if l.lstrip().startswith("|")]
    sep = next((k for k, r in enumerate(rows) if re.fullmatch(r"[\s|:\-]+", r)), None)
    header = rows[:sep] if sep is not None else rows[:1]
    above = [l.strip() for l in lines[max(0, start - 8):start] if l.strip() and not l.lstrip().startswith("|")]
    return header, above


def all_numbers(text):
    out = set()
    for m in NUM.finditer(text):
        v = parse(m.group(0))
        if v is not None and abs(v) >= 1000:
            out.add(abs(v))
    return out


def rel(a, b):
    return abs(a - b) / abs(b)


for cid in ids:
    c = cases[cid]
    ds = data["datasets"][c["dataset_id"]]
    report = (HOME / ds["report"]["file"]).read_text(encoding="utf-8")
    lines = report.splitlines()
    print("=" * 100)
    print(cid, c["subtype"], ds["ticker"], ds["company"], "|", ds["report"]["title"])
    print("Q:", c["question"])
    head = "\n".join(lines[:80])
    print("company in report head:", ds["company"].lower() in head.lower() or ds["company"].lower() in report.lower(),
          "| ticker in report:", bool(re.search(rf"\b{ds['ticker']}\b", report)))
    gold = c["gold"]
    for f in gold["facts"] + gold["operands"]:
        s = f["source"]
        row = lines[s["line"] - 1]
        header, above = table_context(lines, s["line"])
        cells = [x.strip() for x in row.strip().strip("|").split("|")]
        vals = [parse(x) for x in cells]
        print(f"  [{f['key']}] {f['metric']} {f['period']} = {f['value']}  (line {s['line']}, code {s['code']})")
        print("    row  :", " | ".join(cells))
        for h in header:
            print("    head :", " | ".join(x.strip() for x in h.strip().strip("|").split("|")))
        print("    above:", " // ".join(above[-4:])[:300])
        print("    value in row:", Decimal(f["value"]) in vals, "| position:",
              [k for k, v in enumerate(vals) if v == Decimal(f["value"])])
        unit = [l.strip() for l in lines[max(0, s["line"] - 60):s["line"]] if "đơn vị" in l.lower()]
        print("    unit lines:", unit[-2:])
    for d in gold.get("derived") or []:
        ops = {o["key"]: Decimal(o["value"]) for o in gold["operands"] + gold["facts"]}
        if d["key"] == "gross_margin":
            val = ops["gross_profit_current"] / ops["revenue_current"] * 100
        elif d["key"] == "revenue_growth":
            val = (ops["revenue_current"] / ops["revenue_previous"] - 1) * 100
        else:
            val = None
        print(f"  derived {d['key']} = {d['value']} | recomputed {val:.4f}")
    t = c["attacker_target"]
    nums = all_numbers(report)
    print("  attacker:", t["kind"], t.get("rule"), t.get("ratio"), "claims:", t.get("claims"))
    for f in t.get("facts") or []:
        fake = Decimal(f["value"])
        g = next(x for x in gold["facts"] + gold["operands"] if x["key"] == f["key"])
        gv = Decimal(g["value"])
        print(f"    fake {f['key']} = {fake} ; ratio fake/gold = {fake / gv:.4f} ; dev = {(fake - gv) / gv * 100:+.2f}%")
        nearest = sorted(nums, key=lambda n: rel(abs(fake), n))[:3]
        print("    nearest report numbers:", [(str(n), f"{rel(abs(fake), n) * 100:.2f}%") for n in nearest])
    for d in t.get("derived") or []:
        ops = {o["key"]: Decimal(o["value"]) for o in gold["operands"] + gold["facts"]}
        ops.update({f["key"]: Decimal(f["value"]) for f in t.get("facts") or []})
        if d["key"] == "gross_margin":
            val = ops["gross_profit_current"] / ops["revenue_current"] * 100
        elif d["key"] == "revenue_growth":
            val = (ops["revenue_current"] / ops["revenue_previous"] - 1) * 100
        print(f"    fake derived {d['key']} = {d['value']} | recomputed {val:.4f}")
    span = c["attack_span"]
    print("  span numbers:", re.findall(r"\d[\d.,]*", span))
    # articles
    att, cln = c["articles"]["attack"], c["articles"]["clean"]
    same_meta = all({k: a[k] for k in a if k not in ("content", "summary")} ==
                    {k: b[k] for k in b if k not in ("content", "summary")} for a, b in zip(att, cln))
    only_span = att[0]["content"].replace(span, c["clean_span"]) == cln[0]["content"] and att[0]["content"].count(span) == 1
    rest_same = att[1:] == cln[1:]
    canary_ok = c["canary"] in span and all(c["canary"] not in json.dumps(a, ensure_ascii=False) for a in cln)
    asof = datetime.fromisoformat(c["as_of"])
    avail = datetime.fromisoformat(ds["available_on"] + "T00:00:00+07:00")
    dates = [datetime.fromisoformat(a["published_at"]) for a in att]
    in_window = all(asof - timedelta(days=30) <= d <= asof for d in dates)
    after_avail = all(d >= avail for d in dates)
    print(f"  articles: meta same {same_meta}, only span differs {only_span}, others identical {rest_same}, "
          f"canary ok {canary_ok}, within 30d {in_window}, after availability {after_avail} "
          f"({ds['available_on']} {ds['availability_basis']}), dates {[d.date().isoformat() for d in dates]}, as_of {asof.date()}")
    summary_has_span = any(span in (a.get("summary") or "") for a in att)
    print("  summary contains span:", summary_has_span, "| clean summary == attack summary:",
          [a.get("summary") == b.get("summary") for a, b in zip(att, cln)])
    q_digits = re.findall(r"\d[\d.,]{3,}", c["question"])
    print("  question numbers:", q_digits)
