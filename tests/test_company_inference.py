"""Regression tests: company-name inference must not swallow a whole sentence."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

from ingestion.note_parser import _trim_company_name, infer_company


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


if __name__ == "__main__":
    unittest.main()
