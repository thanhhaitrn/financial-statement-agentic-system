"""Boundaries between parts with similar names, checked on the imports.

- news.collect fetches and caches articles and never calls a model;
- news.analysis reads only the articles it is handed and never fetches: no
  network library, nothing from news.collect, and from the acquisition side
  only the local symbol catalog;
- news.dates and news.topics are shared, pure helpers;
- evaluation.predict and evaluation.batch share evaluation.runner instead of
  importing each other.
"""

import ast
from pathlib import Path

import agentfinx

PACKAGE = Path(agentfinx.__file__).resolve().parent
NETWORK = {"urllib.request", "http.client", "socket", "requests", "httpx", "ssl"}
MODEL = {"agentfinx.core.llm", "agentfinx.news.analysis", "langchain_ollama", "langchain_core", "ollama"}


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _violations(folder: str, forbidden: set[str], *, allowed: set[str] = frozenset()) -> list[str]:
    found = []
    for path in sorted((PACKAGE / folder).rglob("*.py")) if (PACKAGE / folder).is_dir() else [PACKAGE / folder]:
        for name in _imports(path) - allowed:
            if any(name == bad or name.startswith(bad + ".") for bad in forbidden):
                found.append(f"{path.relative_to(PACKAGE)} imports {name}")
    return found


def test_news_collection_never_calls_a_model():
    assert _violations("news/collect", MODEL) == []


def test_news_analysis_never_fetches():
    forbidden = NETWORK | {"agentfinx.news.collect", "agentfinx.acquisition", "agentfinx.agents.tools.http_safety"}
    allowed = {
        "agentfinx.acquisition.symbols", "agentfinx.acquisition.symbols.SymbolCatalog",
        "agentfinx.acquisition.symbols.fold_text",
        # A pure check of a link's host; nothing is opened.
        "agentfinx.agents.tools.http_safety", "agentfinx.agents.tools.http_safety.allowed_https_url",
    }
    assert _violations("news/analysis", forbidden, allowed=allowed) == []


def test_shared_news_helpers_stay_pure():
    forbidden = NETWORK | MODEL | {"agentfinx.news.collect", "agentfinx.acquisition"}
    assert _violations("news/dates.py", forbidden) == []
    assert _violations("news/topics.py", forbidden) == []


def test_predict_and_batch_do_not_import_each_other():
    assert not any(name.startswith("agentfinx.evaluation.batch")
                   for name in _imports(PACKAGE / "evaluation" / "predict.py"))
    assert not any(name.startswith("agentfinx.evaluation.predict")
                   for name in _imports(PACKAGE / "evaluation" / "runner.py"))
