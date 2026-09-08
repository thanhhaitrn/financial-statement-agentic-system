"""Tests for the lexical hybrid recall booster in get_related_info."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import unittest

import tools.tools as tools_module
from tools.tools import get_related_info
from vectorstore.lexical_index import LexicalIndex, get_lexical_index, reset_lexical_index

BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
NOTE = "THUYẾT MINH BÁO CÁO TÀI CHÍNH"

# Gold fact carries a distinctive VND amount; a distractor shares the heading.
GOLD = (
    "Bảng BẢNG CÂN ĐỐI KẾ TOÁN. Nguyên giá tài sản cố định hữu hình | 2024 VND. "
    "Giá trị 202.406.369.251.",
    {"heading": BS, "item_name": "Nguyên giá tài sản cố định hữu hình | 2024 VND",
     "raw_value": "202.406.369.251", "source": "apec.md"},
)
DISTRACTOR = (
    "Bảng BẢNG CÂN ĐỐI KẾ TOÁN. Tài sản ngắn hạn | 2024 VND. Giá trị 27.309.234.148.",
    {"heading": BS, "item_name": "Tài sản ngắn hạn | 2024 VND",
     "raw_value": "27.309.234.148", "source": "apec.md"},
)
OTHER_TABLE = (
    "Bảng BÁO CÁO KẾT QUẢ. Doanh thu | 2024 VND. Giá trị 5.000.000.000.",
    {"heading": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", "item_name": "Doanh thu | 2024 VND",
     "raw_value": "5.000.000.000", "source": "apec.md"},
)


class FakeHybridCollection:
    """Dense .query misses the gold fact; .get exposes the whole corpus."""

    def __init__(self, corpus, dense_hits):
        self.corpus = corpus
        self.dense_hits = dense_hits
        self.query_calls = 0

    def query(self, query_embeddings, n_results, where=None):
        self.query_calls += 1
        docs = [d for d, _ in self.dense_hits]
        metas = [m for _, m in self.dense_hits]
        return {"documents": [docs], "metadatas": [metas]}

    def get(self, where=None, include=None):
        docs = [d for d, _ in self.corpus]
        metas = [m for _, m in self.corpus]
        return {"documents": docs, "metadatas": metas}


class FakeStructuredCollection:
    def __init__(self, corpus, dense_hits):
        self.name = "structured-slot-test"
        self.corpus = corpus
        self.dense_hits = dense_hits
        self.query_calls = 0
        self.get_calls = []

    def query(self, query_embeddings, n_results, where=None):
        self.query_calls += 1
        docs = [doc for doc, _meta in self.dense_hits]
        metas = [meta for _doc, meta in self.dense_hits]
        return {"documents": [docs], "metadatas": [metas]}

    def get(self, where=None, include=None):
        where = dict(where or {})
        self.get_calls.append(where)
        rows = [
            (doc, meta)
            for doc, meta in self.corpus
            if all(str(meta.get(key, "") or "") == str(value) for key, value in where.items())
        ]
        return {
            "documents": [doc for doc, _meta in rows],
            "metadatas": [meta for _doc, meta in rows],
        }


class LexicalIndexTest(unittest.TestCase):
    def test_retrieves_table_filtered_match(self):
        idx = LexicalIndex([GOLD[0], DISTRACTOR[0], OTHER_TABLE[0]],
                           [GOLD[1], DISTRACTOR[1], OTHER_TABLE[1]])
        hits = idx.query("nguyên giá tài sản cố định hữu hình", table=BS, top_n=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0][1]["item_name"], GOLD[1]["item_name"])
        # heading filter excludes the income-statement row
        self.assertTrue(all(m["heading"] == BS for _, m in hits))

    def test_matches_distinctive_figure(self):
        idx = LexicalIndex([GOLD[0], DISTRACTOR[0]], [GOLD[1], DISTRACTOR[1]])
        hits = idx.query("202.406.369.251", table=BS, top_n=5)
        self.assertEqual(hits[0][1]["item_name"], GOLD[1]["item_name"])

    def test_empty_corpus_is_safe(self):
        idx = LexicalIndex([], [])
        self.assertFalse(idx.ready)
        self.assertEqual(idx.query("anything", table=BS), [])


class HybridGetRelatedInfoTest(unittest.TestCase):
    def setUp(self):
        reset_lexical_index()
        tools_module.embed_query_text = lambda _q: [0.0]

    def tearDown(self):
        reset_lexical_index()

    def test_lexical_surfaces_fact_dense_missed(self):
        col = FakeHybridCollection(
            corpus=[GOLD, DISTRACTOR, OTHER_TABLE],
            dense_hits=[DISTRACTOR],  # dense never returns the gold fact
        )
        result = get_related_info("nguyên giá tài sản cố định hữu hình", BS, col)
        self.assertIn("202.406.369.251", result["context"])
        self.assertEqual(col.query_calls, 1)  # still a single dense query call

    def test_cross_table_surfaces_fact_from_wrong_routed_table(self):
        # Gold lives in a note-schedule heading the router can't reach; the query
        # is (mis)routed to the balance sheet. cross_table must still surface it.
        note_gold = (
            "Bảng 18a. Vay ngắn hạn. Vay ngân hàng ngắn hạn | 2024 VND. Giá trị 12.345.678.901.",
            {"heading": "18a. Vay ngắn hạn", "item_name": "Vay ngân hàng ngắn hạn | 2024 VND",
             "raw_value": "12.345.678.901", "source": "apec.md"},
        )
        col = FakeHybridCollection(
            corpus=[note_gold, DISTRACTOR, OTHER_TABLE],
            dense_hits=[DISTRACTOR],
        )
        on = get_related_info("vay ngân hàng ngắn hạn", BS, col, cross_table=True)
        self.assertIn("12.345.678.901", on["context"])

        reset_lexical_index()
        off = get_related_info("vay ngân hàng ngắn hạn", BS, col, cross_table=False)
        self.assertNotIn("12.345.678.901", off["context"])

    def test_no_lexical_index_falls_back_to_dense(self):
        # A collection without .get cannot build a lexical index -> pure dense.
        class DenseOnly:
            def __init__(self):
                self.query_calls = 0

            def query(self, query_embeddings, n_results, where=None):
                self.query_calls += 1
                return {"documents": [[DISTRACTOR[0]]], "metadatas": [[DISTRACTOR[1]]]}

        col = DenseOnly()
        result = get_related_info("nguyên giá tài sản cố định hữu hình", BS, col)
        self.assertIn("Tài sản ngắn hạn", result["context"])
        self.assertNotIn("202.406.369.251", result["context"])
        self.assertEqual(col.query_calls, 1)

    def test_synonym_is_normalized_once_for_dense_lexical_and_rerank(self):
        captured_queries = []
        tools_module.embed_query_text = lambda query: captured_queries.append(query) or [0.0]
        retained_earnings = (
            "Bảng BẢNG CÂN ĐỐI KẾ TOÁN. Lợi nhuận sau thuế chưa phân phối | "
            "Số cuối năm. Giá trị 43.404.961.299 VND.",
            {
                "heading": BS,
                "item_name": "Lợi nhuận sau thuế chưa phân phối | Số cuối năm",
                "raw_value": "43.404.961.299",
                "unit": "VND",
                "source": "apec.md",
            },
        )
        col = FakeHybridCollection(
            corpus=[retained_earnings, DISTRACTOR],
            dense_hits=[DISTRACTOR],
        )

        result = get_related_info("lợi nhuận giữ lại", BS, col)

        self.assertEqual(captured_queries, ["lợi nhuận sau thuế chưa phân phối"])
        self.assertEqual(result["canonical_query"], "lợi nhuận sau thuế chưa phân phối")
        self.assertIn("43.404.961.299", result["context"])

    def test_exact_structured_slot_hit_avoids_dense_query(self):
        exact = (
            "Dự phòng phải thu ngắn hạn khó đòi | CTCP ABC | Cuối kỳ: 100 VND",
            {
                "heading": BS,
                "item_name": "Dự phòng phải thu ngắn hạn khó đòi | CTCP ABC | Cuối kỳ",
                "period": "cuối",
                "value_type": "dự phòng",
                "aggregation_level": "component",
                "raw_value": "100",
                "source": "report.md",
            },
        )
        col = FakeStructuredCollection(corpus=[exact], dense_hits=[DISTRACTOR])
        query = (
            "Dự phòng phải thu ngắn hạn khó đòi của CTCP ABC "
            "cuối kỳ là bao nhiêu?"
        )

        result = get_related_info(query, BS, col, intent=query)

        self.assertEqual(col.query_calls, 0)
        self.assertEqual(result["retrieval_mode"], "structured_slots")
        self.assertIn("100 VND", result["context"])
        self.assertIn(
            {
                "heading": BS,
                "period": "cuối",
                "value_type": "dự phòng",
                "aggregation_level": "component",
            },
            col.get_calls,
        )

    def test_structured_slot_probe_can_be_disabled_for_narrative_retrieval(self):
        exact = (
            "Công ty được cổ phần hóa ngày 01/10/2003.",
            {
                "heading": NOTE,
                "item_name": "Cổ phần hóa",
                "raw_value": "Công ty được cổ phần hóa ngày 01/10/2003.",
                "fact_id": "corporatization",
                "source": "report.md#page=12",
            },
        )
        registration = (
            "Công ty đăng ký trở thành công ty cổ phần ngày 20/11/2003.",
            {
                "heading": NOTE,
                "item_name": "Đăng ký công ty cổ phần",
                "raw_value": (
                    "Công ty đăng ký trở thành công ty cổ phần "
                    "ngày 20/11/2003."
                ),
                "fact_id": "joint-stock-registration",
                "source": "report.md#page=12",
            },
        )
        col = FakeStructuredCollection(
            corpus=[exact, registration],
            dense_hits=[exact, registration],
        )
        query = "sự kiện cổ phần hóa và đăng ký công ty cổ phần"

        result = get_related_info(
            query,
            NOTE,
            col,
            strict_table=True,
            cross_table=False,
            intent=query,
            structured_slots=False,
        )

        self.assertEqual(col.query_calls, 1)
        self.assertNotEqual(result.get("retrieval_mode"), "structured_slots")
        self.assertIn("đăng ký trở thành công ty cổ phần", result["context"])

    def test_geographic_net_revenue_uses_surface_slots_before_synonym_expansion(self):
        foreign_net_revenue = (
            "Doanh thu thuần | Nước ngoài 2025 VND: 7.105.418.295.238",
            {
                "heading": NOTE,
                "fact_id": "foreign-net-revenue-2025",
                "metric_label": "Doanh thu thuần",
                "row_label": "Doanh thu thuần",
                "column_label": "Nước ngoài 2025 VND",
                "item_name": "Doanh thu thuần | Nước ngoài 2025 VND",
                "period_label": "Nước ngoài 2025 VND",
                "aggregation_level": "component",
                "entity_label": "Nước ngoài",
                "geography": "Nước ngoài",
                # Deliberately blank. Expanding "doanh thu thuần" to the long
                # primary-statement synonym must not fabricate sale semantics.
                "transaction_type": "",
                "raw_value": "7.105.418.295.238",
                "source": "report.md#geographic-segments",
            },
        )
        col = FakeStructuredCollection(
            corpus=[foreign_net_revenue],
            dense_hits=[DISTRACTOR],
        )
        query = "doanh thu thuần từ thị trường nước ngoài 2025"

        result = get_related_info(
            query,
            NOTE,
            col,
            strict_table=True,
            cross_table=False,
            intent=query,
        )

        self.assertEqual(col.query_calls, 0)
        self.assertEqual(result["retrieval_mode"], "structured_slots")
        self.assertEqual(
            result["metadatas"][0]["fact_id"],
            "foreign-net-revenue-2025",
        )

    def test_structured_slot_miss_falls_back_to_dense(self):
        col = FakeStructuredCollection(
            corpus=[DISTRACTOR],
            dense_hits=[DISTRACTOR],
        )
        query = (
            "Dự phòng phải thu ngắn hạn khó đòi của CTCP ABC "
            "cuối kỳ là bao nhiêu?"
        )

        result = get_related_info(query, BS, col, intent=query)

        self.assertEqual(col.query_calls, 1)
        self.assertNotEqual(result.get("retrieval_mode"), "structured_slots")
        self.assertIn("Tài sản ngắn hạn", result["context"])

    def test_generic_current_year_metric_does_not_structured_return_early(self):
        wrong = (
            "Doanh thu cung cấp dịch vụ | Năm nay: 20 VND",
            {
                "heading": BS,
                "metric_label": "Doanh thu cung cấp dịch vụ",
                "item_name": "Doanh thu cung cấp dịch vụ | Năm nay",
                "period_role": "current",
                "raw_value": "20",
            },
        )
        gold = (
            "Doanh thu bán hàng hóa | Năm nay: 100 VND",
            {
                "heading": BS,
                "metric_label": "Doanh thu bán hàng hóa",
                "item_name": "Doanh thu bán hàng hóa | Năm nay",
                "period_label": "Năm nay",
                "raw_value": "100",
            },
        )
        col = FakeStructuredCollection(
            corpus=[wrong, gold],
            dense_hits=[gold],
        )
        query = (
            "Doanh thu gộp từ hàng hóa giữ để bán trong năm hiện tại "
            "là bao nhiêu?"
        )

        result = get_related_info(query, BS, col, intent=query)

        self.assertEqual(col.query_calls, 1)
        self.assertNotEqual(result.get("retrieval_mode"), "structured_slots")
        self.assertIn("Doanh thu bán hàng hóa", result["context"])

    def test_full_ratio_never_structured_returns_after_only_one_leg(self):
        total_assets = (
            "Tổng cộng tài sản | Cuối kỳ: 1.000 VND",
            {
                "heading": BS,
                "item_name": "Tổng cộng tài sản | Cuối kỳ",
                "period": "cuối",
                "aggregation_level": "total",
                "raw_value": "1000",
                "source": "report.md",
            },
        )
        inventory = (
            "Hàng tồn kho | Cuối kỳ: 250 VND",
            {
                "heading": BS,
                "item_name": "Hàng tồn kho | Cuối kỳ",
                "period": "cuối",
                "aggregation_level": "component",
                "raw_value": "250",
                "source": "report.md",
            },
        )
        col = FakeStructuredCollection(
            corpus=[total_assets, inventory],
            dense_hits=[inventory, total_assets],
        )
        query = "Tỷ trọng hàng tồn kho trên tổng tài sản cuối kỳ?"

        result = get_related_info(query, BS, col, intent=query)

        self.assertEqual(col.query_calls, 1)
        self.assertNotEqual(result.get("retrieval_mode"), "structured_slots")
        self.assertIn("Hàng tồn kho", result["context"])
        self.assertIn("Tổng cộng tài sản", result["context"])

    def test_roll_forward_completes_only_same_block_without_widening_limit(self):
        block_rows = [
            (
                "Vay ngắn hạn | Số đầu năm: 80 VND",
                {
                    "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "block_id": "loan-roll-forward",
                    "metric_label": "Vay ngắn hạn",
                    "item_name": "Vay ngắn hạn | Số đầu năm",
                    "period": "đầu",
                    "raw_value": "80",
                },
            ),
            (
                "Vay ngắn hạn | Vay thêm trong năm: 40 VND",
                {
                    "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "block_id": "loan-roll-forward",
                    "metric_label": "Vay ngắn hạn",
                    "item_name": "Vay ngắn hạn | Vay thêm trong năm",
                    "raw_value": "40",
                },
            ),
            (
                "Vay ngắn hạn | Hoàn trả trong năm: 20 VND",
                {
                    "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "block_id": "loan-roll-forward",
                    "metric_label": "Vay ngắn hạn",
                    "item_name": "Vay ngắn hạn | Hoàn trả trong năm",
                    "raw_value": "20",
                },
            ),
            (
                "Vay ngắn hạn | Số cuối năm: 100 VND",
                {
                    "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "block_id": "loan-roll-forward",
                    "metric_label": "Vay ngắn hạn",
                    "item_name": "Vay ngắn hạn | Số cuối năm",
                    "period": "cuối",
                    "raw_value": "100",
                },
            ),
        ]
        unrelated = (
            "Vay ngắn hạn | Số cuối năm: 999 VND",
            {
                "heading": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "block_id": "different-loan-block",
                "metric_label": "Vay ngắn hạn",
                "item_name": "Vay ngắn hạn | Số cuối năm",
                "period": "cuối",
                "raw_value": "999",
            },
        )
        col = FakeStructuredCollection(
            corpus=[*block_rows, unrelated],
            dense_hits=[block_rows[-1], unrelated],
        )
        query = "Tình hình tăng giảm vay ngắn hạn trong năm?"

        result = get_related_info(
            query,
            "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
            col,
            strict_table=True,
            cross_table=False,
            limit=4,
            intent=query,
        )

        self.assertEqual(len(result["documents"]), 4)
        self.assertNotIn("999 VND", result["context"])
        self.assertTrue(
            all(
                marker in result["context"]
                for marker in (
                    "Số đầu năm",
                    "Vay thêm trong năm",
                    "Hoàn trả trong năm",
                    "Số cuối năm",
                )
            )
        )
        self.assertIn({"block_id": "loan-roll-forward"}, col.get_calls)

    def test_closed_component_marker_requires_every_block_component_to_fit(self):
        query = "Phân tích cơ cấu của chi phí bán hàng"
        slots = tools_module.parse_query_slots(query)
        docs = [
            "Tổng chi phí bán hàng: 100 VND",
            "Chi phí nhân viên: 60 VND",
            "Chi phí dịch vụ: 40 VND",
        ]
        metas = [
            {
                "block_id": "selling-expense-components",
                "item_name": "Tổng chi phí bán hàng",
                "aggregation_level": "total",
            },
            {
                "block_id": "selling-expense-components",
                "item_name": "Chi phí nhân viên",
                "aggregation_level": "component",
            },
            {
                "block_id": "selling-expense-components",
                "item_name": "Chi phí dịch vụ",
                "aggregation_level": "component",
            },
        ]

        complete_metas = [dict(meta) for meta in metas]
        chosen = tools_module._inject_required_siblings(
            [0, 1, 2],
            docs,
            complete_metas,
            slots=slots,
            limit=3,
        )
        self.assertEqual(chosen, [0, 1, 2])
        self.assertEqual(
            complete_metas[0]["coverage_complete_legs"],
            ["components_closed"],
        )

        truncated_metas = [dict(meta) for meta in metas]
        tools_module._inject_required_siblings(
            [0, 1, 2],
            docs,
            truncated_metas,
            slots=slots,
            limit=2,
        )
        self.assertNotIn("coverage_complete_legs", truncated_metas[0])


if __name__ == "__main__":
    unittest.main()
