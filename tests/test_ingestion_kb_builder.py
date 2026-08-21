import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ingestion.kb_builder import build_fact_rows
from ingestion.table_parser import attach_context
from schemas.table_names import TABLE_NOTE


class IngestionKbBuilderTests(unittest.TestCase):
    def test_note_schedule_stays_note_when_note_chapter_mentions_balance_sheet(self):
        md_text = """
# BẢN THUYẾT MINH BÁO CÁO TÀI CHÍNH

# V. THÔNG TIN BỔ SUNG CHO CÁC KHOẢN MỤC TRÌNH BÀY TRONG BẢNG CÂN ĐỐI KẾ TOÁN

### 2a. Chứng khoán kinh doanh

| Chỉ tiêu | Số đầu năm |
|---|---:|
| Công ty A | 3.920.700.000 |
"""

        rows = build_fact_rows(
            attach_context(md_text),
            company="APEC",
            source="fixture.md",
            fiscal_year=2025,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[2], TABLE_NOTE)
        self.assertEqual(row[5], "2a. Chứng khoán kinh doanh")

    def test_note_section_schedule_uses_table_note_heading_and_original_subheading(self):
        md_text = """
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

### 18. Vay ngắn hạn

| Chỉ tiêu | 30/06/2025 |
|---|---:|
| Ngân hàng A | 1.234 |
"""

        rows = build_fact_rows(
            attach_context(md_text),
            company="APEC",
            source="fixture.md",
            fiscal_year=2025,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[2], TABLE_NOTE)
        self.assertEqual(row[5], "18. Vay ngắn hạn")
        self.assertEqual(row[6], "Ngân hàng A | 30/06/2025")
        self.assertEqual(row[7], "1.234")

    def test_read_with_notes_sentence_does_not_start_note_section(self):
        md_text = """
# BẢNG CÂN ĐỐI KẾ TOÁN

| Chỉ tiêu | Số cuối năm |
|---|---:|
| Tiền | 100 |

Báo cáo này phải được đọc cùng với Bản thuyết minh Báo cáo tài chính

# BÁO CÁO LƯU CHUYỂN TIỀN TỆ

| Chỉ tiêu | Số cuối năm |
|---|---:|
| Lưu chuyển tiền thuần | 200 |
"""

        rows = build_fact_rows(
            attach_context(md_text),
            company="APEC",
            source="fixture.md",
            fiscal_year=2025,
        )

        self.assertEqual(rows[0][2], "BẢNG CÂN ĐỐI KẾ TOÁN")
        self.assertEqual(rows[1][2], "BÁO CÁO LƯU CHUYỂN TIỀN TỆ")

    def test_unit_caption_and_stock_count_unit_survive_ingestion(self):
        md_text = """
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

Đơn vị tính: VND

## 19b. Cổ phiếu

| Chỉ tiêu | Số cuối kỳ |
|---|---:|
| Số lượng cổ phiếu phổ thông đang lưu hành | 84.083.976 |
| Mệnh giá cổ phiếu | 10.000 |
"""

        rows = build_fact_rows(
            attach_context(md_text),
            company="APEC",
            source="fixture.md",
            fiscal_year=2024,
        )

        by_name = {row[6].split(" | ", 1)[0]: row for row in rows}
        self.assertEqual(
            by_name["Số lượng cổ phiếu phổ thông đang lưu hành"][13],
            "cổ phiếu",
        )
        self.assertEqual(by_name["Mệnh giá cổ phiếu"][13], "VND")


if __name__ == "__main__":
    unittest.main()
