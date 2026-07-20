"""Create the chat model client from local environment configuration."""
# Code note: LLM modules isolate provider/client behavior from graph orchestration.

import os

from langchain_ollama import ChatOllama
from dotenv import load_dotenv

load_dotenv()


OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
if not OLLAMA_API_KEY:
    raise RuntimeError("Missing OLLAMA_API_KEY env var")

LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "900"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))

# LangChain's Ollama client is used for both local and Ollama cloud models.
llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud"),
    temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0")),
    base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
    sync_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
    async_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
)
