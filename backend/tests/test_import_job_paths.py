"""An import job's files are stored relative to FINX_HOME, so the whole
directory can move: jobs created at A run to completion from B after A is
gone, whatever the current directory. No LlamaParse call is made; the parse
API is the scripted fake of test_import_billing."""

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentfinx.acquisition.download import PdfArtifactValidator
from agentfinx.acquisition.llamaparse import LlamaParseDocumentConverter
from agentfinx.acquisition.service import ReportAcquisitionService
from agentfinx.acquisition.store import SCHEMA_VERSION, AcquisitionStore
from agentfinx.core.config import paths
from agentfinx.core.config.runtime_policy import AcquisitionPolicy
from agentfinx.core.schemas.acquisition import ImportJob
from agentfinx.datasets import registry, storage_migration
from test_import_billing import _Crash, _Lifecycle


def _service(parsing, *, builder=None):
    acquisition = paths.acquisition_dir()
    validator = PdfArtifactValidator(max_file_bytes=1 << 20, max_pdf_pages=200, page_counter=lambda _path: 1)
    return ReportAcquisitionService(
        store=AcquisitionStore(acquisition / "acquisition.db"),
        discovery_provider=SimpleNamespace(),
        converter=LlamaParseDocumentConverter(
            client=SimpleNamespace(parsing=parsing), cache_dir=acquisition / "llamaparse-cache",
            local_extractor=None, sleeper=lambda _seconds: None, max_retries=1,
        ),
        validator=validator,
        downloader=SimpleNamespace(),
        policy=AcquisitionPolicy(max_file_bytes=1 << 20, max_pdf_pages=200, monthly_parse_credit_budget=100_000,
                                 trial_successful_imports=10, per_owner_concurrency=5),
        artifact_dir=acquisition / "artifacts",
        converted_dir=registry.sources_dir() / "imports",
        dataset_builder=builder or (lambda job, _artifact, _path: f"ds-{job.job_id[:8]}"),
    )


def _upload(service, tmp_path, name, *, key):
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.7\n" + name.encode())
    return service.create_upload_import(owner_id="a", session_id="s1", upload_path=pdf, idempotency_key=key,
                                        allow_cloud_parse=True)


def _stored(store, job_id):
    with sqlite3.connect(store.path) as conn:
        return json.loads(conn.execute("SELECT payload_json FROM import_jobs WHERE job_id = ?",
                                       (job_id,)).fetchone()[0])


def test_jobs_finish_after_finx_home_moves_and_the_old_place_is_gone(monkeypatch, tmp_path):
    first, second, elsewhere = tmp_path / "A", tmp_path / "B", tmp_path / "C"
    for directory in (first, elsewhere):
        directory.mkdir()
    monkeypatch.setenv("FINX_HOME", str(first))
    parsing = _Lifecycle(wait_errors=[_Crash()])
    builds = []

    def crash_first_build(job, _artifact, _path):
        builds.append(job.job_id)
        if len(builds) == 1:
            raise _Crash()
        return f"ds-{job.job_id[:8]}"

    service = _service(parsing, builder=crash_first_build)
    queued = _upload(service, tmp_path, "queued.pdf", key="key-queued")
    polling = _upload(service, tmp_path, "polling.pdf", key="key-polling")
    building = _upload(service, tmp_path, "building.pdf", key="key-building")
    with pytest.raises(_Crash):
        service.run_job(polling.job_id)  # submitted, then the worker died while polling
    with pytest.raises(_Crash):
        service.run_job(building.job_id)  # parsed and converted, then died while building
    assert service.store.get_job(polling.job_id).status == "parsing"
    stuck = service.store.get_job(building.job_id)
    assert stuck.status == "building" and Path(stuck.converted_path).is_file()

    # Stored relative to FINX_HOME: the PDF and the Markdown.
    assert _stored(service.store, queued.job_id)["upload_path"].startswith("storage/acquisition/artifacts/")
    assert _stored(service.store, building.job_id)["converted_path"].startswith("storage/datasets/sources/imports/")
    created_before = len(parsing.created)
    assert created_before == 2

    shutil.move(first, second)  # A no longer exists
    monkeypatch.setenv("FINX_HOME", str(second))
    monkeypatch.chdir(elsewhere)
    service = _service(parsing, builder=crash_first_build)

    for job in (queued, polling, building):
        moved = service.store.get_job(job.job_id)
        assert Path(moved.upload_path).is_file() and Path(moved.upload_path).is_relative_to(second)
    markdown = Path(service.store.get_job(building.job_id).converted_path)
    assert markdown.is_file() and markdown.is_relative_to(second)

    recovered = {job.job_id: job.status for job in service.recover_interrupted_jobs()}
    assert recovered == {polling.job_id: "queued", building.job_id: "queued"}
    finished = {job.job_id: service.run_job(job.job_id) for job in (queued, polling, building)}
    assert all(job.status == "ready" for job in finished.values())
    assert Path(finished[building.job_id].converted_path).is_relative_to(second)
    # Only the never-started job submitted a parse; the interrupted one polled its known remote job.
    assert len(parsing.created) == created_before + 1
    assert finished[polling.job_id].billing_status == "confirmed"

    # Sending the same request again with its idempotency key returns the job, at B.
    replay = _upload(service, elsewhere, "queued.pdf", key="key-queued")
    assert replay.job_id == queued.job_id and Path(replay.upload_path).is_relative_to(second)


def _insert_v2(store_path: Path, job: ImportJob, key: str):
    with sqlite3.connect(store_path) as conn:
        conn.execute("INSERT INTO import_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (job.job_id, job.owner_id, job.session_id, job.status, key, job.created_at,
                      job.updated_at, json.dumps(job.model_dump(mode="json"), sort_keys=True)))


def _rows(store_path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(store_path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def test_upgrading_a_v2_database_changes_only_the_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("FINX_HOME", str(home))
    database = home / "storage" / "acquisition" / "acquisition.db"
    store = AcquisitionStore(database)
    inside = home / "storage" / "acquisition" / "artifacts" / "ns" / "x.pdf"
    outside = tmp_path / "elsewhere" / "y.pdf"
    common = dict(owner_id="a", session_id="s", origin="upload", created_at="2026-01-01T00:00:00+00:00",
                  updated_at="2026-01-02T00:00:00+00:00", allow_cloud_parse=True,
                  consented_at="2026-01-01T00:00:01+00:00", billing_status="pending", billing_pending=True,
                  billing_blocking=True, reserved_credits=13, billed_credits=0)
    jobs = {
        "inside": ImportJob(job_id="inside", status="parsing", upload_path=str(inside),
                            converted_path=str(home / "storage" / "datasets" / "sources" / "imports" / "x.md"),
                            **common),
        "outside": ImportJob(job_id="outside", status="queued", upload_path=str(outside), **common),
        "empty": ImportJob(job_id="empty", status="failed", **common),
    }
    for key, job in jobs.items():
        _insert_v2(database, job, f"key-{key}")
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO parse_attempts VALUES ('att-1', 'inside', 'a', 'sha', 'cost_effective', 'all', "
                     "'payer', NULL, 'submitted', 'remote-9', '', NULL, '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO usage_events (owner_id, kind, amount, created_at) VALUES ('a', 'question', 1, 'x')")
        conn.execute("INSERT INTO parse_slots VALUES ('inside', '2026-01-01')")
        conn.execute("PRAGMA user_version = 2")
    columns = {row[0]: row[:7] for row in _rows(database, "import_jobs")}  # every column but the payload
    ledger = {table: _rows(database, table) for table in ("parse_attempts", "usage_events", "parse_slots")}

    store = AcquisitionStore(database)

    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    assert len(list(database.parent.glob("acquisition.db.backup-v2-*"))) == 1
    assert {row[0]: row[:7] for row in _rows(database, "import_jobs")} == columns  # status, times, owner, key
    assert {table: _rows(database, table) for table in ledger} == ledger  # the billing ledger
    for job_id, original in jobs.items():
        stored, before = _stored(store, job_id), original.model_dump(mode="json")
        assert {k: v for k, v in stored.items() if k not in ("upload_path", "converted_path")} == \
               {k: v for k, v in before.items() if k not in ("upload_path", "converted_path")}
    assert _stored(store, "inside")["upload_path"] == "storage/acquisition/artifacts/ns/x.pdf"
    assert _stored(store, "inside")["converted_path"] == "storage/datasets/sources/imports/x.md"
    assert _stored(store, "outside")["upload_path"] == str(outside)  # outside FINX_HOME: kept as is
    assert _stored(store, "empty")["upload_path"] == "" and _stored(store, "empty")["converted_path"] == ""
    assert store.get_job("inside").upload_path == str(inside.resolve())

    AcquisitionStore(database)  # a second open changes nothing more
    assert len(list(database.parent.glob("acquisition.db.backup-v2-*"))) == 1


def test_the_storage_migration_stores_job_paths_relative_from_either_form(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("FINX_HOME", str(home))
    legacy = home / "dataset_store"
    artifacts = legacy / "acquisition" / "artifacts" / "ns"
    artifacts.mkdir(parents=True)
    (legacy / "registry.json").write_text(json.dumps({
        "schema_version": registry.REGISTRY_SCHEMA_VERSION, "updated_at": "", "records": [], "quarantine": [],
        "migration": None}), encoding="utf-8")
    for name in ("absolute.pdf", "relative.pdf"):
        (artifacts / name).write_bytes(b"%PDF-1.7\n" + name.encode())
    database = legacy / "acquisition" / "acquisition.db"
    AcquisitionStore(database)
    base = dict(owner_id="a", session_id="s", origin="upload", status="queued",
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
    _insert_v2(database, ImportJob(job_id="absolute", upload_path=str((artifacts / "absolute.pdf").resolve()),
                                   **base), "key-absolute")
    _insert_v2(database, ImportJob(job_id="relative",
                                   upload_path="dataset_store/acquisition/artifacts/ns/relative.pdf", **base),
               "key-relative")

    assert storage_migration.main(["migrate", "--apply"]) == 0
    legacy.rename(tmp_path / "hidden")

    store = AcquisitionStore(paths.acquisition_dir() / "acquisition.db")
    for job_id in ("absolute", "relative"):
        assert _stored(store, job_id)["upload_path"] == f"storage/acquisition/artifacts/ns/{job_id}.pdf"
        assert Path(store.get_job(job_id).upload_path).is_file()
    record = json.loads((home / "storage" / "migration.json").read_text(encoding="utf-8"))
    assert record["unfinished_import_jobs"] == 2 and not record["warnings"]
