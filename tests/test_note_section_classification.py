"""Regression tests: note-section sub-tables are scoped under TABLE_NOTE."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

from ingestion.kb_builder import build_fact_rows
from ingestion.table_parser import attach_context
from schemas.table_names import TABLE_BS, TABLE_NOTE, normalize_table_heading

MD = """# BẢNG CÂN ĐỐI KẾ TOÁN

| Chỉ tiêu | 31/12/2024 |
| --- | --- |
| Tiền và tương đương tiền | 12.000.000.000 |

## THUYẾT MINH BÁO CÁO TÀI CHÍNH

### 18a. Vay ngắn hạn

| Khoản vay | Số cuối kỳ |
| --- | --- |
| Vay ngân hàng ngắn hạn | 131.357.622.354 |

### Tài sản cố định hữu hình

| Chỉ tiêu | Nguyên giá |
| --- | --- |
| Nhà cửa vật kiến trúc | 202.406.369.251 |
"""


class AttachContextSectionTest(unittest.TestCase):
    def test_section_latches_to_notes(self):
        blocks = attach_context(MD)
        sections = [normalize_table_heading(b.get("section", "")) for b in blocks]
        # first table is the balance sheet, the rest are inside notes
        self.assertEqual(normalize_table_heading(blocks[0]["section"]), TABLE_BS)
        self.assertTrue(all(s == TABLE_NOTE for s in sections[1:]))


class NoteScheduleRetagTest(unittest.TestCase):
    def setUp(self):
        self.rows = build_fact_rows(attach_context(MD), company="APEC", source="x.md", fiscal_year="2024")

    def _facts(self):
        # row tuple: company, fy, heading, item_code, note_ref, subheading, item_name, ...
        return [{"heading": normalize_table_heading(r[2]), "subheading": r[5],
                 "item_name": r[6], "raw_value": r[8]} for r in self.rows]

    def test_balance_sheet_row_keeps_its_table(self):
        bs = [f for f in self._facts() if "12.000.000.000" in str(f["raw_value"])]
        self.assertTrue(bs)
        self.assertEqual(bs[0]["heading"], TABLE_BS)
        self.assertEqual(bs[0]["subheading"], "")

    def test_note_schedules_scoped_to_notes_with_title_kept(self):
        loan = [f for f in self._facts() if "131.357.622.354" in str(f["raw_value"])]
        tscd = [f for f in self._facts() if "202.406.369.251" in str(f["raw_value"])]
        self.assertTrue(loan and tscd)
        self.assertEqual(loan[0]["heading"], TABLE_NOTE)
        self.assertEqual(loan[0]["subheading"], "18a. Vay ngắn hạn")
        self.assertEqual(tscd[0]["heading"], TABLE_NOTE)
        self.assertEqual(tscd[0]["subheading"], "Tài sản cố định hữu hình")


if __name__ == "__main__":
    unittest.main()
