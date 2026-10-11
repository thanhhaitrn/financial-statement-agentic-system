"""The pipeline module must not be named ``test``.

Python's standard library ships a ``test`` package. It wins over the
repository's module whenever the repository root is not first on ``sys.path``
-- which is how the installed ``agentfinx`` command runs the web workers. A
chat question then failed with ``module 'test' has no attribute
'execute_query'``, although every in-repo test passed.
"""

import importlib
import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import finx_pipeline

ROOT = Path(__file__).resolve().parents[1]
_IMPORTS_TEST = re.compile(r"^\s*(?:from\s+test\s+import\b|import\s+test\b)", re.MULTILINE)
_SKIP_DIRS = {".venv", "tests", "ragas_runs", "dataset_store", "storage", "artifacts", "build", "dist", "node_modules"}


def test_library_code_never_imports_the_test_module():
    offenders = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in _SKIP_DIRS or relative.name == "test.py" or ".egg-info" in str(relative):
            continue
        if _IMPORTS_TEST.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(relative))
    assert offenders == []


def test_the_compatibility_shim_is_the_pipeline_module():
    assert importlib.import_module("test") is finx_pipeline


def _installed() -> bool:
    try:
        importlib.metadata.distribution("agentfinx")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


@pytest.mark.skipif(not _installed(), reason="needs the agentfinx package installed (pip install -e .)")
def test_the_pipeline_imports_outside_the_repository(tmp_path):
    code = (
        "import finx_pipeline\n"
        "from agentfinx.cli import _load_main\n"
        "from webapp.chat_runner import ChatDependencies\n"
        "execute, collect, _render = ChatDependencies(shared_dataset_ids=frozenset()).pipeline()\n"
        "assert execute is finx_pipeline.execute_query and collect is finx_pipeline.collect_pipeline_errors\n"
        "assert _load_main('finx_pipeline') is finx_pipeline.main\n"
        "print(finx_pipeline.__file__)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == (ROOT / "finx_pipeline.py").resolve()
