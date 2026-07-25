import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# The reranker lazily imports llm.invoke -> llm.client, which requires an API key
# at import time. A dummy value is enough; the LLM call itself is always mocked.
os.environ.setdefault("OLLAMA_API_KEY", "test-key")

from vectorstore import llm_reranker


def _result(parsed=None, raw=None):
    return {"parsed": parsed, "raw": raw, "parsing_error": None, "mode": "structured"}


class _Msg:
    """Minimal stand-in for a LangChain AIMessage (has `.content`)."""

    def __init__(self, content):
        self.content = content


class LlmRerankerTests(unittest.TestCase):
    def test_disabled_returns_empty(self):
        # Flag off (default): no-op, never touches the LLM.
        with patch.dict(os.environ, {"LLM_RERANK": "0"}, clear=False):
            with patch("llm.invoke.invoke_prompt") as mock_invoke:
                self.assertEqual(llm_reranker.llm_rerank_order("q", ["a", "b"]), [])
                mock_invoke.assert_not_called()

    def test_single_doc_no_op(self):
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch("llm.invoke.invoke_prompt") as mock_invoke:
                self.assertEqual(llm_reranker.llm_rerank_order("q", ["only"]), [])
                mock_invoke.assert_not_called()

    def test_valid_ordering_from_parsed(self):
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch(
                "llm.invoke.invoke_prompt",
                return_value=_result(parsed={"ranking": [2, 0, 1]}),
            ):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b", "c"]), [2, 0, 1]
                )

    def test_filters_out_of_range_and_dupes(self):
        # n=3 -> valid ids 0..2; 5 is dropped, repeat 2 deduped, -1 dropped.
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch(
                "llm.invoke.invoke_prompt",
                return_value=_result(parsed={"ranking": [5, 2, 2, -1, 0]}),
            ):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b", "c"]), [2, 0]
                )

    def test_plain_text_fallback(self):
        # Model ignored structured output and returned plain JSON in the message.
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch(
                "llm.invoke.invoke_prompt",
                return_value=_result(raw=_Msg('Here you go: {"ranking": [1, 0]}')),
            ):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b"]), [1, 0]
                )

    def test_bare_array_fallback(self):
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch(
                "llm.invoke.invoke_prompt",
                return_value=_result(raw=_Msg("[1, 0]")),
            ):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b"]), [1, 0]
                )

    def test_exception_is_no_op(self):
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch("llm.invoke.invoke_prompt", side_effect=RuntimeError("boom")):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b"]), []
                )

    def test_unparseable_is_no_op(self):
        with patch.dict(os.environ, {"LLM_RERANK": "1"}, clear=False):
            with patch(
                "llm.invoke.invoke_prompt",
                return_value=_result(raw=_Msg("no ranking here")),
            ):
                self.assertEqual(
                    llm_reranker.llm_rerank_order("q", ["a", "b"]), []
                )


class RerankBlendTests(unittest.TestCase):
    """The blend in tools._rerank_matches: LLM reorders, heuristic still wins ties."""

    def _rerank(self, heuristic_scores, llm_order):
        from tools import tools

        docs = [f"d{i}" for i in range(len(heuristic_scores))]
        metas = [{} for _ in docs]
        score_by_doc = dict(zip(docs, heuristic_scores))

        with patch.object(tools, "neural_rerank_enabled", return_value=False), patch.object(
            tools, "llm_rerank_enabled", return_value=True
        ), patch.object(
            tools, "llm_rerank_order", return_value=llm_order
        ), patch.object(
            tools, "_item_match_score", side_effect=lambda q, m, d, intent=None: score_by_doc[d]
        ), patch.object(
            tools, "_LLM_RERANK_WEIGHT", 50.0
        ), patch.object(
            tools, "_LLM_RERANK_CANDIDATES", 20
        ):
            ranked_docs, _ = tools._rerank_matches("q", docs, metas, limit=len(docs))
        return ranked_docs

    def test_llm_reorders_near_ties(self):
        # Heuristic order [0,1,2]; LLM ranks [2,0,1] -> docs reordered accordingly.
        self.assertEqual(self._rerank([10, 9, 8], [2, 0, 1]), ["d2", "d0", "d1"])

    def test_exact_match_heuristic_still_wins(self):
        # d0 has a dominant heuristic (e.g. exact figure, +60); even when the LLM
        # ranks it dead last, the blend keeps it on top (blend, not replace).
        self.assertEqual(self._rerank([70, 10, 5], [2, 1, 0]), ["d0", "d2", "d1"])

    def test_disabled_keeps_heuristic_order(self):
        from tools import tools

        docs = ["d0", "d1", "d2"]
        metas = [{} for _ in docs]
        score_by_doc = {"d0": 5, "d1": 9, "d2": 7}
        with patch.object(tools, "neural_rerank_enabled", return_value=False), patch.object(
            tools, "llm_rerank_enabled", return_value=False
        ), patch.object(
            tools, "_item_match_score", side_effect=lambda q, m, d, intent=None: score_by_doc[d]
        ):
            ranked_docs, _ = tools._rerank_matches("q", docs, metas, limit=3)
        self.assertEqual(ranked_docs, ["d1", "d2", "d0"])


class GateTests(unittest.TestCase):
    def test_is_value_lookup_query(self):
        from tools import tools

        # Direct figure / ratio lookups -> gated (skip rerank).
        self.assertTrue(tools._is_value_lookup_query("Giá trị TSCĐ cuối kỳ là bao nhiêu VND?"))
        self.assertTrue(tools._is_value_lookup_query("Tỷ lệ tài sản cố định là bao nhiêu phần trăm?"))
        # Analytical questions keep the reranker even when they mention figures.
        self.assertFalse(tools._is_value_lookup_query("Đánh giá hiệu quả quản lý chi phí khi giá trị thay đổi"))
        self.assertFalse(tools._is_value_lookup_query("Ai là Tổng Giám đốc của công ty?"))

    def test_value_lookup_skips_llm(self):
        from tools import tools

        docs = ["d0", "d1", "d2"]
        metas = [{} for _ in docs]
        score_by_doc = {"d0": 10, "d1": 9, "d2": 8}
        with patch.object(tools, "neural_rerank_enabled", return_value=False), patch.object(
            tools, "llm_rerank_enabled", return_value=True
        ), patch.object(
            tools, "llm_rerank_order"
        ) as mock_order, patch.object(
            tools, "_item_match_score", side_effect=lambda q, m, d, intent=None: score_by_doc[d]
        ):
            ranked_docs, _ = tools._rerank_matches(
                "Tổng tài sản ngắn hạn cuối kỳ là bao nhiêu VND?", docs, metas, limit=3
            )
        mock_order.assert_not_called()
        self.assertEqual(ranked_docs, ["d0", "d1", "d2"])  # pure heuristic order


class UnionGuardTests(unittest.TestCase):
    def _rerank(self, heuristic, llm_order, *, protect, limit):
        from tools import tools

        docs = [f"d{i}" for i in range(len(heuristic))]
        metas = [{} for _ in docs]
        score_by_doc = dict(zip(docs, heuristic))
        with patch.object(tools, "neural_rerank_enabled", return_value=False), patch.object(
            tools, "llm_rerank_enabled", return_value=True
        ), patch.object(
            tools, "llm_rerank_order", return_value=llm_order
        ), patch.object(
            tools, "_item_match_score", side_effect=lambda q, m, d, intent=None: score_by_doc[d]
        ), patch.object(
            tools, "_LLM_RERANK_WEIGHT", 100.0
        ), patch.object(
            tools, "_LLM_RERANK_CANDIDATES", 20
        ), patch.object(
            tools, "_LLM_RERANK_PROTECT", protect
        ):
            ranked_docs, _ = tools._rerank_matches("q", docs, metas, limit=limit)
        return ranked_docs

    def test_union_protects_heuristic_top(self):
        # d0 is heuristic top but the LLM (weight 100) ranks it last, pushing it
        # below a limit-2 cut. protect=1 forces it back into the final set.
        # heur order [0,1,2,3]; llm_order [1,2,3,0] -> blended top-2 = d1,d2.
        with_guard = self._rerank([10, 9, 8, 7], [1, 2, 3, 0], protect=1, limit=2)
        self.assertIn("d0", with_guard)

    def test_no_guard_drops_demoted_heuristic_top(self):
        without_guard = self._rerank([10, 9, 8, 7], [1, 2, 3, 0], protect=0, limit=2)
        self.assertNotIn("d0", without_guard)


if __name__ == "__main__":
    unittest.main()
