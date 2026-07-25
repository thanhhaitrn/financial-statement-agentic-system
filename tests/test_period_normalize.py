"""Tests for period/value-type/unit canonicalization and slot-match reranking."""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ingestion.period_normalize import (
    canonical_period,
    canonical_value_type,
    column_period,
    parse_unit,
    period_phrase_alias,
    section_total_alias,
    section_total_key,
)


class CanonicalPeriodTests(unittest.TestCase):
    def test_relative_query_terms(self):
        self.assertEqual(canonical_period("Tổng tài sản ngắn hạn cuối kỳ"), "cuối")
        self.assertEqual(canonical_period("số dư đầu năm"), "đầu")
        self.assertEqual(canonical_period("cuối quý"), "cuối")

    def test_balance_sheet_date_columns(self):
        self.assertEqual(canonical_period("31/12/2024VND"), "cuối")
        self.assertEqual(canonical_period("01/01/2024VND"), "đầu")

    def test_ambiguous_returns_empty(self):
        self.assertEqual(canonical_period("Mã số"), "")
        self.assertEqual(canonical_period("Năm 2024VND"), "")  # needs sibling context

    def test_dau_tu_is_not_period(self):
        # "đầu tư" (investment) must NOT be read as period "đầu".
        self.assertEqual(canonical_period("bất động sản đầu tư"), "")
        self.assertEqual(canonical_period("giá trị bất động sản đầu tư"), "")

    def test_comparison_returns_empty(self):
        # A question naming both periods needs both → no single-period penalty.
        self.assertEqual(
            canonical_period("So sánh giá trị cuối kỳ với đầu năm"), ""
        )

    def test_column_period_year_ranking(self):
        cols = ["Năm 2024VND", "Năm 2023VND"]
        self.assertEqual(column_period("Năm 2024VND", cols), "cuối")
        self.assertEqual(column_period("Năm 2023VND", cols), "đầu")


class CanonicalValueTypeTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(canonical_value_type("Nguyên giá"), "nguyên giá")
        self.assertEqual(canonical_value_type("Giá trị còn lại"), "giá trị còn lại")
        self.assertEqual(canonical_value_type("Giá trị hao mòn lũy kế"), "hao mòn")
        self.assertEqual(canonical_value_type("Tài sản cố định hữu hình"), "")

    def test_note_value_types(self):
        # Provision, fair value, and cost (giá gốc ≡ nguyên giá) from note columns.
        self.assertEqual(canonical_value_type("Số cuối kỳ Dự phòng"), "dự phòng")
        self.assertEqual(canonical_value_type("Số cuối kỳ Giá gốc"), "nguyên giá")
        self.assertEqual(canonical_value_type("Số cuối năm Giá trị hợp lý"), "giá trị hợp lý")
        # A "dự phòng" question must not be read as cost.
        self.assertEqual(canonical_value_type("dự phòng đầu tư vào công ty con"), "dự phòng")


class PeriodPhraseAliasTests(unittest.TestCase):
    def test_trong_nam_to_trong_ky(self):
        self.assertEqual(
            period_phrase_alias("Lưu chuyển tiền thuần trong năm"),
            "Lưu chuyển tiền thuần trong kỳ",
        )
        # Already "trong kỳ" or unrelated -> no alias.
        self.assertEqual(period_phrase_alias("Lưu chuyển tiền thuần trong kỳ"), "")
        self.assertEqual(period_phrase_alias("Tiền và tương đương tiền cuối kỳ"), "")


class ParseUnitTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_unit("31/12/2024VND"), "VND")
        self.assertEqual(parse_unit("Đơn vị: nghìn đồng"), "nghìn đồng")
        self.assertEqual(parse_unit("triệu đồng"), "triệu đồng")
        self.assertEqual(parse_unit("Mã số"), "")


class SectionTotalTests(unittest.TestCase):
    def test_key_buckets(self):
        self.assertEqual(section_total_key("Tổng tài sản ngắn hạn cuối kỳ"), "ts_ngan_han")
        self.assertEqual(section_total_key("A - TÀI SẢN NGẮN HẠN | Số cuối năm"), "ts_ngan_han")
        self.assertEqual(section_total_key("B - TÀI SẢN DÀI HẠN | Số cuối năm"), "ts_dai_han")
        self.assertEqual(section_total_key("TỔNG CỘNG TÀI SẢN | Số cuối năm"), "tong_tai_san")
        # A non-total component row must NOT bucket as a section total.
        self.assertEqual(section_total_key("Tài sản ngắn hạn khác | Số cuối năm"), "")

    def test_key_buckets_liabilities_equity(self):
        self.assertEqual(section_total_key("Tổng nợ phải trả cuối kỳ"), "no_phai_tra")
        self.assertEqual(section_total_key("C - NỢ PHẢI TRẢ | Số cuối năm"), "no_phai_tra")
        self.assertEqual(section_total_key("I. Nợ ngắn hạn | Số cuối năm"), "no_ngan_han")
        self.assertEqual(section_total_key("II. Nợ dài hạn | Số cuối năm"), "no_dai_han")
        self.assertEqual(section_total_key("D - NGUỒN VỐN CHỦ SỞ HỮU | Số cuối năm"), "von_chu")
        self.assertEqual(section_total_key("TỔNG CỘNG NGUỒN VỐN | Số cuối năm"), "nguon_von")

    def test_alias_from_label(self):
        self.assertEqual(section_total_alias("A - TÀI SẢN NGẮN HẠN | Số cuối năm"), "Tổng tài sản ngắn hạn")
        self.assertEqual(section_total_alias("C - NỢ PHẢI TRẢ | Số cuối năm"), "Tổng nợ phải trả")
        self.assertEqual(section_total_alias("I. Nợ ngắn hạn | Số cuối năm"), "Tổng nợ ngắn hạn")
        self.assertEqual(section_total_alias("II. Nợ dài hạn | Số cuối năm"), "Tổng nợ dài hạn")
        self.assertEqual(section_total_alias("TỔNG CỘNG NGUỒN VỐN | Số cuối năm"), "Tổng nguồn vốn")
        # Component rows get no alias.
        self.assertEqual(section_total_alias("Tài sản ngắn hạn khác | Số cuối năm"), "")
        self.assertEqual(section_total_alias("Phải thu ngắn hạn của khách hàng | Số cuối năm"), "")


class IntentPropagationTests(unittest.TestCase):
    def test_intent_recovers_stripped_qualifier(self):
        from tools import tools

        # Keyworder strips "giá trị/cuối kỳ"; the bare keyword reaches `query` but
        # the original question is passed as `intent`.
        kw = "tài sản cố định hữu hình"
        intent = "Giá trị tài sản cố định hữu hình cuối kỳ là bao nhiêu VND?"
        net = {"item_name": "Tài sản cố định hữu hình | Số cuối năm", "value_type": "", "period": "cuối"}
        gross = {"item_name": "Nguyên giá | Số cuối năm", "value_type": "nguyên giá", "period": "cuối"}
        s_net = tools._item_match_score(kw, net, "16.326.198.818", intent=intent)
        s_gross = tools._item_match_score(kw, gross, "24.034.952.927", intent=intent)
        self.assertGreater(s_net, s_gross)

    def test_section_total_boost(self):
        from tools import tools

        intent = "Tổng tài sản ngắn hạn cuối kỳ là bao nhiêu VND?"
        total = {"item_name": "A - TÀI SẢN NGẮN HẠN | Số cuối năm", "period": "cuối"}
        other = {"item_name": "Tài sản ngắn hạn khác | Số cuối năm", "period": "cuối"}
        s_total = tools._item_match_score("tài sản ngắn hạn", total, "984.330.724.539", intent=intent)
        s_other = tools._item_match_score("tài sản ngắn hạn", other, "9.720.712.203", intent=intent)
        self.assertGreater(s_total, s_other)


class SlotMatchRerankTests(unittest.TestCase):
    def _score(self, query, value_type, period):
        from tools import tools

        meta = {
            "item_name": "Tài sản cố định hữu hình | 31/12/2024VND",
            "subheading": "Tài sản cố định hữu hình",
            "value_type": value_type,
            "period": period,
        }
        return tools._item_match_score(query, meta, "Giá trị 16.326.198.818")

    def test_value_type_match_beats_mismatch(self):
        query = "Giá trị còn lại của tài sản cố định hữu hình cuối kỳ là bao nhiêu?"
        match = self._score(query, "giá trị còn lại", "cuối")
        mismatch = self._score(query, "nguyên giá", "cuối")
        self.assertGreater(match, mismatch)

    def test_period_match_beats_mismatch(self):
        query = "Nguyên giá tài sản cố định hữu hình cuối kỳ là bao nhiêu?"
        match = self._score(query, "nguyên giá", "cuối")
        mismatch = self._score(query, "nguyên giá", "đầu")
        self.assertGreater(match, mismatch)


if __name__ == "__main__":
    unittest.main()
