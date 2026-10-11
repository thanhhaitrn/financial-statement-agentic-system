"""Import billing: per-job consent, the parse-attempt ledger, billing status
independent of processing, crash recovery and reconciliation by remote job ID."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from acquisition.download import PdfArtifactValidator
from acquisition.llamaparse import (LLAMAPARSE_CONTRACT_VERSION,
                                    LlamaParseDocumentConverter)
from acquisition.local_text import LOCAL_TEXT_CONTRACT_VERSION
from acquisition.quota import QuotaExceeded
from acquisition.service import ReportAcquisitionService
from acquisition.store import AcquisitionStore, ParseAttemptRecorder
from config.runtime_policy import AcquisitionPolicy
from schemas.acquisition import ImportJob, PdfArtifact

GOOD = "# Báo cáo\n\nNội dung báo cáo tài chính đã được trích xuất đầy đủ."


class _ProviderError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code


class _Crash(BaseException):
    """Stands in for the worker process dying mid-step."""


class _Lifecycle:
    """Parse v2 ``create → wait_for_completion → get`` with scripted outcomes."""

    def __init__(self, *, credits=3, credit_plan=(), outputs=(), create_errors=(), wait_errors=()):
        self.credits = credits
        self.credit_plan = list(credit_plan)
        self.outputs = list(outputs)
        self.create_errors = list(create_errors)
        self.wait_errors = list(wait_errors)
        self.created = []
        self.waited = 0
        self.fetched = []
        self._credit_by_remote = {}
        self._output_by_remote = {}

    def create(self, **kwargs):
        self.created.append(kwargs)
        if self.create_errors:
            raise self.create_errors.pop(0)
        remote_id = f"remote-{len(self.created)}"
        if self.credit_plan:
            self._credit_by_remote[remote_id] = self.credit_plan.pop(0)
        self._output_by_remote[remote_id] = self.outputs.pop(0) if self.outputs else [(1, GOOD)]
        return SimpleNamespace(id=remote_id)

    def wait_for_completion(self, job_id, **kwargs):
        self.waited += 1
        if self.wait_errors:
            raise self.wait_errors.pop(0)

    def get(self, job_id, **kwargs):
        self.fetched.append(job_id)
        pages = self._output_by_remote.get(job_id, [(1, GOOD)])
        return SimpleNamespace(
            markdown=SimpleNamespace(
                pages=[SimpleNamespace(page_number=n, markdown=text) for n, text in pages]
            ),
            metadata=SimpleNamespace(
                version="2026-08-19", usage=SimpleNamespace(num_pages_billed=len(pages))
            ),
            job=SimpleNamespace(
                status="COMPLETED",
                usage=SimpleNamespace(credits=self._credit_by_remote.get(job_id, self.credits)),
            ),
        )


def _pdf(path: Path, body: bytes = b"%PDF-1.7\nfixture") -> Path:
    path.write_bytes(body)
    return path


def _service(tmp_path, parsing, *, attached=None, **policy_overrides):
    policy = AcquisitionPolicy(**{
        "max_file_bytes": 1024 * 1024,
        "max_pdf_pages": 200,
        "monthly_parse_credit_budget": 100_000,
        "trial_successful_imports": 10,
        **policy_overrides,
    })
    validator = PdfArtifactValidator(
        max_file_bytes=1024 * 1024, max_pdf_pages=200, page_counter=lambda _path: 1,
    )
    # The converter's own default is "no consent": consent must come from the job.
    converter = LlamaParseDocumentConverter(
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        local_extractor=None,
        sleeper=lambda _seconds: None,
        max_retries=1,
    )
    attacher = None
    if attached is not None:
        def attacher(owner_id, session_id, dataset_id):
            attached.append((owner_id, session_id, dataset_id))
    return ReportAcquisitionService(
        store=AcquisitionStore(tmp_path / "acquisition.db"),
        discovery_provider=SimpleNamespace(),
        converter=converter,
        validator=validator,
        downloader=SimpleNamespace(),
        policy=policy,
        artifact_dir=tmp_path / "artifacts",
        converted_dir=tmp_path / "converted",
        dataset_builder=lambda job, _artifact, _path: f"ds-{job.job_id[:8]}",
        dataset_attacher=attacher,
    )


def _import(service, tmp_path, *, key="k1", consent=True, name="input.pdf", body=b"%PDF-1.7\nfixture"):
    return service.create_upload_import(
        owner_id="a",
        session_id="s1",
        upload_path=_pdf(tmp_path / name, body),
        idempotency_key=key,
        allow_cloud_parse=consent,
    )


def test_consent_is_read_per_job_by_one_long_lived_converter(tmp_path):
    parsing = _Lifecycle()
    service = _service(tmp_path, parsing)
    assert service.converter.allow_cloud_parse is False

    yes = service.run_job(_import(service, tmp_path, key="yes").job_id)
    assert yes.status == "ready" and yes.allow_cloud_parse and yes.consented_at

    no = service.run_job(
        _import(service, tmp_path, key="no", consent=False, name="b.pdf",
                body=b"%PDF-1.7\nother").job_id
    )
    assert no.status == "failed" and "allow-cloud-parse" in no.error_message
    assert no.billing_status == "not_submitted" and not no.billing_pending
    assert len(parsing.created) == 1


def test_targeted_agentic_retry_uses_job_consent_and_its_own_attempt(tmp_path):
    parsing = _Lifecycle(outputs=[[(1, "x")], [(1, GOOD)]], credit_plan=[3, 10])
    service = _service(tmp_path, parsing)

    job = service.run_job(_import(service, tmp_path).job_id)

    assert job.status == "ready"
    assert [call["tier"] for call in parsing.created] == ["cost_effective", "agentic"]
    attempts = service.store.attempts_for_job(job.job_id)
    assert [row["tier"] for row in attempts] == ["cost_effective", "agentic"]
    assert [call["user_metadata"]["attempt_id"] for call in parsing.created] == [
        row["attempt_id"] for row in attempts
    ]
    assert [row["remote_job_id"] for row in attempts] == ["remote-1", "remote-2"]
    assert job.billing_status == "confirmed"
    assert job.confirmed_credits == "13" and job.billed_credits == 13
    assert job.estimated_credits == 13


def test_ledger_path_refuses_a_parse_only_client(tmp_path):
    class ParseOnly:
        def __init__(self):
            self.calls = []

        def parse(self, **kwargs):
            self.calls.append(kwargs)

    parsing = ParseOnly()
    service = _service(tmp_path, parsing)

    job = service.run_job(_import(service, tmp_path).job_id)

    assert job.status == "failed" and "lifecycle" in job.error_message
    assert parsing.calls == []
    assert job.billing_status == "not_submitted"
    assert not service.store.holds_parse_slot(job.job_id)


def test_create_5xx_is_never_retried_and_stays_pending(tmp_path):
    parsing = _Lifecycle(create_errors=[_ProviderError(503)])
    service = _service(tmp_path, parsing)

    job = service.run_job(_import(service, tmp_path).job_id)

    assert len(parsing.created) == 1
    assert job.status == "failed"
    assert job.billing_status == "pending" and job.billing_pending
    [attempt] = service.store.attempts_for_job(job.job_id)
    assert attempt["state"] == "remote_id_lost" and attempt["remote_job_id"] is None
    # The provider may be running it: keep the slot and the reservation.
    assert service.store.holds_parse_slot(job.job_id)
    assert service.store.committed_credits() == job.reserved_credits == 13
    with pytest.raises(QuotaExceeded) as blocked:
        service.quota.check_import_slot("a")
    assert blocked.value.code == "import_billing_pending"
    # Nothing to look up by ID: reconcile neither resubmits nor invents a charge.
    assert service.reconcile_job(job.job_id).billing_pending
    assert len(parsing.created) == 1 and parsing.fetched == []


def test_create_429_is_retried_under_the_same_attempt(tmp_path):
    parsing = _Lifecycle(create_errors=[_ProviderError(429)])
    service = _service(tmp_path, parsing)

    job = service.run_job(_import(service, tmp_path).job_id)

    assert job.status == "ready" and job.billing_status == "confirmed"
    [attempt] = service.store.attempts_for_job(job.job_id)
    assert len(parsing.created) == 2
    assert {call["user_metadata"]["attempt_id"] for call in parsing.created} == {
        attempt["attempt_id"]
    }


def test_crash_before_the_remote_id_is_stored_fails_pending_without_resubmit(tmp_path):
    parsing = _Lifecycle()
    service = _service(tmp_path, parsing)
    job = _import(service, tmp_path)
    claimed = service.store.claim_job(job.job_id)
    service.store.begin_attempt(claimed, tier="cost_effective", selection="all")

    [recovered] = service.recover_interrupted_jobs()

    assert recovered.status == "failed" and recovered.error_code == "remote_id_lost"
    assert recovered.billing_status == "pending" and recovered.billing_pending
    assert service.run_job(job.job_id).status == "failed"
    assert parsing.created == []


def test_crash_while_polling_resumes_the_known_remote_job(tmp_path):
    parsing = _Lifecycle(wait_errors=[_Crash()])
    service = _service(tmp_path, parsing)
    job = _import(service, tmp_path)

    with pytest.raises(_Crash):
        service.run_job(job.job_id)
    assert service.store.get_job(job.job_id).status == "parsing"
    assert service.store.holds_parse_slot(job.job_id)

    [requeued] = service.recover_interrupted_jobs()
    assert requeued.status == "queued"
    ready = service.run_job(job.job_id)

    assert ready.status == "ready"
    assert ready.billing_status == "confirmed" and ready.billed_credits == 3
    assert len(parsing.created) == 1 and parsing.waited == 2
    assert not service.store.holds_parse_slot(job.job_id)


def test_crash_while_building_reuses_cache_and_keeps_the_real_charge(tmp_path):
    parsing = _Lifecycle()
    service = _service(tmp_path, parsing)
    builds = []

    def crash_once(job, _artifact, _path):
        builds.append(job.job_id)
        if len(builds) == 1:
            raise _Crash()
        return "ds-rebuilt"

    service.dataset_builder = crash_once
    job = _import(service, tmp_path)
    with pytest.raises(_Crash):
        service.run_job(job.job_id)
    assert service.store.get_job(job.job_id).status == "building"

    service.recover_interrupted_jobs()
    ready = service.run_job(job.job_id)

    assert ready.status == "ready" and ready.dataset_id == "ds-rebuilt"
    assert ready.metadata["conversion"]["cache_hit"] is True
    # The cache hit is free for this use, but the job's own charge stands.
    assert ready.billing_status == "confirmed" and ready.billed_credits == 3
    assert len(parsing.created) == 1


def test_ready_with_unreported_usage_is_usable_but_blocks_next_import(tmp_path):
    parsing = _Lifecycle(credits=None)
    attached = []
    service = _service(tmp_path, parsing, attached=attached)

    job = service.run_job(_import(service, tmp_path).job_id)

    assert job.status == "ready" and job.session_attached
    assert attached == [("a", "s1", job.dataset_id)]
    assert job.billing_status == "pending" and job.billing_pending
    assert job.billed_credits == 0 and job.confirmed_credits == ""
    assert job.estimated_credits == 3  # estimate recorded, never as a charge
    assert service.store.committed_credits() == job.reserved_credits == 13
    assert not service.store.holds_parse_slot(job.job_id)  # remote job finished
    with pytest.raises(QuotaExceeded) as blocked:
        _import(service, tmp_path, key="k2", name="b.pdf", body=b"%PDF-1.7\nother")
    assert blocked.value.code == "import_billing_pending"

    parsing.credits = Decimal("2.5")
    settled = service.reconcile_job(job.job_id)
    assert settled.billing_status == "confirmed" and not settled.billing_pending
    assert settled.confirmed_credits == "2.5" and settled.billed_credits == 3
    assert service.store.committed_credits() == 3

    fetched = len(parsing.fetched)
    assert service.reconcile_job(job.job_id) == settled
    assert len(parsing.fetched) == fetched  # settled remote jobs are not re-fetched
    # A late or repeated provider report can never change a recorded charge.
    service.store.complete_remote("remote-1", remote_status="COMPLETED", credits=Decimal(99))
    assert service.store.billing_summary(job.job_id).confirmed_credits == Decimal("2.5")
    service.quota.check_import_slot("a")


def test_reports_cli_reconciles_one_or_all_pending(tmp_path, capsys):
    from acquisition.cli import main

    parsing = _Lifecycle(credits=None)
    service = _service(tmp_path, parsing)
    job = service.run_job(_import(service, tmp_path).job_id)
    factory = lambda **_kwargs: service  # noqa: E731

    assert main(["reconcile", job.job_id], service_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["billing_status"] == "pending"

    parsing.credits = 3
    assert main(["reconcile", "--all-pending"], service_factory=factory) == 0
    [view] = json.loads(capsys.readouterr().out)["jobs"]
    assert view["job_id"] == job.job_id
    assert view["billing_status"] == "confirmed" and view["confirmed_credits"] == "3"
    assert main(["reconcile", "--all-pending"], service_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == []


def test_a_job_processing_and_billing_pending_counts_once(tmp_path):
    service = _service(tmp_path, _Lifecycle(), per_owner_concurrency=2)
    job = _import(service, tmp_path)
    service.store.update_job(job.model_copy(update={"status": "parsing", "billing_pending": True}))

    service.quota.check_import_slot("a")  # one blocking job, limit two


def test_replaying_an_idempotency_key_with_other_consent_is_rejected(tmp_path):
    service = _service(tmp_path, _Lifecycle())
    first = _import(service, tmp_path, consent=True)
    assert _import(service, tmp_path, consent=True).job_id == first.job_id
    with pytest.raises(ValueError, match="consent"):
        _import(service, tmp_path, consent=False)


def test_legacy_jobs_migrate_to_explicit_billing_status(tmp_path):
    path = tmp_path / "acquisition.db"
    AcquisitionStore(path)

    def legacy(job_id, **fields):
        payload = ImportJob(
            job_id=job_id, owner_id="a", session_id="s", origin="upload",
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-02T00:00:00+00:00",
            **fields,
        ).model_dump(mode="json")
        for key in ("billing_status", "allow_cloud_parse", "consented_at",
                    "estimated_credits", "confirmed_credits"):
            payload.pop(key)
        return payload

    rows = {
        "parsed": legacy("parsed", status="ready", billed_credits=39, reserved_credits=39,
                         metadata={"conversion": {"provider": "llamaparse"}}),
        "pending": legacy("pending", status="failed", billing_pending=True, reserved_credits=13),
        "cached": legacy("cached", status="ready",
                         metadata={"conversion": {"provider": "llamaparse", "cache_hit": True}}),
        "queued": legacy("queued", status="queued", reserved_credits=13),
    }
    with sqlite3.connect(path) as connection:
        for job_id, payload in rows.items():
            connection.execute(
                "INSERT INTO import_jobs VALUES (?, 'a', 's', ?, ?, ?, ?, ?)",
                (job_id, payload["status"], f"key-{job_id}", payload["created_at"],
                 payload["updated_at"], json.dumps(payload)),
            )
        connection.execute("PRAGMA user_version = 0")

    store = AcquisitionStore(path)
    jobs = {job_id: store.get_job(job_id) for job_id in rows}

    assert jobs["parsed"].billing_status == "estimated_legacy"
    assert jobs["parsed"].billed_credits == 39 and jobs["parsed"].confirmed_credits == ""
    assert jobs["pending"].billing_status == "pending" and jobs["pending"].billing_pending
    assert jobs["cached"].billing_status == "confirmed" and jobs["cached"].confirmed_credits == "0"
    assert jobs["queued"].billing_status == "not_submitted"
    for job_id, job in jobs.items():
        # Processing status and the accounting time are preserved.
        assert job.status == rows[job_id]["status"]
        assert job.updated_at == "2026-01-02T00:00:00+00:00"


def test_cache_identity_is_unchanged_by_billing_changes():
    assert LLAMAPARSE_CONTRACT_VERSION == "llamaparse-markdown-v2"
    assert LOCAL_TEXT_CONTRACT_VERSION == "pdf-text-preflight-v1"
    converter = LlamaParseDocumentConverter(client=object(), cache_dir=Path("/c"), local_extractor=None)
    artifact = PdfArtifact(path="/x.pdf", source_sha256="a" * 64, size_bytes=1, page_count=1,
                           origin="upload", cache_namespace="owner")
    identity = "fe929d5ecedf9ba78c1962c0af24ef38c412da33946c253b7bb7f9d6afe9967b.json"
    assert converter._cache_path(artifact, private=True) == Path(
        "/c/4c1029697ee358715d3a14a2add817c4b01651440de808371f78165ac90dc581"
    ) / identity
    assert converter._cache_path(artifact, private=False) == Path("/c/public") / identity


def test_public_cache_reuser_links_to_the_payer_and_settles_with_it(tmp_path):
    parsing = _Lifecycle(credits=None)
    service = _service(tmp_path, parsing)
    store, converter = service.store, service.converter
    artifact = service.validator.validate(
        _pdf(tmp_path / "public.pdf"), origin="cafef_discovery", public_source=True
    )

    def job(job_id, owner):
        return store.create_job(
            ImportJob(job_id=job_id, owner_id=owner, session_id="cli", origin="cafef_discovery",
                      source_sha256=artifact.source_sha256, reserved_credits=13),
            idempotency_key=f"key-{job_id}",
        )

    payer, reuser = job("payer", "owner-a"), job("reuser", "owner-b")
    first = converter.convert(artifact, private=False, consent=True,
                              recorder=ParseAttemptRecorder(store, payer))
    second = converter.convert(artifact, private=False, consent=True,
                               recorder=ParseAttemptRecorder(store, reuser))

    assert not first.cache_hit and second.cache_hit and len(parsing.created) == 1
    [paid] = store.attempts_for_job("payer")
    [link] = store.attempts_for_job("reuser")
    assert link["role"] == "consumer" and link["payer_attempt_id"] == paid["attempt_id"]
    # The reuser adds no charge but cannot settle before the payer does.
    assert store.billing_summary("payer").status == "pending"
    assert store.billing_summary("reuser").status == "pending"

    parsing.credits = 3
    settled = service.reconcile_job("reuser")

    assert settled.billing_status == "confirmed" and settled.billed_credits == 0
    assert store.billing_summary("payer").confirmed_credits == Decimal(3)
    assert service.reconcile_job("payer").billed_credits == 3
    assert parsing.fetched.count("remote-1") == 2  # once while parsing, once to reconcile


def test_private_uploads_never_link_to_another_owners_parse(tmp_path):
    parsing = _Lifecycle()
    service = _service(tmp_path, parsing)
    first = service.run_job(_import(service, tmp_path, key="a").job_id)
    other = service.create_upload_import(
        owner_id="b", session_id="s1", upload_path=_pdf(tmp_path / "copy.pdf"),
        idempotency_key="b", allow_cloud_parse=True,
    )
    second = service.run_job(other.job_id)

    assert first.billed_credits == second.billed_credits == 3
    assert len(parsing.created) == 2
    assert all(row["role"] == "payer" for row in service.store.attempts_for_job(other.job_id))
