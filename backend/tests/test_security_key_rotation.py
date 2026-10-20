"""Credential failover preserves completed QA runs and the shared call budget."""

import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from agentfinx.core import experiment
from agentfinx.core.llm.keys import load_keys
from agentfinx.evaluation.security import cases, runner
from agentfinx.evaluation.security.key_rotation import KeyPool, quota_reason


def test_loader_requires_opt_in_for_commented_keys_and_deduplicates(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# OLLAMA_API_KEY="backup-secret"\n'
                    'OLLAMA_API_KEY=active-secret # account\n'
                    'OLLAMA_API_KEY=active-secret\n'
                    '# prose OLLAMA_API_KEY=ignored\n'
                    'OTHER_KEY=ignored\n')
    assert load_keys(path) == ["active-secret"]
    assert load_keys(path, include_commented=True) == ["backup-secret", "active-secret"]


@pytest.mark.parametrize("message", [
    "ResponseError: too many requests (status code: 429)",
    "Ollama session usage limit exceeded", "usage_limit", "insufficient_quota",
    "You have reached your usage limit", "provider quota exhausted",
])
def test_detects_provider_quota_from_structured_errors(message):
    assert quota_reason({"status": "ok", "model_call_errors": [{"error": message}]})
    assert quota_reason({"status": "runtime_error", "errors": [message]})


@pytest.mark.parametrize("result", [
    {"status": "ok", "answer": "HTTP 429 usage_limit"},
    {"status": "ok", "errors": ["HTTP 429"]},
    {"status": "runtime_error", "errors": ["follow-up limit"]},
    {"status": "runtime_error", "errors": ["401 Unauthorized"]},
    {"status": "runtime_error", "errors": ["500 server error"]},
    {"status": "runtime_error", "errors": ["ConnectionError"]},
    {"status": "runtime_error", "errors": ["UnexpectedResponse: 429 Too Many Requests"]},
    {"status": "runtime_error", "errors": ["Qdrant HTTP 429 quota exceeded"]},
    {"status": "budget_exhausted", "model_call_errors": [{"error": "HTTP 429"}]},
])
def test_does_not_rotate_for_model_text_other_failures_or_local_budget(result):
    assert quota_reason(result) == ""


def make_pilot(tmp_path, keys, *, limit=300):
    pilot = runner.Pilot(tmp_path / "pilot", cases.load_cases(), case_ids=["I1"], budget=limit, keys=keys)
    manifest = pilot.prepare(fingerprint_value={"sha256": "same", "payload": {}})
    manifest["schedule"] = manifest["schedule"][:2]
    pilot.manifest_path.write_text(json.dumps(manifest))
    return pilot


def install_launcher(pilot, monkeypatch, statuses):
    seen = []

    def launch(spec):
        # Consume the same persisted budget as real model callbacks.
        experiment.reserve_call(run_id=spec["run_id"], budget_path=str(pilot.budget_path))
        selected = pilot.key_pool.select()
        seen.append((spec["run_id"], selected[1]))
        result = {key: spec[key] for key in ("run_id", "case_id", "config", "condition", "attempt")}
        status = next(statuses)
        result.update(status=status, errors=[], model_calls=1)
        if status == "quota":
            result.update(status="ok", model_call_errors=[{"error": "HTTP 429"}], answer="partial")
        return result

    monkeypatch.setattr(pilot, "_launch", launch)
    return seen


def test_rotation_retries_same_slot_with_new_key_and_keeps_success_on_resume(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["secret-one", "secret-two"])
    seen = install_launcher(pilot, monkeypatch, iter(["quota", "ok", "ok"]))
    status = pilot.run(log=lambda *_: None)
    assert status["stopped"] == ""
    assert [key for _, key in seen] == ["secret-one", "secret-two", "secret-two"]
    assert seen[0][0].rsplit("-a", 1)[0] == seen[1][0].rsplit("-a", 1)[0]
    assert pilot.results()[seen[0][0]]["status"] == "provider_quota"
    assert pilot.budget_state()["used"] == 3
    assert pilot.run(log=lambda *_: None)["runs"] == 3
    assert len(seen) == 3
    for path in pilot.out.rglob("*.json*"):
        text = path.read_text()
        assert "secret-one" not in text and "secret-two" not in text


def test_all_keys_exhausted_stop_and_new_key_resumes_without_repeating_failures(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["one", "two"])
    seen = install_launcher(pilot, monkeypatch, iter(["quota", "quota"]))
    assert pilot.run(log=lambda *_: None)["stopped"] == "all_keys_exhausted"
    assert len(seen) == 2
    assert pilot.run(log=lambda *_: None)["stopped"] == "all_keys_exhausted"
    assert len(seen) == 2
    resumed = runner.Pilot(pilot.out, pilot.cases, case_ids=["I1"], keys=["one", "two", "three"])
    new = install_launcher(resumed, monkeypatch, iter(["ok", "ok"]))
    assert resumed.run(log=lambda *_: None)["stopped"] == ""
    assert new[0][0].endswith("-a3")
    assert [key for _, key in new] == ["three", "three"]
    assert resumed.budget_state()["used"] == 4


def test_budget_limit_takes_precedence_over_rotation(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["one", "two"], limit=1)
    seen = install_launcher(pilot, monkeypatch, iter(["quota"]))
    assert pilot.run(log=lambda *_: None)["stopped"] == "budget_exhausted"
    assert len(seen) == 1
    assert pilot.budget_state()["used"] == 1


def test_network_retry_keeps_key_and_is_bounded(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["one", "two"])
    seen = install_launcher(pilot, monkeypatch, iter(["runtime_error", "runtime_error", "ok"]))
    pilot.run(log=lambda *_: None)
    assert [key for _, key in seen] == ["one"] * 3
    assert pilot.key_pool.state["exhausted"] == []


def test_explicit_quota_reset_preserves_budget_and_processed_failures(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["one"])
    install_launcher(pilot, monkeypatch, iter(["quota"]))
    pilot.run(log=lambda *_: None)
    resumed = runner.Pilot(pilot.out, pilot.cases, case_ids=["I1"], keys=["one"], retry_exhausted_keys=True)
    seen = install_launcher(resumed, monkeypatch, iter(["ok", "ok"]))
    resumed.run(log=lambda *_: None)
    assert seen[0][0].endswith("-a2")
    assert resumed.budget_state()["used"] == 3


def test_lock_blocks_duplicate_runner(tmp_path):
    pilot = make_pilot(tmp_path, ["one"])
    with open(pilot.out / "pilot.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another pilot runner"):
            pilot.run()


def test_secret_is_only_passed_in_subprocess_environment(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path, ["private-key-value"])
    spec = pilot._spec({"case_id": "I1", "config": "C", "condition": "clean"}, 1)
    spec["key_id"] = pilot.key_pool.select()[0]

    def run(command, **kwargs):
        assert kwargs["env"]["OLLAMA_API_KEY"] == "private-key-value"
        assert "private-key-value" not in str(command)
        assert "private-key-value" not in Path(spec["run_dir"], "spec.json").read_text()
        Path(spec["run_dir"], "result.json").write_text('{"status":"ok"}')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert pilot._launch(spec)["status"] == "ok"


def test_pool_state_uses_hashes_and_survives_key_reordering(tmp_path):
    path = tmp_path / "state.json"
    pool = KeyPool(["one", "two"], path)
    first = pool.select()[0]
    pool.exhausted(first, "r1")
    assert KeyPool(["two", "one"], path).select()[1] == "two"
