"""FINX_HOME/.env applies to every command the same way, and paths in the
configuration never depend on the current directory."""

import os
import subprocess
import sys
from pathlib import Path

from agentfinx.api.settings import load_settings
from agentfinx.core.config import paths


def _run(code: str, home: Path, cwd: Path, extra_path: Path | None = None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("WEB_", "FINX_")) and key != "PYTHONPATH"}
    env["FINX_HOME"] = str(home)
    if extra_path is not None:
        env["PYTHONPATH"] = str(extra_path)
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=180)


def test_the_env_file_is_loaded_before_any_command_module(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("WEB_EVIDENCE_ENABLED=1\nWEB_ANALYSIS_ENABLED=1\n", encoding="utf-8")
    probe = tmp_path / "probe"
    probe.mkdir()
    # A command module that builds on the runtime policy as soon as it is imported.
    (probe / "policy_probe.py").write_text(
        "from agentfinx.core.config.runtime_policy import DEFAULT_POLICY\n"
        "ACTIVE = DEFAULT_POLICY.web.analysis_active\n"
        "def main(argv):\n"
        "    print('analysis_active', ACTIVE)\n"
        "    return 0\n", encoding="utf-8")
    code = ("import agentfinx.cli as cli\n"
            "cli._COMMANDS['probe'] = ('policy_probe', True)\n"
            "raise SystemExit(cli.main(['probe']))\n")
    result = _run(code, home, tmp_path, extra_path=probe)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "analysis_active True" in result.stdout

    # Batch and predict build the policy on import, before any model client.
    for module in ("agentfinx.evaluation.batch", "agentfinx.evaluation.predict"):
        result = _run(f"import {module}\nfrom agentfinx.core.config.runtime_policy import DEFAULT_POLICY\n"
                      "print(DEFAULT_POLICY.web.analysis_active)\n", home, tmp_path)
        assert result.stdout.strip() == "True", (module, result.stderr[-2000:])


def test_storage_status_reports_the_database_named_in_the_env_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("FINX_WEB_DB=accounts/web.sqlite3\n", encoding="utf-8")
    result = _run("import agentfinx.cli as cli\nraise SystemExit(cli.main(['storage', 'status']))\n",
                  home, tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert str(home / "accounts" / "web.sqlite3") in result.stdout


def test_a_relative_web_database_is_relative_to_finx_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    monkeypatch.setenv("FINX_HOME", str(home))
    monkeypatch.chdir(elsewhere)

    expected = (home / "accounts.sqlite3").resolve()
    assert paths.web_db_path({"FINX_WEB_DB": "accounts.sqlite3"}) == expected
    settings = load_settings({"FINX_SECRET_KEY": "s" * 40, "FINX_WEB_DB": "accounts.sqlite3"})
    assert settings.db_path == expected and settings.db_path.as_uri()

    from agentfinx.api import cli as webcli

    monkeypatch.setattr(webcli, "_prepare_environment", lambda *args: {"FINX_WEB_DB": "accounts.sqlite3"})
    assert webcli.db_main(["init"]) == 0
    assert expected.is_file() and not (elsewhere / "accounts.sqlite3").exists()
    assert webcli._require_database(settings.db_path, "agentfinx serve")  # opened as a URI, no ValueError
