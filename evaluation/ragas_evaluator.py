"""Construction of the RAGAS judge, embeddings and metric objects.

Extracted from ``ragas_eval_runner`` so that module can focus on running the
evaluation loop and writing reports. Everything here is pure setup: it decides
which judge LLM / embedding backend to use and instantiates the four RAGAS
metrics, with all heavy third-party imports done lazily inside the functions.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any


def _ragas_available() -> bool:
    return importlib.util.find_spec("ragas") is not None


def _set_metric_name(metric: Any, name: str) -> Any:
    if not name:
        return metric
    try:
        metric.name = name
    except Exception:
        try:
            object.__setattr__(metric, "name", name)
        except Exception:
            pass
    return metric


def _instantiate_metric(metric_cls: Any, *, name: str = "") -> Any:
    if name:
        try:
            return metric_cls(name=name)
        except TypeError:
            pass
    return _set_metric_name(metric_cls(), name)


def _metric_from_module(module: Any, instance_name: str, class_names: list[str], *, name: str) -> Any:
    candidate = getattr(module, instance_name, None)
    if candidate is not None:
        if isinstance(candidate, type):
            return _instantiate_metric(candidate, name=name)
        return _set_metric_name(candidate, name)

    for class_name in class_names:
        candidate = getattr(module, class_name, None)
        if candidate is not None:
            return _instantiate_metric(candidate, name=name)

    options = ", ".join([instance_name, *class_names])
    raise ImportError(f"Cannot find RAGAs metric: {options}")


def _load_metric_objects() -> list[Any]:
    import ragas.metrics as metrics

    return [
        _metric_from_module(metrics, "faithfulness", ["Faithfulness"], name="faithfulness"),
        _metric_from_module(metrics, "answer_relevancy", ["ResponseRelevancy"], name="answer_relevancy"),
        _metric_from_module(
            metrics,
            "context_precision",
            ["LLMContextPrecisionWithReference", "LLMContextPrecisionWithoutReference"],
            name="context_precision",
        ),
        _metric_from_module(metrics, "context_recall", ["LLMContextRecall"], name="context_recall"),
    ]


def _build_evaluator_embeddings():
    from llm.client import LLM_REQUEST_TIMEOUT_SECONDS
    from langchain_ollama import OllamaEmbeddings
    from vectorstore.qdrant_store import EMBEDDING_BASE_URL, EMBEDDING_MODEL, _ollama_client_kwargs

    base_url = (
        os.getenv("OLLAMA_EVAL_EMBEDDING_BASE_URL", "").strip()
        or os.getenv("OLLAMA_EMBEDDING_BASE_URL", "").strip()
        or os.getenv("OLLAMA_BASE_URL", "").strip()
        or EMBEDDING_BASE_URL
    )
    model = os.getenv("OLLAMA_EVAL_EMBEDDING_MODEL", "").strip() or EMBEDDING_MODEL

    return OllamaEmbeddings(
        model=model,
        base_url=base_url,
        client_kwargs=_ollama_client_kwargs(base_url),
        sync_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
        async_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
    )


def _build_evaluator_llm():
    from dotenv import load_dotenv

    load_dotenv()
    timeout = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "900"))

    # Opt-in Claude judge (dormant unless ANTHROPIC_API_KEY is set — needs an
    # Anthropic API key, which is separate from a Claude Pro subscription). The
    # local gpt-oss judge proved unreliable for Vietnamese (scored faithfulness=0
    # on answers grounded verbatim in the context). When a key is available this
    # gives trustworthy scores and isn't session-rate-limited.
    judge = os.getenv("RAGAS_JUDGE", "").strip().lower()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if judge == "anthropic" or (judge != "ollama" and anthropic_key):
        if not anthropic_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY env var for the RAGAs Claude judge")
        from langchain_anthropic import ChatAnthropic

        # No temperature: Claude Opus 4.8/4.7 reject sampling params (400).
        # Override the model with RAGAS_JUDGE_MODEL (e.g. claude-sonnet-4-6).
        return ChatAnthropic(
            model=os.getenv("RAGAS_JUDGE_MODEL", "claude-opus-4-8"),
            max_tokens=int(os.getenv("RAGAS_JUDGE_MAX_TOKENS", "4096")),
            timeout=timeout,
            max_retries=int(os.getenv("RAGAS_JUDGE_MAX_RETRIES", "4")),
            api_key=anthropic_key,
        )

    from langchain_ollama import ChatOllama

    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OLLAMA_API_KEY env var")
    # RAGAS_JUDGE_MODEL overrides the judge model independently of the agent's
    # OLLAMA_MODEL. qwen3-coder:480b judged Vietnamese far better than
    # gpt-oss:120b-cloud (which floored faithfulness/recall to 0 on answers
    # grounded verbatim in the context) but was retired by Ollama Cloud on
    # 2026-07-15. minimax-m3 reproduced qwen3-coder's reference scores on an
    # A/B probe (exact match on faithfulness/context_precision/context_recall)
    # and is the strongest judge still on the free tier.
    return ChatOllama(
        model=os.getenv("RAGAS_JUDGE_MODEL", "").strip() or os.getenv("OLLAMA_MODEL", "minimax-m3"),
        temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0")),
        base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
        headers={"Authorization": f"Bearer {api_key}"},
        sync_client_kwargs={"timeout": timeout},
        async_client_kwargs={"timeout": timeout},
    )


def _metric_name(metric: Any) -> str:
    return str(getattr(metric, "name", "") or metric.__class__.__name__).strip()
