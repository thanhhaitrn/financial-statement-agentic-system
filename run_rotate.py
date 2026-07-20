"""Run a batch/eval command with automatic OLLAMA_API_KEY rotation.

Usage: python run_rotate.py <count_file> <predictions|scores> <limit> -- <cmd...>
Rotates OLLAMA_API_KEY across every key in .env; the command must support
--resume so finished work is skipped when a key hits its limit.
"""
import json
import os
import re
import subprocess
import sys

count_file, count_key, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
assert sys.argv[4] == "--", "expected -- separator before command"
cmd = sys.argv[5:]

keys = []
for line in open(".env", encoding="utf-8"):
    m = re.search(r"OLLAMA_API_KEY\s*=\s*(\S+)", line)
    if m and m.group(1) not in keys:
        keys.append(m.group(1))


def count_done() -> int:
    try:
        d = json.load(open(count_file, encoding="utf-8"))
    except Exception:
        return 0
    rows = d.get(count_key, []) or []
    if count_key == "predictions":
        return sum(1 for p in rows if str(p.get("answer", "")).strip() and not p.get("errors"))
    return sum(1 for s in rows if s.get("context_recall") is not None or s.get("faithfulness") is not None)


for key in keys:
    done = count_done()
    if done >= limit:
        break
    env = dict(os.environ)
    env["OLLAMA_API_KEY"] = key
    print(f"[rotate] {count_key} key={key[:8]}.. done={done}/{limit} -> running", flush=True)
    proc = subprocess.run(cmd, env=env)
    print(f"[rotate] key={key[:8]}.. exit={proc.returncode} done={count_done()}/{limit}", flush=True)

final = count_done()
print(f"[rotate] FINAL {count_key} done={final}/{limit}", flush=True)
sys.exit(0 if final >= limit else 1)
