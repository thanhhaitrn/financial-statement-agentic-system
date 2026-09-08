"""Opt-in live Parse API smoke test using ONLY a generated, non-private report.

Requires the web extra plus reportlab. Writes all smoke artifacts to --work-dir;
does not modify the dataset registry, run analysis models, or build Qdrant.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def create_fixture(path: Path, font_path: Path):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    pdfmetrics.registerFont(TTFont("Fixture", str(font_path)))
    style = ParagraphStyle("Fixture", fontName="Fixture", fontSize=11, leading=16)
    table = Table([
        ["Chỉ tiêu", "Năm 2025", "Năm 2024"],
        ["Doanh thu thuần", "1.000.000", "900.000"],
        ["Giá vốn hàng bán", "(250.000)", "(200.000)"],
        ["Lợi nhuận sau thuế", "750.000", "700.000"],
    ], colWidths=[245, 105, 105])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Fixture"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7eff7")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
    ]))
    SimpleDocTemplate(str(path), invariant=1).build([
        Paragraph("CÔNG TY KIỂM THỬ TỔNG HỢP", style), Spacer(1, 15),
        Paragraph("BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", style),
        Paragraph("Năm tài chính 2025 - Đơn vị: VND", style), Spacer(1, 18), table,
        Spacer(1, 20), Paragraph("Dữ liệu giả lập phục vụ kiểm thử. Không phải báo cáo của doanh nghiệp thực.", style),
        Spacer(1, 20), Paragraph("Trang 1", style),
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--create-only", action="store_true")
    parser.add_argument("--allow-cloud-parse", action="store_true")
    args = parser.parse_args(argv)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    pdf = args.work_dir / "synthetic-financial-report.pdf"
    if not pdf.exists():
        create_fixture(pdf, args.font)
    if args.create_only:
        print(json.dumps({"fixture": str(pdf)}))
        return 0
    if not args.allow_cloud_parse:
        parser.error("Live parsing requires --allow-cloud-parse")

    from dotenv import load_dotenv
    load_dotenv()
    from acquisition.download import PdfArtifactValidator, SecurePdfDownloader
    from acquisition.llamaparse import LlamaParseDocumentConverter
    from acquisition.service import ReportAcquisitionService
    from acquisition.store import AcquisitionStore
    from config.runtime_policy import AcquisitionPolicy
    from ingestion.pipeline import build_knowledge_base
    from kb.sqlite_repo import read_kb_manifest
    from schemas.datasets import DatasetRecord
    from ingestion.table_parser import attach_context

    converter = LlamaParseDocumentConverter(cache_dir=args.work_dir / "cache", allow_cloud_parse=True,
                                           local_extractor=None)
    validator = PdfArtifactValidator(max_file_bytes=1024 * 1024, max_pdf_pages=1)
    checks = {}

    def build(job, artifact, md_path):
        record = DatasetRecord(
            dataset_id="synthetic-parse-smoke", owner_id="synthetic-smoke", company="Công ty kiểm thử tổng hợp",
            fiscal_year=2025, file_path=str(md_path), source_pdf_path=artifact.path,
            source_sha256=artifact.source_sha256, source_converter_identity=converter.converter_identity,
            source_converter_version=job.metadata["conversion"]["provider_version"],
            sqlite_db_path=str(args.work_dir / "kb.db"), manifest_path=str(args.work_dir / "index.json"),
            vector_collection_name="synthetic-smoke", raw_tables_path=str(args.work_dir / "tables.json"),
        )
        connection, count = build_knowledge_base(record)
        try:
            manifest = read_kb_manifest(connection)
        finally:
            connection.close()
        tables = attach_context(md_path.read_text(encoding="utf-8"))
        assert tables and all(item["source_page"] == 1 for item in tables)
        assert "250.000" in md_path.read_text(encoding="utf-8")
        assert count > 0
        checks.update(facts_count=count, source_pages=[1], manifest=manifest)
        return record.dataset_id

    service = ReportAcquisitionService(
        store=AcquisitionStore(args.work_dir / "jobs.db"), discovery_provider=None,
        converter=converter, validator=validator,
        downloader=SecurePdfDownloader(validator=validator, max_file_bytes=1024 * 1024),
        policy=AcquisitionPolicy(max_pdf_pages=1, monthly_parse_credit_budget=100),
        artifact_dir=args.work_dir / "artifacts", converted_dir=args.work_dir / "converted",
        dataset_builder=build,
    )
    job = service.create_upload_import(owner_id="synthetic-smoke", session_id="cli", upload_path=pdf,
                                       idempotency_key="synthetic-v1")
    job = service.run_job(job.job_id)
    result = {"status": job.status, "billed_credits": job.billed_credits, "billing_pending": job.billing_pending,
              "error_code": job.error_code, "error_message": job.error_message, **checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if job.status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
