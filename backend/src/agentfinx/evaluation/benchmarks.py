"""Versioned evaluation inputs: seeds, fact contracts and question sets.

They are part of the source checkout (``backend/benchmarks/`` and
``backend/tests/fixtures/``), never of storage or artifacts. A relative name
— a default, or one recorded in a report or contract — is looked up in
``backend/``, then ``backend/benchmarks/`` (reports written before the move
name their seed by its bare file name), then next to the file that names it,
and only last in the current directory, so a default never depends on where a
command runs. Names are recorded relative to ``backend/`` when they are in it.
The reviewed question sets the evaluators use by default ship with the package
(``evaluation/data/``) and are found by file name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agentfinx.core.config import paths

APEC_SEED = "benchmarks/dau_tu_APEC_ragas_seed.json"
DATA_DIR = Path(__file__).resolve().parent / "data"


def resolve(name: str | Path, *, near: Optional[str | Path] = None) -> Path:
    candidate = Path(name).expanduser()
    if candidate.is_absolute():
        return candidate
    project = paths.project_root()
    options = [project / candidate, project / "benchmarks" / candidate]
    if near:
        options.append(Path(near).resolve().parent / candidate)
    options += [DATA_DIR / candidate.name, Path.cwd() / candidate]
    return next((path for path in options if path.exists()), options[0])


def portable(path: str | Path) -> str:
    """How a benchmark path is recorded: relative to ``backend/`` when inside it."""

    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(paths.project_root().resolve()).as_posix()
    except ValueError:
        return str(resolved)
