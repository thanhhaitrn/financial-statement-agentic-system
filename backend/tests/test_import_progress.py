"""Import logs survive polling/reloads and contain actual status transitions."""

import json
import sqlite3

import pytest

from agentfinx.acquisition.store import AcquisitionStore
from agentfinx.api.imports import import_job_out
from agentfinx.core.schemas.acquisition import ImportJob


def new_job(store, *, origin="upload"):
    return store.create_job(ImportJob(job_id="progress-job", owner_id="owner", session_id="session",
                                      origin=origin), idempotency_key="progress-key")


def test_import_progress_is_durable_and_billing_polls_do_not_duplicate_steps(tmp_path):
    store = AcquisitionStore(tmp_path / "imports.db")
    job = new_job(store)
    assert [event.status for event in job.progress] == ["queued"]
    job = store.claim_job(job.job_id)
    assert job.status == "parsing"  # uploaded files do not have a download phase
    job = store.update_job(job.model_copy(update={"billing_pending": True}))
    assert [event.status for event in job.progress] == ["queued", "parsing"]
    for status in ("validating", "building", "ready"):
        job = store.update_job(job.model_copy(update={"status": status}))
    loaded = AcquisitionStore(store.path).get_job(job.job_id, owner_id="owner")
    assert [event.status for event in loaded.progress] == ["queued", "parsing", "validating", "building", "ready"]
    public = import_job_out(loaded)
    assert all(set(event) == {"status", "at"} for event in public["progress"])
    assert all(event["at"] for event in public["progress"])
    assert public["billing_pending"] is True and public["status"] == "ready"
    assert store.create_job(job, idempotency_key="progress-key").progress == loaded.progress


def test_progress_uses_stored_history_even_if_the_callers_copy_is_old(tmp_path):
    store = AcquisitionStore(tmp_path / "imports.db")
    original = new_job(store, origin="cafef_discovery")
    store.claim_job(original.job_id)
    job = store.update_job(original.model_copy(update={"status": "building"}))
    assert [event.status for event in job.progress] == ["queued", "downloading", "building"]
    with pytest.raises(ValueError, match="owner mismatch"):
        store.update_job(job.model_copy(update={"owner_id": "another-owner"}))
    assert store.get_job(job.job_id).progress == job.progress


def test_legacy_job_keeps_only_known_stages_and_progress_is_bounded(tmp_path):
    store = AcquisitionStore(tmp_path / "imports.db")
    job = new_job(store)
    with sqlite3.connect(store.path) as conn:
        raw = json.loads(conn.execute("SELECT payload_json FROM import_jobs WHERE job_id=?", (job.job_id,)).fetchone()[0])
        raw.pop("progress")
        conn.execute("UPDATE import_jobs SET payload_json=? WHERE job_id=?", (json.dumps(raw), job.job_id))
    job = store.get_job(job.job_id)
    assert job.progress == []
    job = store.update_job(job.model_copy(update={"status": "building"}))
    assert [event.status for event in job.progress] == ["queued", "building"]
    for index in range(70):
        job = store.update_job(job.model_copy(update={"status": "parsing" if index % 2 == 0 else "queued"}))
    assert len(store.get_job(job.job_id).progress) == 64
