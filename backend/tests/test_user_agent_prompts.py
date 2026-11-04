"""Prompt ownership, run snapshots and runtime isolation; no model calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from test_webapp import Web, ORIGIN, _alice_session, _deps, _submit, register
from agentfinx.api import db as webdb
from agentfinx.api.worker import run_chat_once
from agentfinx.agents.profiles import AGENT_PROFILES, get_agent_profile
from agentfinx.agents.planner_web import web_planner_instruction, DISCOVERY_ADDENDUM
from agentfinx.core.config.agent_prompts import activate, resolve, MAX_PROMPT_LENGTH


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    return Web(tmp_path)


def put(client, prompts, *, origin=ORIGIN):
    return client.put("/api/me/agent-prompts", json={"prompts": prompts}, headers={"Origin": origin})


def test_prompts_are_authenticated_owner_scoped_and_can_reset(web):
    assert web.client().get("/api/me/agent-prompts").status_code == 401
    alice, _, _, _ = _alice_session(web)
    initial = alice.get("/api/me/agent-prompts").json()
    assert len(initial["agents"]) == 9
    assert initial["prompts"] == {}
    assert all(set(agent) == {"id", "label", "description"} for agent in initial["agents"])
    assert "default_prompt" not in json.dumps(initial)
    for profile in AGENT_PROFILES.values():
        assert profile["system_instruction"] not in json.dumps(initial, ensure_ascii=False)
    custom = {"agent_synth": "Trình bày ngắn gọn. Giữ nguyên JSON."}
    assert put(alice, custom).status_code == 200
    bob = web.client()
    register(bob, "Bob", "bob@example.com")
    assert bob.get("/api/me/agent-prompts").json()["prompts"] == {}
    assert put(bob, {"agent_planner": "Bob prompt"}).status_code == 200
    assert alice.get("/api/me/agent-prompts").json()["prompts"] == custom
    assert put(alice, {}).status_code == 200
    assert alice.get("/api/me/agent-prompts").json()["prompts"] == {}


@pytest.mark.parametrize("prompts", [
    {"unknown_agent": "prompt"}, {"agent_synth": "   "},
    {"agent_synth": "a" * (MAX_PROMPT_LENGTH + 1)}, {"agent_synth": 123},
])
def test_invalid_prompts_do_not_overwrite_saved_values(web, prompts):
    alice, _, _, _ = _alice_session(web)
    valid = {"agent_synth": "Original prompt"}
    assert put(alice, valid).status_code == 200
    assert put(alice, prompts).status_code == 422
    assert alice.get("/api/me/agent-prompts").json()["prompts"] == valid


def test_prompt_updates_require_allowed_origin(web):
    alice, _, _, _ = _alice_session(web)
    assert put(alice, {"agent_synth": "changed"}, origin="https://foreign.example").status_code == 403
    assert alice.get("/api/me/agent-prompts").json()["prompts"] == {}


def test_queued_run_uses_submission_snapshot_and_restores_context(web):
    alice, _, _, session = _alice_session(web)
    old = {"agent_synth": "Prompt at submission", "news_select": "Old news prompt"}
    assert put(alice, old).status_code == 200
    run_id = _submit(alice, session)
    assert put(alice, {"agent_synth": "New prompt"}).status_code == 200
    with web.db.read() as conn:
        assert json.loads(webdb.get_run(conn, run_id)["agent_prompts_json"]) == old
    observed = []
    deps = _deps(web)
    execute = deps.execute

    def inspect(*args, **kwargs):
        observed.append(get_agent_profile("agent_synth")["system_instruction"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(copy_context().run, resolve, "news_select", "default").result())
        return execute(*args, **kwargs)

    deps.execute = inspect
    assert run_chat_once(web.db, "prompt-test", deps)
    assert observed[0].startswith(AGENT_PROFILES["agent_synth"]["system_instruction"])
    assert old["agent_synth"] in observed[0]
    assert observed[1].startswith("default") and old["news_select"] in observed[1]
    assert get_agent_profile("agent_synth")["system_instruction"] == AGENT_PROFILES["agent_synth"]["system_instruction"]
    second_id = _submit(alice, session, key="key-second-prompt")
    with web.db.read() as conn:
        assert json.loads(webdb.get_run(conn, second_id)["agent_prompts_json"]) == {"agent_synth": "New prompt"}


def test_context_isolated_for_parallel_users_and_resets_after_failure():
    def check(prompt):
        with activate({"agent_planner": prompt}):
            return web_planner_instruction()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(check, ["Alice", "Bob"]))
    for output, name, other in zip(outputs, ["Alice", "Bob"], ["Bob", "Alice"]):
        assert output.startswith(AGENT_PROFILES["agent_planner"]["system_instruction"])
        assert name in output and other not in output
        assert output.endswith(DISCOVERY_ADDENDUM)
    with pytest.raises(RuntimeError), activate({"agent_synth": "temporary"}):
        raise RuntimeError("failed run")
    assert resolve("agent_synth", "default") == "default"


def test_personalization_adds_priority_instructions_without_replacing_base_prompt():
    default = "Private base prompt. Return JSON and only use retrieved evidence."
    with activate({"agent_synth": "Mở đầu bằng ba nhận định chính."}):
        combined = resolve("agent_synth", default)
        assert combined.startswith(default)
        assert "CỰC KỲ QUAN TRỌNG" in combined
        assert "Mở đầu bằng ba nhận định chính." in combined
        assert "Không bịa số liệu" in combined
        assert resolve("agent_planner", default) == default
    with activate({"agent_synth": "   "}):
        assert resolve("agent_synth", default) == default
