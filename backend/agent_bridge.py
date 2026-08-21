"""
Integration point between the FastAPI web layer and the real AgentFinX
multi-agent pipeline (https://github.com/thanhhaitrn/financial-statement-agentic-system).

Updated to match the current repo layout (no more ``datasets/`` module —
it's ``dataset_catalog/`` now; graph execution goes through
``execute_query(dataset, collection, query)`` which needs a *built* dataset
+ Qdrant collection, not just the raw compiled graph).

Why a bridge module instead of calling the graph directly in main.py
---------------------------------------------------------------------
The pipeline depends on services that live OUTSIDE this web repo and must
be provisioned by whoever runs the project:

  1. An LLM reachable via ``llm/client.py`` — either local Ollama
     (``OLLAMA_BASE_URL=http://127.0.0.1:11434``, no key needed) or Ollama
     Cloud (needs ``OLLAMA_API_KEY``).
  2. An embedding model for retrieval (``OLLAMA_EMBEDDING_MODEL=bge-m3``,
     also served by Ollama).
  3. A Qdrant vector store — can be ``QDRANT_LOCATION=:memory:`` for local
     testing, or a real Qdrant Cloud cluster for anything persistent.
  4. A **source document already in the project's Markdown convention**
     (see ``data/document.md`` in the agent repo for the expected shape:
     frontmatter + parseable tables/notes). The ingestion pipeline does
     NOT parse arbitrary PDF/Excel uploads — see the big caveat in this
     project's README about what that means for the "upload file" button
     in the chat UI.

This module tries to use the real pipeline when it's importable and
configured; if anything is missing it fails soft with a clear message
instead of crashing the chat endpoint or fabricating financial figures.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agentfinx.agent_bridge")

_AGENT_REPO_PATH = os.getenv("FINX_AGENT_REPO_PATH", "").strip()
if not _AGENT_REPO_PATH:
    # Default: the agent repo is bundled one level up, at ../agent
    # (agentfinx-web/agent/), so it works out of the box with zero config.
    _bundled = Path(__file__).resolve().parent.parent / "agent"
    if _bundled.exists():
        _AGENT_REPO_PATH = str(_bundled)
_DEFAULT_DATASET_ID = os.getenv("FINX_DATASET_ID", "demo").strip()

_lock = threading.Lock()
_state: dict[str, Any] = {"checked": False, "available": False, "reason": ""}

# Per-process cache of "built" (dataset, connection, collection) triples so
# we don't re-run ingestion + re-embed on every single chat message.
_built_cache: dict[str, Any] = {}
_built_cache_lock = threading.Lock()


def _ensure_repo_on_path() -> None:
    if _AGENT_REPO_PATH and _AGENT_REPO_PATH not in sys.path:
        if Path(_AGENT_REPO_PATH).exists():
            sys.path.insert(0, _AGENT_REPO_PATH)


def _probe_pipeline() -> tuple[bool, str]:
    """Import the real pipeline once and report whether it's usable."""
    _ensure_repo_on_path()
    try:
        from dataset_catalog.registry import get_dataset  # noqa: F401
        from graph.workflow import build_graph  # noqa: F401
        from llm.client import get_llm  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any import/setup error counts
        return False, f"Không thể nạp AI Agent thật ({exc.__class__.__name__}): {exc}"

    # Import succeeding doesn't guarantee the LLM/Qdrant creds are valid —
    # that's only discovered on first real call — but it's the cheapest
    # useful signal without paying for a network round trip on every /api/me.
    missing_env = []
    if not os.getenv("OLLAMA_API_KEY") and "127.0.0.1" not in os.getenv("OLLAMA_BASE_URL", "127.0.0.1"):
        missing_env.append("OLLAMA_API_KEY (hoặc dùng Ollama local)")
    if not os.getenv("QDRANT_URL") and not os.getenv("QDRANT_LOCATION"):
        missing_env.append("QDRANT_URL hoặc QDRANT_LOCATION")
    if missing_env:
        return False, "Thiếu cấu hình: " + ", ".join(missing_env)

    return True, ""


def pipeline_status() -> dict[str, Any]:
    """Cheap, cached check of whether the real agent pipeline is usable."""
    with _lock:
        if not _state["checked"]:
            available, reason = _probe_pipeline()
            _state.update(checked=True, available=available, reason=reason)
        return dict(_state)


def resolve_dataset_id(context_filename: Optional[str]) -> str:
    """
    Map an uploaded/attached file to a dataset_id already registered via
    the agent repo's own CLI (``agentfinx ask --file-path ... --dataset-id
    ...``). This project's web backend does NOT ingest PDFs/Excel on the
    fly — see the README caveat. Prefer an explicit FINX_DATASET_ID (single
    company/report deployments); otherwise fall back to the file's stem,
    which only works if that exact dataset_id was pre-registered.
    """
    if _DEFAULT_DATASET_ID:
        return _DEFAULT_DATASET_ID
    if context_filename:
        return Path(context_filename).stem
    return _DEFAULT_DATASET_ID or "default"


def _get_built_dataset(dataset_id: str):
    """Build (or fetch from cache) the SQLite KB + Qdrant collection for a
    dataset that's already registered in dataset_catalog/registry.json."""
    with _built_cache_lock:
        cached = _built_cache.get(dataset_id)
        if cached is not None:
            return cached

    _ensure_repo_on_path()
    from dataset_catalog.registry import get_dataset

    dataset = get_dataset(dataset_id)
    if dataset is None:
        raise LookupError(
            f"Dataset '{dataset_id}' chưa được đăng ký. Hãy chạy "
            f"'agentfinx ask --file-path <file>.md --dataset-id {dataset_id} --query \"...\"' "
            "trong repo AI Agent trước để ingest + build index cho báo cáo này."
        )

    # ensure_built() mirrors test.py's readiness check: builds/validates the
    # SQLite KB + Qdrant collection, cheap no-op if everything is fresh.
    import test as agent_cli  # the repo's own CLI module, reused as a library

    built_dataset, conn, collection = agent_cli.ensure_built(dataset)
    result = (built_dataset, conn, collection)
    with _built_cache_lock:
        _built_cache[dataset_id] = result
    return result


def _format_final_answer(final_state: dict[str, Any]) -> str:
    answer = str(final_state.get("final_answer") or "").strip()
    if answer:
        return answer.replace("\n", "<br>")

    synth_decision = final_state.get("synth_decision") or {}
    fallback = str(synth_decision.get("answer") or "").strip()
    if fallback:
        return fallback.replace("\n", "<br>")

    return (
        "AI Agent đã xử lý câu hỏi nhưng chưa tạo được câu trả lời cuối cùng. "
        "Vui lòng thử diễn đạt lại câu hỏi hoặc kiểm tra dữ liệu đã ingest."
    )


def run_real_pipeline(question: str, dataset_id: str) -> str:
    """
    Invoke the actual pipeline synchronously for an already-registered
    dataset and return an HTML-safe (``<br>``-joined) answer. Raises on any
    failure so the caller (``generate_ai_reply``) can decide how to degrade.
    """
    _ensure_repo_on_path()
    import test as agent_cli

    dataset, _conn, collection = _get_built_dataset(dataset_id)

    started = time.perf_counter()
    final_state = agent_cli.execute_query(dataset, collection, question)
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info("AgentFinX run finished in %d ms (dataset_id=%s)", duration_ms, dataset_id)

    return _format_final_answer(final_state)
