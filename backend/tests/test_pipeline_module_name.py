"""The backend is one installed package that runs from any directory.

Python's standard library ships a ``test`` package, which once shadowed a
repository module of that name: the web workers failed with ``module 'test'
has no attribute 'execute_query'`` although every in-repo test passed. Code
now lives only under ``agentfinx``; these tests keep it importable and
runnable without the repository on ``sys.path``.
"""

import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import agentfinx

PACKAGE = Path(agentfinx.__file__).resolve().parent
_IMPORTS_TEST = re.compile(r"^\s*(?:from\s+test\s+import\b|import\s+test\b)", re.MULTILINE)
# Top-level names of the layout before backend/src/agentfinx.
_OLD_TOP_LEVEL = re.compile(
    r"^\s*(?:from|import)\s+(?:webapp|agents|tools|config|schemas|llm|common|dataset_catalog|kb|vectorstore|"
    r"graph|finx_pipeline|output_formatter|ingestion|acquisition|web_evidence|web_analysis|evaluation|"
    r"dataset_batch_runner|dataset_batch_result|ragas_eval_runner|eval_retrieval_recall|analyze_batch_metrics|"
    r"analyze_prediction_errors|scripts)\b",
    re.MULTILINE,
)


def test_library_code_never_imports_the_test_module_or_the_old_layout():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if _IMPORTS_TEST.search(source) or _OLD_TOP_LEVEL.search(source):
            offenders.append(str(path.relative_to(PACKAGE)))
    assert offenders == []


def test_tests_import_only_the_package():
    offenders = [path.name for path in Path(__file__).parent.glob("*.py")
                 if _OLD_TOP_LEVEL.search(path.read_text(encoding="utf-8", errors="ignore"))]
    assert offenders == []


def _installed() -> bool:
    try:
        importlib.metadata.distribution("agentfinx")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


@pytest.mark.skipif(not _installed(), reason="needs the agentfinx package installed (pip install -e backend)")
def test_the_pipeline_imports_outside_the_repository(tmp_path):
    code = (
        "import agentfinx.workflow.pipeline as pipeline\n"
        "from agentfinx.cli import _load_main\n"
        "from agentfinx.api.chat_runner import ChatDependencies\n"
        "execute, collect, _render = ChatDependencies(shared_dataset_ids=frozenset()).pipeline()\n"
        "assert execute is pipeline.execute_query and collect is pipeline.collect_pipeline_errors\n"
        "assert _load_main('agentfinx.workflow.pipeline') is pipeline.main\n"
        "print(pipeline.__file__)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == PACKAGE / "workflow" / "pipeline.py"
