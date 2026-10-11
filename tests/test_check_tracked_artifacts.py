import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_tracked_artifacts.py"
_spec = importlib.util.spec_from_file_location("check_tracked_artifacts", _SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_web_runtime_artifacts_are_blocked():
    blocked = dict(check.find_violations([
        "web/node_modules/react/index.js",
        "web/.next/BUILD_ID",
        "web/.env.local",
        "web/backend/finx.db",
        "web/prisma/dev.db-journal",
    ]))
    assert set(blocked) == {
        "web/node_modules/react/index.js",
        "web/.next/BUILD_ID",
        "web/.env.local",
        "web/backend/finx.db",
        "web/prisma/dev.db-journal",
    }


def test_web_sources_and_templates_pass():
    assert check.find_violations([
        "web/app/chat/page.tsx",
        "web/package.json",
        "web/package-lock.json",
        "web/next.config.mjs",
        "web/.env.example",
    ]) == []


def test_web_manifests_are_not_hidden_by_the_json_ignore_rule():
    # The root .gitignore drops *.json; without its exceptions a commit would
    # silently leave out the files `npm ci` needs.
    import subprocess

    root = _SCRIPT.parents[1]
    for path in ("web/package.json", "web/package-lock.json", "web/tsconfig.json"):
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=root)
        assert result.returncode == 1, f"{path} is git-ignored"


def test_storage_and_artifacts_keep_only_their_gitignore():
    blocked = dict(check.find_violations([
        "storage/datasets/registry.json",
        "storage/reports/report.md",
        "artifacts/eval/apec_predictions.json",
        "storage/.gitignore",
        "artifacts/.gitignore",
    ]))
    assert set(blocked) == {
        "storage/datasets/registry.json",
        "storage/reports/report.md",
        "artifacts/eval/apec_predictions.json",
    }
