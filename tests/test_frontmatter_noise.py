"""Regression tests for digital-signature / scanner noise in front-matter parsing."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

from ingestion.frontmatter_parser import (
    _is_report_header_line,
    _is_signature_noise_line,
    build_frontmatter_rows,
)


COVER_PAGE = """![Digital Signature of CÔNG TY CP ĐẦU TƯ CHÂU Á - THÁI BÌNH DƯƠNG](page_1_image_1_v2.jpg)

**Digitally signed by CÔNG TY CP ĐẦU TƯ CHÂU Á - THÁI BÌNH DƯƠNG**
**DN**: C=VN, S=THÀNH PHỐ HÀ NỘI, CN=CÔNG TY CP ĐẦU TƯ CHÂU Á - THÁI BÌNH DƯƠNG, OID.0.9.2342.19200300.100.1.1=MST:0102005769
**Reason**:
**Location**:
**Date**: 2025.01.24 18:45:17+07'00
**Foxit PDF Reader Version**: 12.0.2

# CÔNG TY CỔ PHẦN ĐẦU TƯ CHÂU Á – THÁI BÌNH DƯƠNG

# BÁO CÁO TÀI CHÍNH RIÊNG QUÝ IV/2024

## BÁO CÁO CỦA BAN TỔNG GIÁM ĐỐC

### Khái quát về Công ty

Công ty Cổ phần Đầu tư Châu Á – Thái Bình Dương là Công ty cổ phần hoạt động theo Giấy chứng nhận đăng ký doanh nghiệp số 0102005769 ngày 31 tháng 7 năm 2006 do Sở Kế hoạch và Đầu tư thành phố Hà Nội cấp.
"""

# Subheading / item_name / value blob markers that must never reach a fact.
NOISE_MARKERS = ("oid.", "foxit", "digitally signed", "18:45:17", "mst:0102005769")


class SignatureNoiseLineTest(unittest.TestCase):
    def test_signature_lines_detected(self):
        for line in (
            "**Digitally signed by CÔNG TY CP ĐẦU TƯ**",
            "**DN**: C=VN, S=THÀNH PHỐ HÀ NỘI, OID.0.9.2342.19200300.100.1.1=MST:0102005769",
            "**Reason**:",
            "**Location**:",
            "**Date**: 2025.01.24 18:45:17+07'00",
            "**Foxit PDF Reader Version**: 12.0.2",
        ):
            self.assertTrue(_is_signature_noise_line(line), line)
            self.assertTrue(_is_report_header_line(line), line)

    def test_real_content_not_flagged(self):
        for line in (
            "Khái quát về Công ty",
            "Công ty hoạt động theo Giấy chứng nhận đăng ký doanh nghiệp số 0102005769 "
            "ngày 31 tháng 7 năm 2006.",
            "Lý do: công ty thay đổi người đại diện theo pháp luật.",
        ):
            self.assertFalse(_is_signature_noise_line(line), line)

    def test_bare_title_recognised_as_header(self):
        self.assertTrue(_is_report_header_line("# CÔNG TY CỔ PHẦN ĐẦU TƯ CHÂU Á – THÁI BÌNH DƯƠNG"))
        self.assertTrue(_is_report_header_line("# BÁO CÁO TÀI CHÍNH RIÊNG QUÝ IV/2024"))


class FrontmatterNoiseTest(unittest.TestCase):
    def setUp(self):
        self.rows = build_frontmatter_rows(
            COVER_PAGE,
            company="CÔNG TY CỔ PHẦN ĐẦU TƯ APEC",
            source="data/test.md",
            fiscal_year="2024",
        )

    def test_no_signature_noise_in_any_field(self):
        for row in self.rows:
            blob = f"{row[5]} || {row[6]} || {row[7]}".lower()
            for marker in NOISE_MARKERS:
                self.assertNotIn(marker, blob, f"{marker!r} leaked into fact: {row}")

    def test_no_bare_title_value(self):
        values = [str(row[7] or "").strip() for row in self.rows]
        self.assertNotIn("# CÔNG TY CỔ PHẦN ĐẦU TƯ CHÂU Á – THÁI BÌNH DƯƠNG", values)
        self.assertNotIn("# BÁO CÁO TÀI CHÍNH RIÊNG QUÝ IV/2024", values)

    def test_real_fact_preserved(self):
        hits = [row for row in self.rows if "31 tháng 7 năm 2006" in str(row[7])]
        self.assertTrue(hits, "Expected the GCN registration-date fact to survive cleaning")
        # the cleaned fact must not carry the corrupted DN/OID subheading or item_name
        self.assertNotIn("oid.", str(hits[0][5]).lower())
        self.assertNotIn("oid.", str(hits[0][6]).lower())


if __name__ == "__main__":
    unittest.main()
