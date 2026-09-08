"""Exercise the real installed SDK over a local, non-network HTTP transport."""

import json

import pytest

llama_cloud = pytest.importorskip("llama_cloud")
httpx = pytest.importorskip("httpx")

from acquisition.download import PdfArtifactValidator
from acquisition.llamaparse import LlamaParseDocumentConverter


def test_real_sdk_upload_poll_and_usage_contract(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        job = {"id": "job-sdk", "project_id": "project-sdk", "status": "COMPLETED",
               "usage": {"credits": 3}}
        if request.method == "POST":
            body = request.read()
            assert b"%PDF-1.7" in body
            assert b"cost_effective" in body
            assert b"disable_cache" in body
            return httpx.Response(200, json=job)
        return httpx.Response(200, json={"job": job,
            "markdown": {"pages": [{"page_number": 1, "success": True,
                "markdown": "# Báo cáo\n\nNội dung báo cáo tài chính đầy đủ."}]},
            "metadata": {"pages": []}})

    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic SDK transport fixture")
    artifact = PdfArtifactValidator(max_file_bytes=1024, max_pdf_pages=1,
                                   page_counter=lambda _: 1).validate(source, origin="upload", cache_namespace="owner")
    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        client = llama_cloud.LlamaCloud(api_key="non-secret-test-key", http_client=http_client)
        converter = LlamaParseDocumentConverter(client=client, cache_dir=tmp_path / "cache",
                                                allow_cloud_parse=True, local_extractor=None)
        first = converter.convert(artifact, private=True)
        second = converter.convert(artifact, private=True)
    assert sum(request.method == "POST" for request in requests) == 1
    assert first.billed_credits == 3 and second.billed_credits == 0
    assert second.cache_hit
    assert "--- Page 0" in first.markdown
    references = list((tmp_path / "cache").rglob("*.job.json"))
    assert json.loads(references[0].read_text()) == {"job_id": "job-sdk"}
