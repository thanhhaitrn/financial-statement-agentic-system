"""Versioned, crash-safe report helpers shared by batch/evaluation CLIs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


REPORT_SCHEMA_VERSION = 2
PROVIDER_LIMIT_MARKERS = (
    "session usage limit",
    "usage_limit",
    "session_limit",
    "follow-up limit",
    "follow up limit",
    "followup limit",
    "ratelimiterror",
    "rate_limit_exceeded",
    "insufficient_quota",
    "status code: 429",
    "http 429",
    "retry-after",
    "retry after",
)
_PROVIDER_CONTEXT = (
    r"(?:api|provider|inference|llm|ollama|openai|endpoint|server|"
    r"http|status code|retry|backoff|too many requests|resource exhausted|"
    r"resourceexhausted|response error|responseerror)"
)
_PROVIDER_LIMIT_PATTERNS = (
    (
        "rate limit",
        re.compile(
            rf"\b(?:{_PROVIDER_CONTEXT})\b(?:\s+\w+){{0,8}}\s+"
            r"\brate limit\b|"
            rf"\brate limit\b(?:\s+\w+){{0,8}}\s+\b(?:{_PROVIDER_CONTEXT})\b|"
            r"\btoo many requests\b"
        ),
    ),
    (
        "quota",
        re.compile(
            rf"\b(?:{_PROVIDER_CONTEXT})\b(?:\s+\w+){{0,8}}\s+\bquota\b|"
            rf"\bquota\b(?:\s+\w+){{0,8}}\s+\b(?:{_PROVIDER_CONTEXT})\b"
        ),
    ),
    (
        "follow-up limit",
        re.compile(
            r"\b(?:follow up(?: questions?)? (?:usage )?limit|"
            r"limit (?:for |on |of )?follow up(?: questions?)?|"
            r"(?:reached|hit|exceeded) (?:the |your )?(?:maximum )?"
            r"(?:number of )?follow up(?: questions?)?|"
            r"no more follow up(?: questions?)?(?: are)? "
            r"(?:allowed|available))\b"
        ),
    ),
    (
        "session limit",
        re.compile(
            r"\b(?:session|conversation) "
            r"(?:usage |message |turn )?limit\b|"
            r"\b(?:reached|hit|exceeded) (?:the |your )?"
            r"(?:session|conversation)(?: usage| message| turn)? limit\b"
        ),
    ),
    (
        "follow-up limit",
        re.compile(
            r"\b(?:da )?(?:dat|cham|vuot)(?: den)? gioi han "
            r"(?:su dung )?(?:follow up|luot (?:hoi|tra loi)"
            r"(?: tiep| them)?)\b|"
            r"\bgioi han (?:su dung )?(?:follow up|luot (?:hoi|tra loi)"
            r"(?: tiep| them)?)\b|"
            r"\bhet (?:luot )?(?:follow up|hoi tiep|hoi them)\b"
        ),
    ),
    (
        "session limit",
        re.compile(
            r"\b(?:da )?(?:dat|cham|vuot)(?: den)? gioi han "
            r"(?:su dung )?phien\b|"
            r"\bgioi han (?:su dung )?phien\b|"
            r"\bphien .{0,24}\b(?:dat|cham|vuot|het) gioi han\b"
        ),
    ),
)

# Untracked runtime outputs must not change a code identity while a long batch
# is checkpointing.  Version-controlled changes are always included; these
# exclusions apply only to untracked files reported by Git.
_UNTRACKED_RUNTIME_PREFIXES = (
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".venv/",
    "__pycache__/",
    "build/",
    "dist/",
    "htmlcov/",
    "node_modules/",
    "ragas_runs/",
    "dataset_store/manifests/",
    "dataset_store/raw_tables/",
    "dataset_store/registry_backups/",
    "dataset_store/sqlite/",
)
_UNTRACKED_RUNTIME_DIR_NAMES = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "node_modules",
}
_UNTRACKED_RUNTIME_SUFFIXES = {
    ".coverage",
    ".db",
    ".lock",
    ".log",
    ".pid",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".tmp",
}
_UNTRACKED_RUNTIME_NAMES = {
    ".DS_Store",
    ".coverage",
}


def stable_json_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def atomic_write_json(path: str | Path, value: Any) -> Path:
    return atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
    )


def provider_limit_reason(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for marker in PROVIDER_LIMIT_MARKERS:
        if marker in text:
            return marker
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(
        character
        for character in folded
        if not unicodedata.combining(character)
    ).replace("đ", "d")
    folded = " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())
    for reason, pattern in _PROVIDER_LIMIT_PATTERNS:
        if pattern.search(folded):
            return reason
    return ""


def git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _is_relevant_untracked_path(relative_name: str) -> bool:
    """Whether an untracked path can affect source-controlled behaviour."""

    normalized = str(relative_name or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if not normalized:
        return False
    if any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _UNTRACKED_RUNTIME_PREFIXES
    ):
        return False

    path = PurePosixPath(normalized)
    if any(part in _UNTRACKED_RUNTIME_DIR_NAMES for part in path.parts):
        return False
    if path.name in _UNTRACKED_RUNTIME_NAMES:
        return False
    lowered_name = path.name.casefold()
    if any(lowered_name.endswith(suffix) for suffix in _UNTRACKED_RUNTIME_SUFFIXES):
        return False
    if lowered_name.endswith((".egg-info", ".dist-info")):
        return False
    if any(part.endswith((".egg-info", ".dist-info")) for part in path.parts):
        return False
    return True


def git_worktree_provenance(
    repository: str | Path | None = None,
) -> dict[str, Any]:
    """Fingerprint HEAD, tracked diff, and relevant untracked source files.

    ``git_revision`` alone is insufficient when a benchmark runs before the
    implementation is committed.  The report stores digests only, never source
    or diff contents.  Untracked generated/runtime files are intentionally
    excluded so checkpoints, SQLite builds, caches, and RAGAS outputs cannot
    mutate the identity during one run.
    """

    root = Path(repository or Path(__file__).resolve().parents[1]).resolve()

    def run_git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout

    try:
        revision = run_git("rev-parse", "HEAD").decode(
            "utf-8",
            errors="replace",
        ).strip()
        status = run_git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        tracked_diff = run_git("diff", "--binary", "HEAD", "--", ".")
        untracked_output = run_git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "scheme": "git-head-plus-worktree-diff-v1",
            "worktree_dirty": None,
            "repository_dirty": None,
            "git_revision": "",
            "worktree_diff_sha256": None,
            "code_sha256": None,
            "untracked_files_n": None,
            "relevant_untracked_files_n": None,
            "excluded_untracked_files_n": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    all_untracked_paths = sorted(
        os.fsdecode(raw_path)
        for raw_path in untracked_output.split(b"\0")
        if raw_path
    )
    relevant_untracked_paths = [
        relative_name
        for relative_name in all_untracked_paths
        if _is_relevant_untracked_path(relative_name)
    ]

    diff_digest = hashlib.sha256()
    diff_digest.update(b"agentfinx-worktree-diff-v1\0")
    diff_digest.update(tracked_diff)
    for relative_name in relevant_untracked_paths:
        diff_digest.update(b"\0untracked\0")
        diff_digest.update(
            relative_name.encode("utf-8", errors="surrogateescape")
        )
        candidate = root / relative_name
        try:
            if candidate.is_symlink():
                diff_digest.update(b"\0symlink\0")
                diff_digest.update(
                    os.readlink(candidate).encode(
                        "utf-8",
                        errors="surrogateescape",
                    )
                )
            elif candidate.is_file():
                diff_digest.update(b"\0file\0")
                with candidate.open("rb") as handle:
                    for chunk in iter(
                        lambda: handle.read(1024 * 1024),
                        b"",
                    ):
                        diff_digest.update(chunk)
            else:
                diff_digest.update(b"\0missing-or-non-file\0")
        except OSError as exc:
            diff_digest.update(
                f"\0read-error:{type(exc).__name__}\0".encode("ascii")
            )

    worktree_diff_sha256 = diff_digest.hexdigest()
    code_sha256 = stable_json_fingerprint(
        {
            "scheme": "git-head-plus-worktree-diff-v1",
            "git_revision": revision,
            "worktree_diff_sha256": worktree_diff_sha256,
        }
    )
    return {
        "scheme": "git-head-plus-worktree-diff-v1",
        "worktree_dirty": bool(tracked_diff or relevant_untracked_paths),
        "repository_dirty": bool(status),
        "git_revision": revision,
        "worktree_diff_sha256": worktree_diff_sha256,
        "code_sha256": code_sha256,
        "untracked_files_n": len(all_untracked_paths),
        "relevant_untracked_files_n": len(relevant_untracked_paths),
        "excluded_untracked_files_n": (
            len(all_untracked_paths) - len(relevant_untracked_paths)
        ),
        "error": "",
    }
