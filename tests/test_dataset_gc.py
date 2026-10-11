"""``agentfinx datasets gc``: which superseded Qdrant builds may be deleted."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dataset_catalog import registry
from dataset_catalog.gc import collect_dataset, main

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _hours_ago(hours, now=NOW):
    return (now - timedelta(hours=hours)).isoformat()


class _FakeQdrant:
    def __init__(self, collections, aliases):
        self.collections = dict(collections)
        self.aliases = dict(aliases)
        self.deleted = []

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in self.collections])

    def get_collection(self, collection_name):
        return SimpleNamespace(config=SimpleNamespace(metadata=self.collections[collection_name]))

    def get_aliases(self):
        return SimpleNamespace(aliases=[
            SimpleNamespace(alias_name=alias, collection_name=target)
            for alias, target in self.aliases.items()
        ])

    def delete_collection(self, collection_name):
        self.deleted.append(collection_name)
        self.collections.pop(collection_name)


@pytest.fixture
def dataset(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    return registry.save_dataset(
        registry.build_dataset_record(file_path="data/document.md", company="Acme", fiscal_year=2025)
    )


def _qdrant(logical, *, with_alias=True, now=NOW):
    return _FakeQdrant(
        {
            f"{logical}__v_aaaa_1": {"created_at": _hours_ago(30, now)},   # active
            f"{logical}__v_ffff_6": {"created_at": _hours_ago(2, now)},    # staged, never activated
            f"{logical}__v_bbbb_2": {"created_at": _hours_ago(100, now)},  # previous generation
            f"{logical}__v_cccc_3": {"created_at": _hours_ago(150, now)},  # superseded
            f"{logical}__v_dddd_4": {},                               # legacy, age unknown
            logical: {},                                              # unversioned legacy
            "financial_statement__other__v_zzzz_9": {"created_at": _hours_ago(500, now)},
        },
        {f"{logical}__active": f"{logical}__v_aaaa_1"} if with_alias else {},
    )


def test_gc_keeps_active_previous_young_and_untimestamped_builds(dataset):
    logical = dataset.vector_collection_name
    client = _qdrant(logical)

    plan = collect_dataset(dataset.dataset_id, client=client, now=NOW)

    reasons = {item["name"]: item["reason"] for item in plan["collections"]}
    assert reasons == {
        f"{logical}__v_aaaa_1": "active",
        f"{logical}__v_ffff_6": "younger_than_48h",
        f"{logical}__v_bbbb_2": "previous",
        f"{logical}__v_cccc_3": "superseded",
        f"{logical}__v_dddd_4": "no_timestamp",
    }
    assert plan["applied"] is False and client.deleted == []

    applied = collect_dataset(dataset.dataset_id, apply=True, client=client, now=NOW)
    assert applied["deleted"] == client.deleted == [f"{logical}__v_cccc_3"]
    assert "financial_statement__other__v_zzzz_9" in client.collections and logical in client.collections


def test_gc_without_an_active_alias_deletes_nothing(dataset):
    client = _qdrant(dataset.vector_collection_name, with_alias=False)

    result = collect_dataset(dataset.dataset_id, apply=True, client=client, now=NOW)

    assert client.deleted == []
    assert {item["reason"] for item in result["collections"]} == {"no_active_alias"}


def test_gc_cli_is_a_dry_run_unless_apply_is_given(dataset, monkeypatch, capsys):
    import dotenv

    import vectorstore.qdrant_store as qdrant_store

    # The CLI uses the real clock, so the fixture ages are relative to it.
    client = _qdrant(dataset.vector_collection_name, now=datetime.now(timezone.utc))
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)

    assert main(["gc", "--dataset-id", dataset.dataset_id]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False and client.deleted == []
    assert main(["gc", "--dataset-id", dataset.dataset_id, "--apply"]) == 0
    assert len(json.loads(capsys.readouterr().out)["deleted"]) == 1
    assert main(["gc", "--dataset-id", "missing-dataset"]) == 1
