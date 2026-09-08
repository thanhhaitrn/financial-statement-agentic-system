"""Regression tests: company-name inference must not swallow a whole sentence."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

from ingestion.note_parser import (
    _trim_company_name,
    infer_audit_status,
    infer_company,
    infer_fiscal_quarter,
    infer_report_scope,
    infer_ticker,
)


class TrimCompanyNameTest(unittest.TestCase):
    def test_cuts_clause_after_name(self):
        self.assertEqual(
            _trim_company_name(
                "Công ty Cổ phần Đầu tư Châu Á – Thái Bình Dương là Công ty cổ phần "
                "hoạt động theo Giấy chứng nhận đăng ký doanh nghiệp số 0102005769"
            ),
            "Công ty Cổ phần Đầu tư Châu Á – Thái Bình Dương",
        )

    def test_cuts_at_punctuation_and_keywords(self):
        self.assertEqual(_trim_company_name("Công ty TNHH ABC, trụ sở tại Hà Nội"), "Công ty TNHH ABC")
        self.assertEqual(_trim_company_name("Công ty Cổ phần X được thành lập năm 2010"), "Công ty Cổ phần X")

    def test_clean_name_unchanged(self):
        name = "CÔNG TY CỔ PHẦN ĐẦU TƯ CHÂU Á – THÁI BÌNH DƯƠNG"
        self.assertEqual(_trim_company_name(name), name)

    def test_legal_name_keeps_decimal_suffix(self):
        self.assertEqual(
            _trim_company_name("Công ty Cổ phần Sông Đà 7.02"),
            "Công ty Cổ phần Sông Đà 7.02",
        )


class InferCompanyTest(unittest.TestCase):
    def test_prose_line_yields_just_the_name(self):
        md = (
            "# CÔNG TY CỔ PHẦN ĐẦU TƯ CHÂU Á – THÁI BÌNH DƯƠNG\n\n"
            "### Khái quát về Công ty\n\n"
            "Công ty Cổ phần Đầu tư Châu Á – Thái Bình Dương là Công ty cổ phần hoạt động "
            "theo Giấy chứng nhận đăng ký doanh nghiệp số 0102005769 ngày 31 tháng 7 năm 2006.\n"
        )
        company = infer_company(md)
        self.assertEqual(company, "Công ty Cổ phần Đầu tư Châu Á – Thái Bình Dương")
        self.assertNotIn("Giấy chứng nhận", company)
        self.assertLess(len(company), 60)

    def test_heading_keeps_decimal_suffix(self):
        self.assertEqual(
            infer_company(
                "# BÁO CÁO TÀI CHÍNH\n"
                "## CÔNG TY CỔ PHẦN SÔNG ĐÀ 7.02\n"
            ),
            "Công ty cổ phần sông đà 7.02",
        )


class SourceMetadataInferenceTest(unittest.TestCase):
    def test_quarterly_separate_report_without_assurance_is_unaudited(self):
        md = (
            "# CÔNG TY CỔ PHẦN A\n"
            "# BÁO CÁO TÀI CHÍNH RIÊNG QUÝ IV/2024\n"
        )
        self.assertEqual(infer_fiscal_quarter(md), 4)
        self.assertEqual(infer_report_scope(md), "separate")
        self.assertEqual(infer_audit_status(md), "unaudited")

    def test_review_takes_precedence_over_audit_explanation(self):
        md = (
            "# CÔNG TY CỔ PHẦN A\n"
            "# BÁO CÁO TÀI CHÍNH RIÊNG GIỮA NIÊN ĐỘ ĐÃ ĐƯỢC SOÁT XÉT\n"
            "Công việc soát xét không phải là một cuộc kiểm toán.\n"
        )
        self.assertEqual(infer_audit_status(md), "reviewed")

    def test_combined_scope_and_explicit_ticker(self):
        md = (
            "# CÔNG TY CỔ PHẦN A\n"
            "# BÁO CÁO TÀI CHÍNH TỔNG HỢP ĐÃ ĐƯỢC KIỂM TOÁN\n"
            "Mã chứng khoán là AAA\n"
        )
        self.assertEqual(infer_report_scope(md), "combined")
        self.assertEqual(infer_audit_status(md), "audited")
        self.assertEqual(infer_ticker(md), "AAA")


if __name__ == "__main__":
    unittest.main()
