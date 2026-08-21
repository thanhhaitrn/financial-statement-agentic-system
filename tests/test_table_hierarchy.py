"""Tests for A8: hierarchical/section context restored into fact subheadings."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

from ingestion.kb_builder import build_fact_rows
from ingestion.table_parser import attach_context
from schemas.requirements import requirement_name_matches_fact


def _facts(md):
    rows = build_fact_rows(attach_context(md), company="APEC", source="x.md", fiscal_year="2024")
    # row tuple: company, fy, heading, item_code, note_ref, subheading, item_name, value, ...
    return [{"heading": r[2], "subheading": r[5], "item_name": r[6], "value": r[7]} for r in rows]


BS_MD = """# BẢNG CÂN ĐỐI KẾ TOÁN

| Chỉ tiêu | Mã số | Số cuối năm | Số đầu năm |
| --- | --- | --- | --- |
| 1. Tài sản cố định hữu hình | 221 | 16.326.198.818 | 189.912.249.532 |
| Nguyên giá | 222 | 24.034.952.927 | 202.406.369.251 |
| Giá trị hao mòn lũy kế | 223 | (7.708.754.109) | (12.494.119.719) |
| 3. Tài sản cố định vô hình | 227 | 17.566.667 | 51.732.022 |
| Nguyên giá | 228 | 337.728.000 | 337.728.000 |
"""

MATRIX_MD = """## THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 9. Tài sản cố định hữu hình

| Nguyên giá | Nhà cửa, vật kiến trúc | Cộng |
| --- | --- | --- |
| Số đầu năm | 196.560.414.828 | 202.406.369.251 |
| Số cuối kỳ | 18.238.988.174 | 24.034.952.927 |
| Giá trị hao mòn |  |  |
| Số đầu năm | 10.320.799.076 | 12.494.119.719 |
| Số cuối kỳ | (6.217.026.740) | 7.708.754.109 |
"""


class BalanceSheetHierarchyTest(unittest.TestCase):
    def setUp(self):
        self.facts = _facts(BS_MD)

    def _by_value(self, v):
        return [f for f in self.facts if f["value"] == v]

    def test_child_inherits_numbered_parent(self):
        f = self._by_value("202.406.369.251")
        self.assertTrue(f)
        self.assertTrue(f[0]["item_name"].lower().startswith("nguyên giá"))
        self.assertEqual(f[0]["subheading"], "Tài sản cố định hữu hình")

    def test_same_label_disambiguated_across_parents(self):
        # "Nguyên giá | Số đầu năm" exists under two parents with different values.
        huu_hinh = self._by_value("202.406.369.251")[0]
        vo_hinh = [f for f in self.facts if f["value"] == "337.728.000"
                   and f["item_name"].lower().startswith("nguyên giá")][0]
        self.assertNotEqual(huu_hinh["subheading"], vo_hinh["subheading"])
        self.assertEqual(vo_hinh["subheading"], "Tài sản cố định vô hình")

    def test_match_filter_uses_subheading(self):
        f = self._by_value("202.406.369.251")[0]
        # query by concept matches the hierarchical fact only via its subheading
        self.assertTrue(
            requirement_name_matches_fact("nguyên giá tài sản cố định hữu hình", f, table=f["heading"])
        )


class NoteMatrixSectionTest(unittest.TestCase):
    def setUp(self):
        self.facts = _facts(MATRIX_MD)

    def test_section_from_header_in_subheading(self):
        f = [x for x in self.facts if x["value"] == "202.406.369.251"]
        self.assertTrue(f)
        self.assertIn("Nguyên giá", f[0]["subheading"])
        self.assertIn("Tài sản cố định hữu hình", f[0]["subheading"])

    def test_divider_switches_section_and_yields_no_fact(self):
        # the value below the "Giá trị hao mòn" divider must carry that section
        hm = [x for x in self.facts if x["value"] == "12.494.119.719"]
        self.assertTrue(hm)
        self.assertIn("Giá trị hao mòn", hm[0]["subheading"])
        # the divider row itself produced no fact
        self.assertFalse([x for x in self.facts if x["item_name"].lower().startswith("giá trị hao mòn |")])

    def test_repeated_cell_label_disambiguated_by_section(self):
        # "Số đầu năm | Cộng" appears in both Nguyên giá and Giá trị hao mòn sections
        same = [x for x in self.facts if x["item_name"] == "Số đầu năm | Cộng"]
        subs = {x["subheading"] for x in same}
        self.assertGreaterEqual(len(subs), 2)


if __name__ == "__main__":
    unittest.main()
