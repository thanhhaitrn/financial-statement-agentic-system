"""Storage lives under FINX_HOME, is stored relative to it, and the old
dataset_store/ layout is copied over only through a verified migration."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agentfinx.core.config import paths
from agentfinx.datasets import activation, registry, storage_migration
from agentfinx.datasets.kb.sqlite_repo import init_db, write_kb_manifest
from agentfinx.api import db as webdb


def _home(monkeypatch, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FINX_HOME", str(root))
    return root


def _ready_kb(db_path: Path, source: Path) -> None:
    conn = init_db(str(db_path))
    conn.execute("INSERT INTO financial_facts (company, item_name, value, source) VALUES (?, ?, ?, ?)",
                 ("Acme", "Doanh thu", "100", str(source)))
    conn.commit()
    write_kb_manifest(conn, {"source_path": str(source), "source_sha256": "s", "parser_version": "p",
                             "schema_version": 1, "facts_count": 1, "facts_sha256": "f"})
    conn.close()


def _legacy_store(home: Path) -> dict:
    """dataset_store/ as the previous code left it: absolute paths everywhere."""

    legacy = home / "dataset_store"
    source = home / "data" / "acme.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Báo cáo Acme\n", encoding="utf-8")
    _ready_kb(legacy / "sqlite" / "acme.db", source)
    (legacy / "raw_tables").mkdir(parents=True)
    (legacy / "raw_tables" / "acme.json").write_text("[]", encoding="utf-8")
    record = registry.DatasetRecord(
        dataset_id="acme", company="Acme", fiscal_year=2025, file_path=str(source), status="ready",
        facts_count=1, vector_docs_count=1, vector_collection_name="financial_statement__acme",
        sqlite_db_path=str(legacy / "sqlite" / "acme.db"), manifest_path=str(legacy / "manifests" / "acme.json"),
        raw_tables_path=str(legacy / "raw_tables" / "acme.json"),
    ).model_dump(mode="json")
    (legacy / "manifests").mkdir(parents=True)
    (legacy / "manifests" / "acme.json").write_text(json.dumps(record), encoding="utf-8")
    (legacy / "registry.json").write_text(json.dumps({
        "schema_version": registry.REGISTRY_SCHEMA_VERSION, "updated_at": "2026-01-01T00:00:00+00:00",
        "records": [record], "quarantine": [], "migration": None}), encoding="utf-8")
    web = webdb.WebDB(legacy / "web" / "finx_web.sqlite3")
    web.migrate()
    with web.transaction() as conn:
        conn.execute("INSERT INTO users (id, name, email, password_hash, created_at) VALUES "
                     "('u1', 'A', 'a@example.com', 'x', '2026-01-01T00:00:00+00:00')")
    (legacy / "web" / "cafef_symbols.json").write_text('{"symbols": []}', encoding="utf-8")
    (legacy / "locks").mkdir()
    (legacy / "locks" / "registry.lock").touch()
    (legacy / "qdrant_local_v3").mkdir()
    (legacy / "qdrant_local_v3" / "meta.json").write_text("{}", encoding="utf-8")
    return {"legacy": legacy, "source": source}


def test_storage_follows_finx_home_not_the_current_directory(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    assert paths.storage_dir() == home / "storage"
    assert paths.default_web_db_path() == home / "storage" / "web" / "finx_web.sqlite3"
    assert paths.eval_runs_dir() == home / "artifacts" / "eval"


def test_an_installed_copy_without_finx_home_refuses(monkeypatch, tmp_path):
    monkeypatch.delenv("FINX_HOME")
    monkeypatch.setattr(paths, "_CHECKOUT_ROOT", tmp_path)  # no storage/.gitignore: not a checkout
    with pytest.raises(paths.StorageError, match="FINX_HOME"):
        paths.home()


def test_new_code_refuses_to_run_next_to_an_unmigrated_store(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    (home / "dataset_store").mkdir()
    with pytest.raises(paths.StorageError, match="storage migrate"):
        registry.load_registry()
    assert not (home / "storage" / "datasets").exists()  # nothing empty was created beside it


def test_the_registry_stores_paths_relative_so_storage_can_move(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path / "before")
    record = registry.save_dataset(registry.build_dataset_record(
        file_path="storage/reports/acme.md", company="Acme", fiscal_year=2025))
    stored = json.loads(registry.registry_path().read_text(encoding="utf-8"))["records"][0]
    assert stored["file_path"] == "storage/reports/acme.md"
    assert stored["sqlite_db_path"] == f"storage/datasets/sqlite/{record.dataset_id}.db"
    manifest = json.loads(Path(record.manifest_path).read_text(encoding="utf-8"))
    assert not Path(manifest["sqlite_db_path"]).is_absolute()

    moved = shutil.copytree(home, tmp_path / "after")
    monkeypatch.setenv("FINX_HOME", str(moved))
    [reloaded] = registry.load_registry()
    assert reloaded.sqlite_db_path == str(moved / "storage" / "datasets" / "sqlite" / f"{record.dataset_id}.db")
    assert reloaded.file_path == str(moved / "storage" / "reports" / "acme.md")


def test_dry_run_changes_nothing(monkeypatch, tmp_path, capsys):
    home = _home(monkeypatch, tmp_path)
    _legacy_store(home)
    assert storage_migration.main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "data/acme.md -> storage/reports/acme.md" in out
    assert "qdrant_local_v3" in out and "locks" in out
    assert not (home / "storage").exists()


def test_migration_copies_verifies_and_the_app_opens_the_same_data(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    old = _legacy_store(home)
    (home / "storage").mkdir()
    (home / "storage" / ".gitignore").write_text("*\n", encoding="utf-8")

    assert storage_migration.main(["migrate", "--apply"]) == 0

    record = json.loads((home / "storage" / "migration.json").read_text(encoding="utf-8"))
    assert record["datasets"] == 1 and record["ready_checked"] == 1 and record["web"]["accounts"] == 1
    stored = json.loads(registry.registry_path().read_text(encoding="utf-8"))["records"][0]
    assert stored["file_path"] == "storage/reports/acme.md"
    assert stored["sqlite_db_path"] == "storage/datasets/sqlite/acme.db"
    assert stored["manifest_path"] == "storage/datasets/manifests/acme.json"
    [dataset] = registry.load_registry()
    activation._check_ready_kb(dataset, install_vocabulary=False)  # the chat would open it
    with sqlite3.connect(paths.default_web_db_path()) as conn:
        assert conn.execute("SELECT email FROM users").fetchall() == [("a@example.com",)]
    assert (home / "storage" / "catalog" / "cafef_symbols.json").is_file()
    assert (home / "storage" / "legacy" / "qdrant_local_v3" / "meta.json").is_file()
    assert not (home / "storage" / "locks").exists()
    # Copies only: the old store and the report file are untouched.
    assert (old["legacy"] / "registry.json").is_file() and old["source"].is_file()
    assert storage_migration.main(["migrate", "--apply"]) == 0  # second run: already migrated


def test_migration_refuses_while_something_holds_a_lock(monkeypatch, tmp_path):
    import fcntl
    import os

    home = _home(monkeypatch, tmp_path)
    old = _legacy_store(home)
    fd = os.open(old["legacy"] / "locks" / "registry.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert storage_migration.main(["migrate", "--apply"]) == 2
    finally:
        os.close(fd)
    assert not (home / "storage" / "datasets").exists()


def test_a_dataset_that_would_not_open_stops_the_migration(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    old = _legacy_store(home)
    with sqlite3.connect(old["legacy"] / "sqlite" / "acme.db") as conn:
        conn.execute("DELETE FROM financial_facts")  # manifest says 1 fact: kb_invalid
    assert storage_migration.main(["migrate", "--apply"]) == 2
    assert not (home / "storage" / "migration.json").exists()
    with pytest.raises(paths.StorageError):  # so the app still refuses to start
        paths.storage_dir()
    assert storage_migration.main(["migrate", "--apply"]) == 2  # leftovers must be cleared by hand
