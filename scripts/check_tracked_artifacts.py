"""Fail when git tracks runtime artifacts: databases, uploads, env files, bytecode.

``.gitignore`` only stops *untracked* files from being picked up by a local
``git add``. It does nothing for a file that is already tracked, and nothing
for the GitHub web uploader — which is how a database holding user accounts
once reached a public branch. This check inspects what is actually tracked.

It is a detection layer: by the time CI runs, a pushed file is already public.
"""
# Code note: Scripts are developer tooling; they never modify repository content.

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Iterable, Optional

_BLOCKED: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)__pycache__/"), "Python bytecode cache"),
    (re.compile(r"\.py[cod]$"), "Python bytecode"),
    (re.compile(r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$", re.IGNORECASE), "database file"),
    (re.compile(r"(^|/)\.env(\.[^/]+)?$"), "environment file"),
    (re.compile(r"(^|/)(uploads?|user_uploads)/"), "uploaded files"),
    (re.compile(r"(^|/)dataset_store/"), "runtime dataset store"),
    (re.compile(r"\.pdf$", re.IGNORECASE), "PDF document"),
    (re.compile(r"(^|/)node_modules/"), "Node dependencies"),
    (re.compile(r"(^|/)\.next/"), "Next.js build output"),
)
# Templates that document configuration without holding any.
_ALLOWED_NAMES = frozenset({".env.example"})


def find_violations(paths: Iterable[str]) -> list[tuple[str, str]]:
    violations = []
    for path in paths:
        if PurePosixPath(path).name in _ALLOWED_NAMES:
            continue
        for pattern, reason in _BLOCKED:
            if pattern.search(path):
                violations.append((path, reason))
                break
    return violations


def tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [item.decode("utf-8", "surrogateescape") for item in output.split(b"\0") if item]


def main(argv: Optional[list[str]] = None) -> int:
    violations = find_violations(tracked_files())
    if not violations:
        print("check_tracked_artifacts: no tracked runtime artifacts.")
        return 0
    print("check_tracked_artifacts: these tracked files must not be in the repository:", file=sys.stderr)
    for path, reason in violations:
        print(f"  - {path}  ({reason})", file=sys.stderr)
    print(
        "Remove them with `git rm --cached <path>`; if the file ever reached a public "
        "branch, treat its contents as exposed.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
