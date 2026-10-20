"""Read local Ollama key lists without changing the environment or logging secrets."""

from __future__ import annotations

import re
from pathlib import Path

_KEY_LINE = re.compile(
    r"^\s*OLLAMA_API_KEY\s*=\s*['\"]?([^\s#'\"]+)['\"]?\s*(?:#.*)?$"
)


def load_keys(path: Path, *, include_commented: bool = False) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if include_commented and line.lstrip().startswith("#"):
            line = line.lstrip()[1:].lstrip()
        match = _KEY_LINE.fullmatch(line)
        if match and match.group(1) not in keys:
            keys.append(match.group(1))
    return keys
