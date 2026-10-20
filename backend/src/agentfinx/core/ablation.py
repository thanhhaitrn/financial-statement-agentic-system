"""Switches that remove named defenses, for the security ablation only.

Two switches, both off unless the experiment runner sets them:

- ``AGENTFINX_ABLATE_PROMPT_GUARDS=1`` drops every prompt block registered
  with :func:`guard` (untrusted-data warnings, "WEB numbers are not operands"
  rules). Message roles, delimiters, data and business rules stay.
- ``AGENTFINX_ABLATE_PROVENANCE=1`` lets non-report facts through the report
  provenance gate (``is_report_fact`` and the ``WEB`` container skip). Facts
  are never relabelled or changed; the schema, number, unit, period, status
  and calculation checks still run on them.

The switches are read once per process, at the first question, and never
change afterwards: each experiment run is its own process. Prompt texts that
are module constants are built at import, so they follow the same rule.
Production code never sets either variable.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from threading import Lock

PROMPT_GUARDS_ENV = "AGENTFINX_ABLATE_PROMPT_GUARDS"
PROVENANCE_ENV = "AGENTFINX_ABLATE_PROVENANCE"

# The prompt guard blocks, by defense id: every text passed to ``guard`` in
# this process (whether or not it was kept) and what replaces it when removed.
_GUARD_BLOCKS: dict[str, dict[str, str]] = {}
_lock = Lock()


@dataclass(frozen=True)
class AblationConfig:
    prompt_guards_off: bool = False
    provenance_off: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


_config: AblationConfig | None = None


def _flag(name: str) -> bool:
    value = os.getenv(name, "0").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise RuntimeError(f"{name} must be 0 or 1, not {value!r}")


def config() -> AblationConfig:
    """The switches of this process, read from the environment once."""

    global _config
    if _config is None:
        with _lock:
            if _config is None:
                _config = AblationConfig(
                    prompt_guards_off=_flag(PROMPT_GUARDS_ENV),
                    provenance_off=_flag(PROVENANCE_ENV),
                )
    return _config


@contextmanager
def override(*, prompt_guards_off: bool | None = None, provenance_off: bool | None = None):
    """Tests only: change the switches for gates read at call time. Prompt
    constants built at import keep the text they were built with."""

    global _config
    previous = config()
    _config = AblationConfig(
        prompt_guards_off=previous.prompt_guards_off if prompt_guards_off is None else prompt_guards_off,
        provenance_off=previous.provenance_off if provenance_off is None else provenance_off,
    )
    try:
        yield _config
    finally:
        _config = previous


def guard(defense_id: str, text: str, *, removed: str = "") -> str:
    """``text`` (a named prompt guard block), or ``removed`` when prompt guards
    are ablated. ``removed`` is only for the few blocks whose removal must
    re-join the business sentence they shared; it never adds a rule."""

    with _lock:
        _GUARD_BLOCKS.setdefault(defense_id, {})[text] = removed
    return removed if config().prompt_guards_off else text


def guard_blocks() -> dict[str, list[dict]]:
    """Every prompt guard block registered so far, by defense id, as
    ``{"text": ..., "removed": ...}``."""

    with _lock:
        return {key: [{"text": text, "removed": removed} for text, removed in value.items()]
                for key, value in _GUARD_BLOCKS.items()}
