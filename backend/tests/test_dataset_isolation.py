"""Dataset isolation for multi-process web use: cache keys, locks, activation."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentfinx.agents import keyworder_runner
from agentfinx.core.config import allowed_keywords
from agentfinx.datasets import activation, locks, registry
from agentfinx.datasets.vectorstore import qdrant_store


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Route-candidate cache
# ---------------------------------------------------------------------------


def test_route_specs_follow_the_vocabulary_not_its_size():
    table = allowed_keywords.TABLE_NOTE
    alpha = "zeta khoản mục thử nghiệm alpha"
    beta = "zeta khoản mục thử nghiệm beta"

    def match_texts():
        return {spec["match_text"] for spec in keyworder_runner._candidate_route_specs()}

    try:
        allowed_keywords.set_dynamic_keywords({table: [alpha]})
        first = match_texts()
        # Same number of keywords, different content: a size-keyed cache
        # would hand dataset B the route specs of dataset A.
        allowed_keywords.set_dynamic_keywords({table: [beta]})
        second = match_texts()
        allowed_keywords.set_dynamic_keywords({table: [alpha]})
        third = match_texts()
    finally:
        allowed_keywords.set_dynamic_keywords({})

    assert alpha in first and beta not in first
    assert beta in second and alpha not in second
    assert third == first


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def test_dataset_lock_is_refused_while_holding_the_registry_lock(isolated_store):
    with locks.registry_lock():
        with pytest.raises(locks.LockOrderError):
            with locks.dataset_lock("ds-a"):
                pass

    # The permitted order: dataset first, then registry.
    with locks.dataset_lock("ds-a"):
        with locks.registry_lock():
            pass


def test_registry_lock_is_reentrant_within_a_thread(isolated_store):
    with locks.registry_lock():
        with locks.registry_lock():
            pass


def test_dataset_lock_rejects_path_traversal_ids(isolated_store):
    with pytest.raises(ValueError):
        with locks.dataset_lock("../escape"):
            pass


def test_dataset_lock_excludes_another_process(isolated_store):
    probe = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    print('acquired')\n"
        "except BlockingIOError:\n"
        "    print('blocked')\n"
    )
    lock_path = locks.locks_dir() / "dataset-ds-a.lock"
    with locks.dataset_lock("ds-a"):
        held = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    free = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert held == "blocked"
    assert free == "acquired"


def test_process_singleton_allows_one_holder(isolated_store):
    first = locks.ProcessSingleton("chat")
    second = locks.ProcessSingleton("chat")
    other_queue = locks.ProcessSingleton("import")

    assert first.try_acquire()
    assert not second.try_acquire()
    assert other_queue.try_acquire()

    first.release()
    assert second.try_acquire()
    second.release()
    other_queue.release()


# ---------------------------------------------------------------------------
# Qdrant backend for multi-process web use
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "url", "allowed"),
    [
        (":memory:", "", False),
        ("/tmp/qdrant-embedded", "", False),
        ("", "", False),
        ("http://127.0.0.1:6333", "", True),
        ("", "https://cluster.example.cloud", True),
    ],
)
def test_web_refuses_qdrant_modes_processes_cannot_share(monkeypatch, location, url, allowed):
    monkeypatch.setattr(qdrant_store, "QDRANT_LOCATION", location)
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", url)
    if allowed:
        qdrant_store.ensure_multiprocess_qdrant_backend()
    else:
        with pytest.raises(RuntimeError):
            qdrant_store.ensure_multiprocess_qdrant_backend()


def test_open_active_collection_never_creates_anything(monkeypatch):
    mutations = []

    class FakeClient:
        def get_aliases(self):
            return SimpleNamespace(aliases=[])

        def collection_exists(self, collection_name):
            return False

        def create_collection(self, **_kwargs):
            mutations.append("create_collection")

        def create_payload_index(self, **_kwargs):
            mutations.append("create_payload_index")

    monkeypatch.setattr(qdrant_store, "get_client", lambda: FakeClient())
    with pytest.raises(LookupError):
        qdrant_store.open_active_collection("financial_statement__missing")
    assert mutations == []


# ---------------------------------------------------------------------------
# Read-only activation
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self, count=5, generation="fp-123"):
        self._count = count
        self.generation = generation

    def count(self):
        return self._count


def _patch_kb(monkeypatch, *, facts=3, manifest_facts=3, closed=None):
    class Conn:
        def close(self):
            if closed is not None:
                closed.append(True)

    monkeypatch.setattr(activation, "open_db_readonly", lambda _path: Conn())
    monkeypatch.setattr(
        activation,
        "read_kb_manifest",
        lambda _conn: {"facts_count": manifest_facts, "facts_sha256": "abcdef0123456789ffff"},
    )
    monkeypatch.setattr(activation, "sqlite_count_facts", lambda _conn: facts)


def _dataset(tmp_path, *, status="ready", with_db=True):
    db = tmp_path / "kb.sqlite"
    if with_db:
        db.write_bytes(b"")
    return SimpleNamespace(
        dataset_id="ds-a",
        status=status,
        sqlite_db_path=str(db),
        vector_collection_name="financial_statement__ds_a",
    )


def test_activation_installs_vocabulary_without_building_or_writing(monkeypatch, tmp_path):
    import agentfinx.workflow.pipeline as legacy_cli

    def forbidden(*_args, **_kwargs):
        raise AssertionError("chat activation must never build or write")

    monkeypatch.setattr(legacy_cli, "ensure_built", forbidden)
    monkeypatch.setattr(registry, "save_dataset", forbidden)
    monkeypatch.setattr(qdrant_store, "create_collection", forbidden)

    closed = []
    _patch_kb(monkeypatch, closed=closed)
    monkeypatch.setattr(activation, "derive_keyword_augmentation", lambda _c: {"t": ["kw"]})
    monkeypatch.setattr(activation, "derive_query_slot_lexicon", lambda _c: {"metric": ["m"]})
    installed = {}
    monkeypatch.setattr(activation, "set_dynamic_keywords", lambda m: installed.update(kw=m))
    monkeypatch.setattr(
        activation,
        "set_dataset_slot_lexicon",
        lambda m, dataset_id: installed.update(slot=(m, dataset_id)),
    )

    result = activation.activate_dataset_for_query(
        _dataset(tmp_path), open_collection=lambda _name: _FakeCollection()
    )

    assert result.generation == "fp-123:abcdef0123456789"
    assert installed == {"kw": {"t": ["kw"]}, "slot": ({"metric": ["m"]}, "ds-a")}
    assert closed == [True]


def test_activation_replaces_vocabulary_even_when_derivation_fails(monkeypatch, tmp_path):
    _patch_kb(monkeypatch)

    def boom(_conn):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr(activation, "derive_keyword_augmentation", boom)
    monkeypatch.setattr(activation, "derive_query_slot_lexicon", boom)
    installed = {}
    monkeypatch.setattr(activation, "set_dynamic_keywords", lambda m: installed.update(kw=m))
    monkeypatch.setattr(
        activation,
        "set_dataset_slot_lexicon",
        lambda m, dataset_id: installed.update(slot=(m, dataset_id)),
    )

    activation.activate_dataset_for_query(
        _dataset(tmp_path), open_collection=lambda _name: _FakeCollection()
    )

    # An explicit empty replacement, never the previous dataset's vocabulary.
    assert installed == {"kw": {}, "slot": ({}, "ds-a")}


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (dict(status="kb_ready"), "dataset_not_ready"),
        (dict(with_db=False), "artifact_missing"),
        (dict(manifest_facts=4), "kb_invalid"),
        (dict(opener_error=True), "index_missing"),
        (dict(count=0), "index_missing"),
    ],
)
def test_activation_reports_needs_build_instead_of_answering(monkeypatch, tmp_path, setup, code):
    closed = []
    _patch_kb(monkeypatch, manifest_facts=setup.get("manifest_facts", 3), closed=closed)
    monkeypatch.setattr(activation, "apply_dataset_vocabulary", lambda _c, _d: None)

    def opener(_name):
        if setup.get("opener_error"):
            raise LookupError("not built")
        return _FakeCollection(count=setup.get("count", 5))

    dataset = _dataset(
        tmp_path,
        status=setup.get("status", "ready"),
        with_db=setup.get("with_db", True),
    )
    with pytest.raises(activation.DatasetNotReady) as excinfo:
        activation.activate_dataset_for_query(dataset, open_collection=opener)

    assert excinfo.value.code == code
    if setup.get("manifest_facts"):
        assert closed == [True]  # the read-only handle never leaks on failure


# ---------------------------------------------------------------------------
# Registry: owner access, lost updates, delete lock order
# ---------------------------------------------------------------------------

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _configure_registry_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))


def _save(company, owner=""):
    return registry.save_dataset(
        registry.build_dataset_record(
            file_path="data/document.md",
            company=company,
            fiscal_year=2024,
            owner_id=owner,
        )
    )


def test_web_access_requires_an_owner_and_never_widens_on_empty(monkeypatch, tmp_path):
    from agentfinx.datasets import access

    _configure_registry_paths(monkeypatch, tmp_path)
    mine = _save("Cong ty Rieng", owner="alice")

    for call in (
        lambda: access.get_accessible_dataset(mine.dataset_id, "", frozenset()),
        lambda: access.get_owned_dataset(mine.dataset_id, "   "),
        lambda: access.list_accessible_datasets(None, frozenset()),
    ):
        with pytest.raises(ValueError):
            call()

    # The CLI accessor stays fail-open on purpose; the web layer must not use it.
    assert registry.get_dataset(mine.dataset_id) is not None


def test_owned_and_shared_lookups_are_separate(monkeypatch, tmp_path):
    from agentfinx.datasets import access

    _configure_registry_paths(monkeypatch, tmp_path)
    alice = _save("Cong ty Alice", owner="alice")
    bob = _save("Cong ty Bob", owner="bob")
    sample = _save("Cong ty Mau")  # ownerless
    hidden_sample = _save("Cong ty An")  # ownerless, not allowlisted
    shared = frozenset({sample.dataset_id, bob.dataset_id})

    assert access.get_accessible_dataset(alice.dataset_id, "alice", shared).access == "owned"
    with pytest.raises(access.DatasetAccessError):
        access.get_accessible_dataset(bob.dataset_id, "alice", shared)  # owned by bob
    assert access.get_accessible_dataset(sample.dataset_id, "alice", shared).access == "shared"
    with pytest.raises(access.DatasetAccessError):
        access.get_accessible_dataset(hidden_sample.dataset_id, "alice", shared)
    with pytest.raises(access.DatasetAccessError):
        access.get_accessible_dataset("../etc", "alice", shared)

    listed = {(item.record.dataset_id, item.access) for item in access.list_accessible_datasets("alice", shared)}
    assert listed == {(alice.dataset_id, "owned"), (sample.dataset_id, "shared")}


def test_shared_allowlist_is_empty_by_default_and_rejects_bad_ids():
    from agentfinx.datasets import access

    assert access.shared_dataset_ids_from_env({}) == frozenset()
    assert access.parse_shared_dataset_ids(" a-1 , b.2;") == frozenset({"a-1", "b.2"})
    with pytest.raises(ValueError):
        access.parse_shared_dataset_ids("ok,../bad")


def test_two_processes_saving_datasets_lose_no_registry_records(tmp_path):
    import os

    child = (
        "import sys\n"
        "from agentfinx.datasets import registry\n"
        "prefix = sys.argv[1]\n"
        "for i in range(15):\n"
        "    registry.save_dataset(registry.build_dataset_record(\n"
        "        file_path='data/document.md', company=f'{prefix} {i}', fiscal_year=2024))\n"
    )
    env = dict(os.environ, FINX_HOME=str(tmp_path),
               PYTHONPATH=str(REPO_ROOT / "src"))
    procs = [
        subprocess.Popen([sys.executable, "-c", child, prefix], cwd=REPO_ROOT, env=env)
        for prefix in ("alpha", "beta")
    ]
    assert [proc.wait(timeout=120) for proc in procs] == [0, 0]

    import json

    saved = json.loads((tmp_path / "storage" / "datasets" / "registry.json").read_text(encoding="utf-8"))
    assert len(saved["records"]) == 30


def test_cli_delete_takes_the_dataset_lock_before_the_registry_lock(monkeypatch, tmp_path):
    import agentfinx.workflow.pipeline as legacy_cli

    _configure_registry_paths(monkeypatch, tmp_path)
    target = _save("Cong ty Xoa")
    observed = {}

    def fake_delete(dataset_id):
        observed["held"] = dict(locks._held_kinds())
        return target

    monkeypatch.setattr(legacy_cli, "resolve_dataset_for_delete", lambda _args: target)
    monkeypatch.setattr(legacy_cli, "delete_dataset_record", fake_delete)

    assert legacy_cli.delete_dataset_cli(SimpleNamespace(yes=True)) == 0
    assert observed["held"] == {"dataset": 1}
