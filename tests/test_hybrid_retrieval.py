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


if __name__ == "__main__":
    unittest.main()
