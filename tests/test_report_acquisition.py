from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from acquisition.cafef import (CafeFReportDiscoveryProvider,
                               classify_report_title)
from acquisition.download import (PdfArtifactValidator, PdfValidationError,
                                  SecurePdfDownloader)
from acquisition.llamaparse import LlamaParseDocumentConverter, LlamaParseError
from acquisition.quota import AcquisitionQuota, QuotaExceeded
from acquisition.service import ReportAcquisitionService
from acquisition.store import AcquisitionStore, CandidateAccessError
from config.runtime_policy import AcquisitionPolicy
from schemas.acquisition import (ConvertedDocument, ConvertedPage,
                                 ReportCandidate, ReportQuery)


def _pdf(path: Path, *, pages: int = 2) -> Path:
    path.write_bytes(b"%PDF-1.7\nfixture")
    return path


def _validator(*, pages=2, max_pages=200):
    return PdfArtifactValidator(
        max_file_bytes=1024 * 1024,
        max_pdf_pages=max_pages,
        page_counter=lambda _path: pages,
    )


def test_cafef_discovery_normalizes_independent_dimensions():
    payload = {
        "Success": True,
        "Data": [
            {
                "Name": "BCTC hợp nhất Quý 2 năm 2025 đã soát xét",
                "Link": "https://cafef1.mediacdn.vn/reports/vnm-q2-2025.pdf",
            },
            {"Name": "invalid", "Link": "https://example.com/file.pdf"},
        ],
    }
    provider = CafeFReportDiscoveryProvider(fetch_json=lambda _url: payload)

    reports = provider.discover(
        ReportQuery(
            owner_id="owner-a",
            session_id="session-a",
            ticker="VNM",
            exchange="HOSE",
            fiscal_year=2025,
            fiscal_quarter=2,
            scope="consolidated",
            audit_status="reviewed",
        )
    )

    assert len(reports) == 1
    assert reports[0]["fiscal_quarter"] == 2
    assert reports[0]["scope"] == "consolidated"
    assert reports[0]["audit_status"] == "reviewed"
    assert classify_report_title("BCTC riêng 2024 chưa kiểm toán")["audit_status"] == "unaudited"


def test_pdf_validator_checks_magic_size_and_pages(tmp_path: Path):
    valid = _validator(pages=3).validate(
        _pdf(tmp_path / "valid.pdf"),
        origin="upload",
        cache_namespace="owner",
    )
    assert valid.page_count == 3
    assert len(valid.source_sha256) == 64

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(PdfValidationError, match="%PDF-"):
        _validator().validate(bad, origin="upload")

    with pytest.raises(PdfValidationError) as error:
        _validator(pages=201).validate(valid.path, origin="upload")
    assert error.value.code == "too_many_pages"


class _DownloadResponse(BytesIO):
    def __init__(self, body, *, url, headers=None):
        super().__init__(body)
        self._url = url
        self.headers = headers or {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _candidate():
    return ReportCandidate(
        candidate_id="candidate-a",
        owner_id="owner-a",
        session_id="session-a",
        ticker="VNM",
        exchange="HOSE",
        title="BCTC 2025",
        source_url="https://cafef1.mediacdn.vn/report.pdf",
        expires_at="2026-09-07T00:00:00+00:00",
    )


def test_secure_downloader_rejects_external_redirect_and_bad_mime(tmp_path: Path):
    validator = _validator(pages=1)
    redirected = SecurePdfDownloader(
        validator=validator,
        max_file_bytes=1024,
        opener=lambda *_args, **_kwargs: _DownloadResponse(
            b"%PDF-1.7\nfixture",
            url="https://evil.example/report.pdf",
            headers={"Content-Type": "application/pdf"},
        ),
    )
    with pytest.raises(PdfValidationError) as redirect_error:
        redirected.download(_candidate(), target_dir=tmp_path / "redirect")
    assert redirect_error.value.code == "redirect_not_allowed"

    bad_mime = SecurePdfDownloader(
        validator=validator,
        max_file_bytes=1024,
        opener=lambda *_args, **_kwargs: _DownloadResponse(
            b"%PDF-1.7\nfixture",
            url="https://cafef1.mediacdn.vn/report.pdf",
            headers={"Content-Type": "text/html"},
        ),
    )
    with pytest.raises(PdfValidationError) as mime_error:
        bad_mime.download(_candidate(), target_dir=tmp_path / "mime")
    assert mime_error.value.code == "invalid_content_type"


def test_secure_downloader_uses_sha_for_duplicate_public_pdf(tmp_path: Path):
    def opener(*_args, **_kwargs):
        return _DownloadResponse(
            b"%PDF-1.7\nfixture",
            url="https://cafef1.mediacdn.vn/report.pdf",
            headers={"Content-Type": "application/pdf"},
        )

    downloader = SecurePdfDownloader(
        validator=_validator(pages=1),
        max_file_bytes=1024,
        opener=opener,
    )
    first = downloader.download(_candidate(), target_dir=tmp_path / "reports")
    second = downloader.download(_candidate(), target_dir=tmp_path / "reports")

    assert first.source_sha256 == second.source_sha256
    assert first.path == second.path
    assert Path(first.path).name == f"{first.source_sha256}.pdf"


class _FakeParsing:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _parse_result(pages, *, version="2026-08-01"):
    return SimpleNamespace(
        markdown=SimpleNamespace(
            pages=[
                SimpleNamespace(page_number=number, markdown=markdown)
                for number, markdown in pages
            ]
        ),
        metadata=SimpleNamespace(
            version=version,
            usage=SimpleNamespace(num_pages_billed=len(pages)),
        ),
    )


def test_llamaparse_uses_cost_effective_private_mode_and_local_cache(tmp_path: Path):
    parsing = _FakeParsing(
        [_parse_result([(1, "# Báo cáo\n\nNội dung tài chính đã được trích xuất đầy đủ.")])]
    )
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        sleeper=lambda _seconds: None,
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"),
        origin="upload",
        cache_namespace="owner-a",
    )

    first = converter.convert(artifact, private=True)
    second = converter.convert(artifact, private=True)

    assert first.provider == "llamaparse"
    assert first.tier == "cost_effective"
    assert first.billed_credits == 3
    assert second.cache_hit is True
    assert second.billed_pages == second.billed_credits == 0
    assert len(parsing.calls) == 1
    assert parsing.calls[0]["tier"] == "cost_effective"
    assert parsing.calls[0]["disable_cache"] is True
    assert parsing.calls[0]["processing_options"]["ocr_parameters"]["languages"] == [
        "vi",
        "en",
    ]


def test_good_local_text_layer_avoids_llamaparse_call(tmp_path: Path):
    parsing = _FakeParsing([])
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        local_extractor=lambda _artifact: [
            "Thuyết minh chính sách kế toán trình bày rõ ràng và theo đúng thứ tự."
        ],
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"), origin="upload", cache_namespace="owner-a"
    )

    result = converter.convert(artifact, private=True)

    assert result.provider == "local_text_layer"
    assert result.billed_credits == 0
    assert parsing.calls == []


def test_financial_table_like_local_text_is_sent_to_llamaparse(tmp_path: Path):
    parsing = _FakeParsing(
        [
            _parse_result(
                [
                    (
                        1,
                        "# Báo cáo\n\n| Chỉ tiêu | Giá trị |\n|---|---:|\n"
                        "| A | 1 |\n| B | 2 |\n| C | 3 |\n| Doanh thu | 4 |",
                    )
                ]
            )
        ]
    )
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        local_extractor=lambda _artifact: [
            "Báo cáo kết quả doanh thu 1 2 3 4 cần giữ đúng cấu trúc bảng."
        ],
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"), origin="upload", cache_namespace="owner-a"
    )

    result = converter.convert(artifact, private=True)

    assert result.provider == "llamaparse"
    assert len(parsing.calls) == 1


def test_llamaparse_retries_only_bad_pages_with_agentic(tmp_path: Path):
    parsing = _FakeParsing(
        [
            _parse_result(
                [
                    (1, "# Trang tốt\n\nNội dung trang thứ nhất đầy đủ."),
                    (2, "x"),
                ]
            ),
            _parse_result([(2, "# Trang sửa\n\nNội dung bảng đã được khôi phục đầy đủ.")]),
        ]
    )
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        sleeper=lambda _seconds: None,
    )
    artifact = _validator(pages=2).validate(
        _pdf(tmp_path / "report.pdf"), origin="cafef_discovery", public_source=True
    )

    result = converter.convert(artifact, private=False)

    assert len(parsing.calls) == 2
    assert parsing.calls[1]["tier"] == "agentic"
    assert parsing.calls[1]["page_ranges"] == {"target_pages": "2"}
    assert "--- Page 1" in result.markdown
    assert result.billed_pages == 3
    assert result.billed_credits == 16
    assert result.targeted_retry_pages == [2]


class _ProviderError(RuntimeError):
    def __init__(self, status_code, retry_after=None):
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def test_llamaparse_honors_429_retry_after(tmp_path: Path):
    parsing = _FakeParsing(
        [
            _ProviderError(429, retry_after=2),
            _parse_result([(1, "# Báo cáo\n\nNội dung tài chính đầy đủ sau retry.")]),
        ]
    )
    delays = []
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        max_retries=3,
        sleeper=delays.append,
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"), origin="upload", cache_namespace="owner-a"
    )

    result = converter.convert(artifact, private=True)

    assert result.billed_credits == 3
    assert len(parsing.calls) == 2
    assert len(delays) == 1
    assert 2 <= delays[0] <= 2.25


def test_llamaparse_circuit_breaker_stops_new_provider_calls(tmp_path: Path):
    parsing = _FakeParsing([_ProviderError(503), _ProviderError(503)])
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        max_retries=0,
        circuit_failure_threshold=2,
        sleeper=lambda _seconds: None,
        clock=lambda: 100.0,
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"), origin="upload", cache_namespace="owner-a"
    )

    with pytest.raises(LlamaParseError, match="LlamaParse failed"):
        converter.convert(artifact, private=True)
    with pytest.raises(LlamaParseError, match="LlamaParse failed"):
        converter.convert(artifact, private=True)
    with pytest.raises(LlamaParseError, match="circuit breaker is open"):
        converter.convert(artifact, private=True)

    assert len(parsing.calls) == 2


def test_failed_targeted_retry_reports_credits_already_consumed(tmp_path: Path):
    parsing = _FakeParsing(
        [
            _parse_result([(1, "x")]),
            _ProviderError(503),
        ]
    )
    converter = LlamaParseDocumentConverter(
        allow_cloud_parse=True,
        client=SimpleNamespace(parsing=parsing),
        cache_dir=tmp_path / "cache",
        max_retries=0,
        sleeper=lambda _seconds: None,
        local_extractor=None,
    )
    artifact = _validator(pages=1).validate(
        _pdf(tmp_path / "report.pdf"), origin="upload", cache_namespace="owner-a"
    )

    with pytest.raises(LlamaParseError) as error:
        converter.convert(artifact, private=True)

    assert error.value.retryable is True
    assert error.value.billed_pages == 1
    assert error.value.billed_credits == 3


def _service(tmp_path: Path, *, now=None, discovery_payload=None, policy=None):
    policy = policy or AcquisitionPolicy(
        max_file_bytes=1024 * 1024,
        max_pdf_pages=200,
        monthly_parse_credit_budget=100_000,
    )
    store = AcquisitionStore(tmp_path / "acquisition.db")
    discovery = CafeFReportDiscoveryProvider(
        fetch_json=lambda _url: discovery_payload
        or {
            "Success": True,
            "Data": [
                {
                    "Name": "BCTC hợp nhất 2025 đã kiểm toán",
                    "Link": "https://cafef1.mediacdn.vn/report.pdf",
                }
            ],
        }
    )

    class Converter:
        converter_identity = "fake"

        def convert(self, artifact, *, private):
            return ConvertedDocument(
                markdown="# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest.",
                pages=[
                    ConvertedPage(
                        page_number=1,
                        markdown="# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest.",
                    )
                ],
                provider="llamaparse",
                provider_version="test",
                tier="cost_effective",
                source_sha256=artifact.source_sha256,
                billed_pages=artifact.page_count,
                billed_credits=artifact.page_count * 3,
            )

    validator = _validator(pages=1)
    built = []

    def dataset_builder(job, artifact, converted_path):
        built.append((job.owner_id, artifact.source_sha256, converted_path))
        return "dataset-ready"

    return (
        ReportAcquisitionService(
            store=store,
            discovery_provider=discovery,
            converter=Converter(),
            validator=validator,
            downloader=SimpleNamespace(),
            policy=policy,
            artifact_dir=tmp_path / "artifacts",
            converted_dir=tmp_path / "converted",
            dataset_builder=dataset_builder,
            now=now,
        ),
        built,
    )


def test_explicit_cloud_consent_is_required_even_with_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "not-a-real-key")
    parsing = _FakeParsing([])
    converter = LlamaParseDocumentConverter(client=SimpleNamespace(parsing=parsing),
                                           cache_dir=tmp_path / "cache", local_extractor=None)
    artifact = _validator(pages=1).validate(_pdf(tmp_path / "input.pdf"), origin="upload", cache_namespace="owner")
    with pytest.raises(LlamaParseError, match="allow-cloud-parse"):
        converter.convert(artifact, private=True)
    assert parsing.calls == []


def test_zero_provider_usage_is_not_replaced_by_fallback():
    result = {"metadata": {"usage": {"num_pages_billed": 0}}, "job": {"usage": {"credits": 0}}}
    assert LlamaParseDocumentConverter._billed_pages(result, fallback=200) == 0
    assert LlamaParseDocumentConverter._billed_credits(result, fallback=600) == 0


def test_quality_gate_detects_negative_loss_and_individual_bad_table():
    from acquisition.quality import markdown_quality_issues, numeric_retention_issues
    assert numeric_retention_issues("Giá vốn (250.000)", "Giá vốn 250.000") == ["negative_numeric_token_loss"]
    assert numeric_retention_issues("Giá vốn (250.000)", "Giá vốn -250000") == []
    good = "| Chỉ tiêu | Giá trị |\n|---|---|\n| A | 100 |"
    malformed = "| Chỉ tiêu | Giá trị |\n| B | 200 |\n| C | 300 |"
    assert "table_without_header_separator" in markdown_quality_issues(good + "\n\n" + malformed)
    assert markdown_quality_issues(good) == []


def test_good_local_pages_are_not_sent_to_provider(tmp_path):
    parsing = _FakeParsing([_parse_result([(2, "# Trang hai\n\nNội dung trang scan đã trích xuất đầy đủ.")])])
    converter = LlamaParseDocumentConverter(client=SimpleNamespace(parsing=parsing), allow_cloud_parse=True,
        cache_dir=tmp_path / "cache", local_extractor=lambda _: ["# Trang một\n\nNội dung giới thiệu doanh nghiệp đầy đủ.", ""])
    artifact = _validator(pages=2).validate(_pdf(tmp_path / "input.pdf"), origin="upload", cache_namespace="owner")
    result = converter.convert(artifact, private=True)
    assert parsing.calls[0]["page_ranges"] == {"target_pages": "2"}
    assert [page.page_number for page in result.pages] == [1, 2]
    assert result.billed_credits == 3
    assert "--- Page 0" in result.markdown and "--- Page 1" in result.markdown


def test_redirect_handler_blocks_before_opening_next_host():
    from urllib.request import Request
    from tools.http_safety import AllowlistedRedirectHandler
    from acquisition.download import allowed_report_url
    handler = AllowlistedRedirectHandler(allowed_report_url)
    request = Request("https://cafef1.mediacdn.vn/report.pdf")
    with pytest.raises(ValueError, match="Redirect target"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://127.0.0.1/private")
    assert not allowed_report_url("https://cafef1.mediacdn.vn:8443/report.pdf")
    assert not allowed_report_url("https://user:pass@cafef1.mediacdn.vn/report.pdf")
    assert not allowed_report_url("https://cafef.vn.evil.example/report.pdf")


@pytest.mark.parametrize("same_owner", [True, False])
def test_concurrent_admission_cannot_overbook_owner_or_credits(tmp_path, same_owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    policy = AcquisitionPolicy(monthly_parse_credit_budget=20 if not same_owner else 100)
    services = [_service(tmp_path, policy=policy)[0] for _ in range(2)]
    for service in services:
        stage = service._stage_upload
        def staged(artifact, *, owner_id, original=stage):
            barrier.wait(timeout=3)
            return original(artifact, owner_id=owner_id)
        service._stage_upload = staged
    source = _pdf(tmp_path / "input.pdf")
    def create(index):
        try:
            return services[index].create_upload_import(
                owner_id="same" if same_owner else f"owner-{index}", session_id="cli",
                upload_path=source, idempotency_key=f"key-{index}").status
        except QuotaExceeded as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [0, 1]))
    assert results.count("queued") == 1
    assert ("owner_concurrency" if same_owner else "global_provider_lock") in results
    assert services[0].store.committed_credits() == 13


def test_duplicate_dispatch_does_not_parse_twice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    service, _ = _service(tmp_path)
    source = _pdf(tmp_path / "input.pdf")
    job = service.create_upload_import(owner_id="a", session_id="cli", upload_path=source, idempotency_key="one")
    entered, release = Event(), Event()
    calls = []
    original = service.converter.convert
    def convert(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)
    service.converter.convert = convert
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.run_job, job.job_id)
        assert entered.wait(timeout=3)
        try:
            assert service.run_job(job.job_id).status == "parsing"
        finally:
            release.set()
        assert first.result().status == "ready"
    assert len(calls) == 1


def test_parse_slots_are_shared_between_store_instances(tmp_path):
    first = AcquisitionStore(tmp_path / "jobs.db")
    second = AcquisitionStore(tmp_path / "jobs.db")
    with first.parse_slot("job-one", limit=1):
        with pytest.raises(LlamaParseError, match="concurrency"):
            with second.parse_slot("job-two", limit=1):
                pytest.fail("slot admitted over global limit")
    with second.parse_slot("job-two", limit=1):
        pass


def test_unconfirmed_remote_billing_retains_budget_owner_and_slot(tmp_path):
    service, _ = _service(tmp_path)
    source = _pdf(tmp_path / "input.pdf")
    job = service.create_upload_import(owner_id="a", session_id="cli", upload_path=source, idempotency_key="one")
    def pending(*_args, **_kwargs):
        raise LlamaParseError("reconcile remote job", retryable=True, billing_pending=True)
    service.converter.convert = pending
    result = service.run_job(job.job_id)
    assert result.status == "failed" and result.billing_pending
    assert service.store.committed_credits() == 13
    with pytest.raises(QuotaExceeded, match="already active"):
        service.quota.check_import_slot("a")
    with pytest.raises(LlamaParseError, match="concurrency"):
        with AcquisitionStore(service.store.path).parse_slot("two", limit=1):
            pytest.fail("unconfirmed remote job released its global slot")


def test_simultaneous_public_cache_misses_only_parse_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    parsing = _FakeParsing([_parse_result([(1, "# Báo cáo\n\nNội dung báo cáo đầy đủ và được kiểm chứng.")])])
    original = parsing.parse
    def parse(**kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(**kwargs)
    parsing.parse = parse
    converters = [LlamaParseDocumentConverter(client=SimpleNamespace(parsing=parsing), allow_cloud_parse=True,
                  cache_dir=tmp_path / "cache", local_extractor=None) for _ in range(2)]
    artifact = _validator(pages=1).validate(_pdf(tmp_path / "input.pdf"), origin="cafef_discovery", public_source=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(converters[0].convert, artifact, private=False)
        assert entered.wait(timeout=3)
        second = executor.submit(converters[1].convert, artifact, private=False)
        release.set()
        results = [first.result(), second.result()]
    assert len(parsing.calls) == 1
    assert sorted(result.billed_credits for result in results) == [0, 3]


def test_poll_retry_reuses_submitted_job_and_provider_usage(tmp_path):
    class Parsing:
        def __init__(self):
            self.created = self.waited = 0
        def create(self, **kwargs):
            assert "expand" not in kwargs
            self.created += 1
            return SimpleNamespace(id="remote-id")
        def wait_for_completion(self, job_id, **kwargs):
            assert job_id == "remote-id"
            self.waited += 1
            if self.waited == 1:
                raise _ProviderError(429)
        def get(self, job_id, **kwargs):
            assert "usage" in kwargs["expand"]
            result = _parse_result([(1, "# Báo cáo\n\nNội dung báo cáo được trích xuất đầy đủ.")])
            result.job = SimpleNamespace(usage=SimpleNamespace(credits=0))
            return result
    parsing = Parsing()
    converter = LlamaParseDocumentConverter(client=SimpleNamespace(parsing=parsing), allow_cloud_parse=True,
        cache_dir=tmp_path / "cache", local_extractor=None, sleeper=lambda _: None)
    artifact = _validator(pages=1).validate(_pdf(tmp_path / "input.pdf"), origin="upload", cache_namespace="a")
    result = converter.convert(artifact, private=True)
    assert parsing.created == 1 and parsing.waited == 2
    assert result.billed_credits == 0


@pytest.mark.parametrize("source_kind", ["upload", "candidate"])
def test_reports_cli_discover_confirm_or_upload_to_ready(tmp_path, capsys, source_kind):
    import json
    from acquisition.cli import main
    service, built = _service(tmp_path)
    source = _pdf(tmp_path / "input.pdf")
    consent = []
    def factory(**kwargs):
        consent.append(kwargs["allow_cloud_parse"])
        return service
    common = ["--owner-id", "owner-a", "--session-id", "cli"]
    if source_kind == "candidate":
        assert main(["discover", "--ticker", "VNM", *common], service_factory=factory) == 0
        candidate_id = json.loads(capsys.readouterr().out)["candidates"][0]["candidate_id"]
        service.downloader = SimpleNamespace(download=lambda *_args, **_kwargs: service.validator.validate(
            source, origin="cafef_discovery", public_source=True))
        flags = ["--confirmed-candidate-id", candidate_id]
    else:
        flags = ["--pdf", str(source)]
    assert main(["import", *flags, *common, "--allow-cloud-parse"], service_factory=factory) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready" and result["dataset_id"] == "dataset-ready"
    assert consent[-1] is True and len(built) == 1


def test_upload_import_is_idempotent_owner_scoped_and_ready(tmp_path: Path):
    service, built = _service(tmp_path)
    uploaded = _pdf(tmp_path / "upload.pdf")

    first = service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=uploaded,
        idempotency_key="same-request",
        metadata={"company": "Vinamilk", "ticker": "VNM"},
    )
    second = service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=uploaded,
        idempotency_key="same-request",
    )
    ready = service.run_job(first.job_id)

    assert second.job_id == first.job_id
    assert ready.status == "ready"
    assert ready.dataset_id == "dataset-ready"
    assert ready.billed_credits == 3
    assert ready.billed_pages == 1
    assert ready.metadata["conversion"]["provider"] == "llamaparse"
    assert Path(ready.converted_path).exists()
    assert len(built) == 1


def test_dataset_is_attached_to_chat_only_after_ready(tmp_path: Path):
    service, _built = _service(tmp_path)
    attached = []

    # The callback can also query by a captured job ID; set it after enqueue.
    job_ref = {}

    def attach_after_ready(owner_id, session_id, dataset_id):
        persisted = service.store.get_job(job_ref["job_id"])
        attached.append((owner_id, session_id, dataset_id, persisted.status))

    service.dataset_attacher = attach_after_ready
    queued = service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=_pdf(tmp_path / "upload.pdf"),
        idempotency_key="attach",
    )
    job_ref["job_id"] = queued.job_id

    ready = service.run_job(queued.job_id)

    assert ready.session_attached is True
    assert attached == [("owner-a", "session-a", "dataset-ready", "ready")]


def test_quota_blocks_before_import_can_reach_llamaparse(tmp_path: Path):
    service, _built = _service(
        tmp_path,
        policy=AcquisitionPolicy(
            max_file_bytes=1024 * 1024,
            max_pdf_pages=200,
            monthly_parse_credit_budget=10,
        ),
    )

    with pytest.raises(QuotaExceeded) as error:
        service.create_upload_import(
            owner_id="owner-a",
            session_id="session-a",
            upload_path=_pdf(tmp_path / "upload.pdf"),
            idempotency_key="blocked",
        )

    assert error.value.code == "global_provider_lock"
    assert service.store.count_jobs("owner-a", statuses={"queued"}) == 0


def test_owner_can_have_only_one_active_import(tmp_path: Path):
    service, _built = _service(tmp_path)
    uploaded = _pdf(tmp_path / "upload.pdf")
    service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=uploaded,
        idempotency_key="first",
    )

    with pytest.raises(QuotaExceeded) as error:
        service.create_upload_import(
            owner_id="owner-a",
            session_id="session-a",
            upload_path=uploaded,
            idempotency_key="second",
        )

    assert error.value.code == "owner_concurrency"


def test_trial_allows_only_one_successful_report_import(tmp_path: Path):
    service, _built = _service(tmp_path)
    uploaded = _pdf(tmp_path / "upload.pdf")
    first = service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=uploaded,
        idempotency_key="first",
    )
    assert service.run_job(first.job_id).status == "ready"

    with pytest.raises(QuotaExceeded) as error:
        service.create_upload_import(
            owner_id="owner-a",
            session_id="session-a",
            upload_path=uploaded,
            idempotency_key="second",
        )

    assert error.value.code == "trial_import_limit"


def test_failed_parse_job_reconciles_consumed_credits(tmp_path: Path):
    service, _built = _service(tmp_path)

    class FailedConverter:
        def convert(self, _artifact, *, private):
            raise LlamaParseError(
                "targeted parse failed",
                retryable=True,
                billed_pages=1,
                billed_credits=3,
            )

    service.converter = FailedConverter()
    job = service.create_upload_import(
        owner_id="owner-a",
        session_id="session-a",
        upload_path=_pdf(tmp_path / "upload.pdf"),
        idempotency_key="failed-parse",
    )

    failed = service.run_job(job.job_id)

    assert failed.status == "failed"
    assert failed.retryable is True
    assert failed.billed_pages == 1
    assert failed.billed_credits == 3
    assert service.store.committed_credits() == 3


def test_discovered_report_requires_unexpired_owner_confirmation(tmp_path: Path):
    current = datetime(2026, 9, 5, tzinfo=timezone.utc)
    service, _built = _service(tmp_path, now=lambda: current)
    candidates = service.discover(
        ReportQuery(
            owner_id="owner-a",
            session_id="session-a",
            ticker="VNM",
            fiscal_year=2025,
        )
    )
    candidate = candidates[0]

    with pytest.raises(CandidateAccessError, match="owner_mismatch"):
        service.create_candidate_import(
            owner_id="owner-b",
            session_id="session-a",
            candidate_id=candidate.candidate_id,
            idempotency_key="wrong-owner",
        )

    service._now = lambda: current + timedelta(hours=1)
    with pytest.raises(CandidateAccessError, match="expired"):
        service.create_candidate_import(
            owner_id="owner-a",
            session_id="session-a",
            candidate_id=candidate.candidate_id,
            idempotency_key="expired",
        )


def test_confirmed_candidate_downloads_converts_and_builds_dataset(tmp_path: Path):
    service, built = _service(tmp_path)
    candidate = service.discover(
        ReportQuery(
            owner_id="owner-a",
            session_id="session-a",
            ticker="VNM",
            fiscal_year=2025,
        )
    )[0]

    class Downloader:
        def download(self, confirmed, *, target_dir, cache_namespace):
            assert confirmed.candidate_id == candidate.candidate_id
            Path(target_dir).mkdir(parents=True, exist_ok=True)
            path = _pdf(Path(target_dir) / "download.pdf")
            return _validator(pages=1).validate(
                path,
                origin="cafef_discovery",
                source_url=confirmed.source_url,
                public_source=True,
                cache_namespace=cache_namespace,
            )

    service.downloader = Downloader()
    queued = service.create_candidate_import(
        owner_id="owner-a",
        session_id="session-a",
        candidate_id=candidate.candidate_id,
        idempotency_key="confirmed",
    )
    ready = service.run_job(queued.job_id)

    assert ready.status == "ready"
    assert ready.dataset_id == "dataset-ready"
    assert ready.reserved_credits == 13
    assert built[0][0] == "owner-a"


def test_trial_clock_expires_after_fourteen_days(tmp_path: Path):
    current = [datetime(2026, 9, 1, tzinfo=timezone.utc)]
    store = AcquisitionStore(tmp_path / "acquisition.db")
    quota = AcquisitionQuota(
        store,
        AcquisitionPolicy(monthly_parse_credit_budget=100_000),
        now=lambda: current[0],
    )

    quota.admit_question("owner-a")
    current[0] += timedelta(days=14)

    with pytest.raises(QuotaExceeded) as error:
        quota.admit_question("owner-a")
    assert error.value.code == "trial_expired"


def test_budget_state_warns_and_locks_at_configured_thresholds(tmp_path: Path):
    quota = AcquisitionQuota(
        AcquisitionStore(tmp_path / "acquisition.db"),
        AcquisitionPolicy(monthly_parse_credit_budget=100),
    )

    warning = quota.budget_state(additional_credits=80)
    locked = quota.budget_state(additional_credits=95)

    assert warning["warning"] is True
    assert warning["trial_locked"] is False
    assert locked["trial_locked"] is True


def test_news_refresh_trial_quota_counts_only_three_admissions(tmp_path: Path):
    quota = AcquisitionQuota(
        AcquisitionStore(tmp_path / "acquisition.db"),
        AcquisitionPolicy(monthly_parse_credit_budget=100_000),
    )
    for _ in range(3):
        quota.admit_news_refresh("owner-a")

    with pytest.raises(QuotaExceeded) as error:
        quota.admit_news_refresh("owner-a")

    assert error.value.code == "trial_news_refresh_daily"
