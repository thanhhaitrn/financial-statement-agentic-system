import os
from langchain_ollama import ChatOllama

"""def create_llm():
    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "qwen3-vl:235b"),
        temperature=float(os.getenv("OLLAMA_TEMPERATURE", 0.2)),
        base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
        headers={
            "Authorization": f"Bearer {os.getenv('OLLAMA_API_KEY')}"
        }
    )"""

llm = ChatOllama(
    model="qwen2.5:7b",
    temperature=0,
    base_url="http://localhost:11434",
)

