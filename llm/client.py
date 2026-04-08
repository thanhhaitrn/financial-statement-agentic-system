import os
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

"""def create_llm():
    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "qwen3-vl:235b"),
        temperature=float(os.getenv("OLLAMA_TEMPERATURE", 0.2)),
        base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
        headers={
            "Authorization": f"Bearer {os.getenv('OLLAMA_API_KEY')}"
        }
    )"""

"""llm = ChatOllama(
    model="qwen2.5:7b",
    temperature=0,
    base_url="http://localhost:11434",
)"""


OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
if not OLLAMA_API_KEY:
    raise RuntimeError("Missing OLLAMA_API_KEY env var")

LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "420"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))

"""llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL", "qwen3-vl:235b-cloud"),
    temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0")),
    base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
)"""

llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud"),
    temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0")),
    base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
)


"""BASE_URL = 'https://hummocky-kellee-supercandid.ngrok-free.dev/v1'
llm = ChatOpenAI(
    base_url=BASE_URL,
    api_key = 'empty',
    model= "Qwen/Qwen2.5-3B-Instruct",
    temperature = 0.0,
    request_timeout = LLM_REQUEST_TIMEOUT_SECONDS,
    max_retries = LLM_MAX_RETRIES,
)"""
