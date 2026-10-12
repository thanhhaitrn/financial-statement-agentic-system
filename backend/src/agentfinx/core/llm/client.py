"""Create the chat model client lazily from validated environment settings."""
# Code note: LLM modules isolate provider/client behavior from graph orchestration.

import os
from functools import lru_cache
from urllib.parse import urlparse

from langchain_ollama import ChatOllama

from agentfinx.core.config import paths

paths.load_env()


LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "900"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))


def _is_local_base_url(base_url: str) -> bool:
    parsed = urlparse(str(base_url or ""))
    host = (parsed.hostname or "").lower()
    return host in {"", "localhost", "127.0.0.1", "::1"}


def build_llm(*, timeout: float | None = None, json_mode: bool = False) -> ChatOllama:
    """A client with its own HTTP timeout: a caller with a time budget passes
    what is left of it, so a slow call is cut off at the socket, not merely
    abandoned while it keeps running."""
    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com").strip()
    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not api_key and not _is_local_base_url(base_url):
        raise RuntimeError("Missing OLLAMA_API_KEY for non-local OLLAMA_BASE_URL")

    seconds = LLM_REQUEST_TIMEOUT_SECONDS if timeout is None else max(float(timeout), 1.0)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    kwargs = {
        "model": os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud"),
        "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0")),
        "base_url": base_url,
        "sync_client_kwargs": {"timeout": seconds},
        "async_client_kwargs": {"timeout": seconds},
    }
    if json_mode:
        kwargs["format"] = "json"
    if headers:
        kwargs["headers"] = headers
    return ChatOllama(**kwargs)


@lru_cache(maxsize=1)
def get_llm() -> ChatOllama:
    """Return one configured client, creating it only on first model call."""
    return build_llm()


def get_llm_identity() -> tuple[str, str]:
    return (
        os.getenv("OLLAMA_BASE_URL", "https://ollama.com").strip(),
        os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud").strip(),
    )


class _LazyLLMProxy:
    """Compatibility proxy for existing ``from llm.client import llm`` calls."""

    def __getattr__(self, name):
        return getattr(get_llm(), name)


llm = _LazyLLMProxy()
