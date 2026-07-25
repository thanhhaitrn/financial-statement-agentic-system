"""Run a resumable command while rotating configured Ollama API keys."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


_KEY_LINE_RE = re.compile(
    r"^\s*OLLAMA_API_KEY\s*=\s*['\"]?([^\s#'\"]+)['\"]?\s*(?:#.*)?$"
)


def _load_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _KEY_LINE_RE.match(line)
        if match and match.group(1) not in keys:
            keys.append(match.group(1))
    return keys


def _count_done(path: Path, count_key: str) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    rows = payload.get(count_key, []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return 0
    if count_key == "predictions":
        return sum(
            1
            for row in rows
            if isinstance(row, dict)
            and str(row.get("answer", "") or "").strip()
            and not row.get("errors")
        )
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and (row.get("context_recall") is not None or row.get("faithfulness") is not None)
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count_file", type=Path)
    parser.add_argument("count_key", choices=("predictions", "scores"))
    parser.add_argument("limit", type=int)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if args.limit <= 0:
        parser.error("limit must be positive")
    if not args.command:
        parser.error("a command is required after --")
    if not args.env_file.is_file():
        parser.error(f"env file not found: {args.env_file}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    keys = _load_keys(args.env_file)
    if not keys:
        raise SystemExit("No uncommented OLLAMA_API_KEY entries found.")

    last_return_code = 0
    for key in keys:
        done = _count_done(args.count_file, args.count_key)
        if done >= args.limit:
            break
        env = dict(os.environ)
        env["OLLAMA_API_KEY"] = key
        print(f"[rotate] {args.count_key} done={done}/{args.limit} -> running", flush=True)
        process = subprocess.run(args.command, env=env)
        last_return_code = int(process.returncode)
        print(
            f"[rotate] exit={last_return_code} "
            f"done={_count_done(args.count_file, args.count_key)}/{args.limit}",
            flush=True,
        )

    final = _count_done(args.count_file, args.count_key)
    print(f"[rotate] FINAL {args.count_key} done={final}/{args.limit}", flush=True)
    if final >= args.limit:
        return 0
    return last_return_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
