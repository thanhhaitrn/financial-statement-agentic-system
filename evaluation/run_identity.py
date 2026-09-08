"""Run/output provenance fingerprints for batch prediction reports.

Extracted from ``dataset_batch_result`` to keep that module focused on running
predictions and building reports. Everything here is a pure, secret-free
fingerprint of the inputs that affect a run's output (model, embedding, prompts,
config, dataset identity, seed selection), so a resume can detect when any of
them changed and refuse to reuse stale scores.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common import prediction_key
from config.domain_catalog import DOMAIN_CATALOG_VERSION, catalog_fingerprint
from config.runtime_policy import active_policy
from evaluation.contracts import (git_worktree_provenance, sha256_file,
                                  stable_json_fingerprint)

ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_IDENTITY_VERSION = 4
_PROMPT_IDENTITY_FILES = (
    "agents/prompts.py",
    "agents/profiles.py",
)
_CONFIG_IDENTITY_FILES = (
    "output_formatter.py",
    "agents/agent_registry.py",
    "config/domain_catalog.py",
    "config/runtime_policy.py",
    "agents/keyworder_runner.py",
    "agents/planner_runner.py",
    "agents/synth_runner.py",
    "graph/dispatch_nodes.py",
    "graph/evidence.py",
    "graph/router.py",
    "schemas/web_evidence.py",
    "schemas/evidence_origin.py",
    "acquisition/llamaparse.py",
    "acquisition/local_text.py",
    "acquisition/quality.py",
    "acquisition/cafef.py",
    "acquisition/download.py",
    "schemas/financial_validation.py",
    "tools/http_safety.py",
    "tools/tool_runner.py",
    "tools/tools.py",
    "web_evidence/cache.py",
    "web_evidence/vietstock.py",
)
_RUNTIME_CONFIG_DEFAULTS = {
    "LLM_REQUEST_TIMEOUT_SECONDS": "900",
    "LLM_MAX_RETRIES": "1",
    "PERIOD_PENALTY_COMPARISON": "0.5",
    "LLM_RERANK": "0",
    "LLM_RERANK_CANDIDATES": "20",
    "LLM_RERANK_WEIGHT": "50",
    "LLM_RERANK_PROTECT": "2",
    "LLM_RERANK_GATE_VALUE_LOOKUP": "1",
    "SCHEDULE_LIMIT": "24",
    "VALUE_LOOKUP_LIMIT": "20",
    "EXACT_MATCH_COLLAPSE": "0",
    "RUNTIME_EVIDENCE_CACHE_MAX_ITEMS": "256",
}


def _safe_endpoint_identity(value: Any) -> str:
    """Return a stable endpoint identity without credentials or query secrets."""

    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme and not parsed.netloc:
        return text
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host.lower()}{port}{path}"


def _source_files_identity(relative_paths: tuple[str, ...]) -> dict[str, str]:
    identity = {}
    for relative_path in relative_paths:
        path = ROOT_DIR / relative_path
        identity[relative_path] = sha256_file(path) if path.is_file() else "missing"
    return identity


def _model_identity_payload() -> dict[str, Any]:
    return {
        "provider": "ollama",
        "model": os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud").strip(),
        "base_url": _safe_endpoint_identity(
            os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
        ),
        "temperature": os.getenv("OLLAMA_TEMPERATURE", "0").strip(),
        "request_timeout_seconds": os.getenv(
            "LLM_REQUEST_TIMEOUT_SECONDS", "900"
        ).strip(),
        "max_retries": os.getenv("LLM_MAX_RETRIES", "1").strip(),
    }


def _embedding_identity_payload() -> dict[str, Any]:
    try:
        from vectorstore import qdrant_store

        model = str(qdrant_store.EMBEDDING_MODEL or "").strip()
        base_url = qdrant_store.EMBEDDING_BASE_URL
        query_instruction = qdrant_store.EMBEDDING_QUERY_INSTRUCTION
        document_instruction = qdrant_store.EMBEDDING_DOCUMENT_INSTRUCTION
        vector_size = qdrant_store.QDRANT_VECTOR_SIZE
        distance = str(qdrant_store.QDRANT_DISTANCE)
        qdrant_endpoint = (
            qdrant_store.QDRANT_LOCATION or qdrant_store.QDRANT_URL
        )
    except Exception:
        model = os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3").strip()
        base_url = os.getenv("OLLAMA_EMBEDDING_BASE_URL") or os.getenv(
            "OLLAMA_LOCAL_BASE_URL", "http://127.0.0.1:11434"
        )
        query_instruction = os.getenv("OLLAMA_EMBEDDING_QUERY_INSTRUCTION", "")
        document_instruction = os.getenv(
            "OLLAMA_EMBEDDING_DOCUMENT_INSTRUCTION", ""
        )
        vector_size = os.getenv("QDRANT_VECTOR_SIZE", "1024")
        distance = "Cosine"
        qdrant_endpoint = (
            os.getenv("QDRANT_LOCATION", "").strip()
            or os.getenv("QDRANT_URL", "").strip()
            or os.getenv("QDRANT_CLOUD_URL", "").strip()
        )
    return {
        "provider": "ollama",
        "model": model,
        "base_url": _safe_endpoint_identity(base_url),
        "query_instruction": str(query_instruction or "").strip(),
        "document_instruction": str(document_instruction or "").strip(),
        "vector_size": int(vector_size),
        "distance": distance,
        "qdrant_endpoint": _safe_endpoint_identity(qdrant_endpoint),
    }


def build_runtime_fingerprints(
    *,
    debug_trace: bool = False,
    skip_eval: bool = True,
) -> dict[str, Any]:
    """Fingerprint all output-affecting runtime inputs without storing secrets."""

    model_config = _model_identity_payload()
    embedding_config = _embedding_identity_payload()
    prompt_files = _source_files_identity(_PROMPT_IDENTITY_FILES)
    # The effective policy, not a re-typed copy of it: an env-only cap change
    # leaves the git tree untouched, so without this a resumed batch would reuse
    # answers produced under different limits.
    policy = active_policy()
    runtime_config = {
        "debug_trace": bool(debug_trace),
        "skip_eval": bool(skip_eval),
        "runtime_policy_fingerprint": policy.fingerprint(),
        "runtime_policy": policy.as_dict(),
        "domain_catalog_version": DOMAIN_CATALOG_VERSION,
        "domain_catalog_fingerprint": catalog_fingerprint(),
        "tool_result_facts_limit": 5,
        "environment": {
            name: os.getenv(name, default).strip()
            for name, default in _RUNTIME_CONFIG_DEFAULTS.items()
        },
        "source_files": _source_files_identity(_CONFIG_IDENTITY_FILES),
    }
    worktree = git_worktree_provenance(ROOT_DIR)
    code_sha256 = str(worktree.get("code_sha256", "") or "").strip()
    if not code_sha256:
        # Installed source trees still need a deterministic identity when Git
        # metadata is unavailable. This fallback covers every configured
        # runtime/prompt file and fails closed when one changes.
        code_sha256 = stable_json_fingerprint(
            {
                "scheme": "runtime-source-files-fallback-v1",
                "prompt_files": prompt_files,
                "runtime_files": runtime_config["source_files"],
            }
        )
    code_provenance = {
        "scheme": str(
            worktree.get("scheme", "")
            or "runtime-source-files-fallback-v1"
        ),
        "git_revision": str(worktree.get("git_revision", "") or ""),
        "worktree_diff_sha256": worktree.get("worktree_diff_sha256"),
        "worktree_dirty": worktree.get("worktree_dirty"),
        "relevant_untracked_files_n": worktree.get(
            "relevant_untracked_files_n"
        ),
        "error": str(worktree.get("error", "") or ""),
    }
    return {
        "code": code_sha256,
        "code_provenance": code_provenance,
        "model": stable_json_fingerprint(model_config),
        "embedding": stable_json_fingerprint(embedding_config),
        "prompt": stable_json_fingerprint(prompt_files),
        "config": stable_json_fingerprint(runtime_config),
        "model_config": model_config,
        "embedding_config": embedding_config,
    }


def dataset_identity_payload(dataset_meta: dict | None) -> dict[str, Any]:
    metadata = dict(dataset_meta or {})
    dataset_config = {
        key: metadata.get(key)
        for key in (
            "dataset_id",
            "company",
            "ticker",
            "fiscal_year",
            "fiscal_quarter",
            "scope",
            "audit_status",
            "file_path",
            "ingestion_version",
            "vector_collection_name",
            "source_sha256",
            "source_converter_identity",
            "facts_sha256",
            "parser_version",
            "kb_schema_version",
        )
    }
    dataset_generation = str(metadata.get("dataset_generation", "") or "").strip()
    if not dataset_generation:
        dataset_generation = stable_json_fingerprint(dataset_config)
    kb_generation = str(metadata.get("kb_generation", "") or "").strip()
    index_generation = str(
        metadata.get("index_generation", "")
        or metadata.get("collection_generation", "")
        or metadata.get("index_fingerprint", "")
        or ""
    ).strip()
    return {
        "dataset_id": str(
            metadata.get("dataset_id", "") or metadata.get("id", "") or ""
        ).strip(),
        "dataset_generation": dataset_generation,
        "kb_generation": kb_generation,
        "index_generation": index_generation,
        "vector_collection_name": str(
            metadata.get("vector_collection_name", "") or ""
        ).strip(),
    }


def build_selection_contract(
    records: list[dict] | None,
    *,
    full: bool,
    limit: int | None,
    offset: int = 0,
    seed_population_count: int | None = None,
) -> dict[str, Any]:
    selected_records = [item for item in (records or []) if isinstance(item, dict)]
    selected_count = len(selected_records)
    full_seed_selection = bool(
        full
        and int(offset) == 0
        and seed_population_count is not None
        and selected_count == seed_population_count
    )
    return {
        "full": bool(full),
        "offset": int(offset),
        "limit": None if full else limit,
        "selected_count": selected_count,
        "seed_population_count": seed_population_count,
        "is_full_seed_selection": full_seed_selection,
        "selected_query_ids": [item.get("id") for item in selected_records],
        "selected_sample_keys": [prediction_key(item) for item in selected_records],
    }


def _seed_population_count(seed_path: Path) -> int | None:
    if not seed_path.is_file():
        return None
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return len(payload) if isinstance(payload, list) else None


def build_run_identity(
    *,
    seed_file: str | Path,
    dataset_meta: dict,
    selected_records: list[dict] | None,
    full: bool,
    limit: int | None,
    offset: int = 0,
    debug_trace: bool = False,
    skip_eval: bool = True,
) -> dict[str, Any]:
    seed_path = Path(seed_file)
    seed_checksum = sha256_file(seed_path) if seed_path.is_file() else ""
    selection = build_selection_contract(
        selected_records,
        full=full,
        limit=limit,
        offset=offset,
        seed_population_count=_seed_population_count(seed_path),
    )
    dataset_identity = dataset_identity_payload(dataset_meta)
    runtime = build_runtime_fingerprints(
        debug_trace=debug_trace,
        skip_eval=skip_eval,
    )
    identity = {
        "identity_version": RUN_IDENTITY_VERSION,
        "seed_sha256": seed_checksum,
        "selection": selection,
        "dataset": dataset_identity,
        "code_provenance": dict(runtime["code_provenance"]),
        "fingerprints": {
            "code": runtime["code"],
            "seed": seed_checksum,
            "selection": stable_json_fingerprint(selection),
            "query": stable_json_fingerprint(
                {
                    "selected_query_ids": selection["selected_query_ids"],
                    "selected_sample_keys": selection["selected_sample_keys"],
                }
            ),
            "dataset": stable_json_fingerprint(dataset_identity),
            "index": stable_json_fingerprint(
                {
                    "dataset_id": dataset_identity["dataset_id"],
                    "collection": dataset_identity["vector_collection_name"],
                    "generation": dataset_identity["index_generation"],
                }
            ),
            "embedding": runtime["embedding"],
            "prompt": runtime["prompt"],
            "model": runtime["model"],
            "config": runtime["config"],
        },
    }
    identity["run_fingerprint"] = stable_json_fingerprint(identity)
    return identity
