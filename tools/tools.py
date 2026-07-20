"""Retrieval and web-search tool implementations exposed to agents."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

import json
import os
import re

from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from vectorstore.qdrant_store import embed_query_text
from vectorstore.lexical_index import get_lexical_index
from vectorstore.neural_reranker import neural_rerank_enabled, neural_similarity
from ingestion.period_normalize import (
    canonical_period,
    canonical_value_type,
    is_comparison_query,
    query_value_types,
    section_total_key,
)
from vectorstore.llm_reranker import llm_rerank_enabled, llm_rerank_order


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)
# Distinctive figures: long VND amounts / registration & tax IDs (>=7 digits).
_FIGURE_RE = re.compile(r"\d[\d.,]*\d")
# How many cross-table lexical hits to fold in as a routing safety net.
_CROSS_TABLE_TOP_N = 25
# Blend weight for the optional neural reranker (cosine sim 0..1) into the
# heuristic score (range ~0..200). Kept modest so it only breaks near-ties.
_NEURAL_RERANK_WEIGHT = float(os.getenv("NEURAL_RERANK_WEIGHT", "40"))
# Scale on the -30 opposite-period penalty for comparison-phrased queries
# (0 = no penalty, 1 = legacy full penalty).
_PERIOD_PENALTY_COMPARISON = float(os.getenv("PERIOD_PENALTY_COMPARISON", "0.5"))
# How many top heuristic candidates to hand the optional LLM listwise reranker.
# Bounds prompt size/cost so it never scales with the full candidate pool.
_LLM_RERANK_CANDIDATES = int(os.getenv("LLM_RERANK_CANDIDATES", "20"))
# Blend weight for the LLM reranker: the rank-position score is normalized to
# [0,1), so ~50 reshuffles near-ties and prose while the +60 exact-figure
# heuristic still wins number questions (blend, not replace).
_LLM_RERANK_WEIGHT = float(os.getenv("LLM_RERANK_WEIGHT", "50"))
# Recall guard (union): when the LLM reranker runs, never let it evict the
# heuristic's top N picks from the final cut. Offline A/B showed the reranker
# demoting the exact "số cuối kỳ" balance-sheet row out of top-K on value lookups.
_LLM_RERANK_PROTECT = int(os.getenv("LLM_RERANK_PROTECT", "2"))
# Gate: skip the LLM reranker on direct value-lookup / ratio questions (the answer
# IS a figure the query does not cite, so the +60 heuristic boost can't protect the
# gold row). Analytical questions ("đánh giá/phân tích/...") keep the reranker —
# that is where it lifts precision.
_LLM_RERANK_GATE = os.getenv("LLM_RERANK_GATE_VALUE_LOOKUP", "1").strip().lower() in {"1", "true", "yes", "on"}
_VALUE_LOOKUP_RE = re.compile(
    r"bao nhiêu|tỷ lệ|tỷ trọng|phần trăm|current ratio|mức độ thay đổi|chênh lệch|số dư",
    re.IGNORECASE,
)
_ANALYTICAL_RE = re.compile(
    r"đánh giá|phân tích|dự báo|nhận xét|tầm quan trọng|ý nghĩa|tác động|ảnh hưởng|vai trò",
    re.IGNORECASE,
)
_ABBREV_REPLACEMENTS = {
    "tndn": "thu nhập doanh nghiệp",
    "tscd": "tài sản cố định",
    "tscđ": "tài sản cố định",
    "hdkd": "hoạt động kinh doanh",
    "hđkd": "hoạt động kinh doanh",
    "lctt": "lưu chuyển tiền tệ",
    "lnst": "lợi nhuận sau thuế",
    "qldn": "quản lý doanh nghiệp",
}
_GENERIC_TABLE_LABELS = {
    "tong",
    "tổng",
    "cong",
    "cộng",
    "tong cong",
    "tổng cộng",
}
_AGGREGATE_QUERY_TOKENS = {"tong", "tổng", "cong", "cộng", "total"}
# Closing-balance phrasing signals aggregate intent even without a "tổng/cộng"
# token (e.g. "Số dư ... cuối kỳ"). Matched as substrings on the normalized
# query so a group total is never buried under its component rows.
_CLOSING_BALANCE_MARKERS = (
    "số dư",
    "so du",
    "cuối kỳ",
    "cuoi ky",
    "cuối năm",
    "cuoi nam",
    "cuối quý",
    "cuoi quy",
    "tại thời điểm",
    "tai thoi diem",
)
_POLICY_QUERY_TOKENS = {
    "chinh",
    "chính",
    "sach",
    "sách",
    "phuong",
    "phương",
    "phap",
    "pháp",
    "hach",
    "hạch",
    "toan",
    "toán",
    "du",
    "dự",
    "phong",
    "phòng",
}


def _normalize_text(value):
    text = str(value or "").strip().lower()
    for short, expanded in _ABBREV_REPLACEMENTS.items():
        text = re.sub(rf"\b{re.escape(short)}\b", expanded, text)
    return _SPACE_RE.sub(" ", text)


def _text_tokens(value):
    return set(_TOKEN_RE.findall(_normalize_text(value)))


def _extract_docs_and_metas(results):
    documents = results.get("documents", [[]])
    metadatas = results.get("metadatas", [[]])
    docs = documents[0] if documents else []
    metas = metadatas[0] if metadatas else []
    return docs, metas


def _extract_flat_docs_and_metas(results):
    docs = results.get("documents", []) or []
    metas = results.get("metadatas", []) or []
    return docs, metas


def _match_key(doc, meta):
    if isinstance(meta, dict):
        stable_meta = {
            key: str(meta.get(key, "") or "")
            for key in (
                "heading",
                "item_code",
                "note_ref",
                "subheading",
                "item_name",
                "source",
                "raw_value",
                "normalized_value",
            )
        }
        return json.dumps(stable_meta, ensure_ascii=False, sort_keys=True)
    return str(doc)


def _merge_docs_and_metas(primary_docs, primary_metas, extra_docs, extra_metas):
    docs = list(primary_docs or [])
    metas = list(primary_metas or [])
    seen = {
        _match_key(doc, meta)
        for doc, meta in zip(docs, metas)
    }

    for doc, meta in zip(extra_docs or [], extra_metas or []):
        key = _match_key(doc, meta)
        if key in seen:
            continue
        docs.append(doc)
        metas.append(meta)
        seen.add(key)

    return docs, metas


def _join_sources(metas):
    sources = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        source = str(meta.get("source", "")).strip()
        if source and source not in sources:
            sources.append(source)
    return ", ".join(sources) if sources else ""


def _item_match_score(query, meta, doc, intent=None):
    item_name = ""
    item_code = ""
    if isinstance(meta, dict):
        item_name = str(meta.get("item_name", "") or "")
        item_code = str(meta.get("item_code", "") or "")
        subheading = str(meta.get("subheading", "") or "")
        if subheading:
            item_name = f"{item_name} {subheading}".strip()

    # Slot intent (period / value-type) reads the ORIGINAL question when given —
    # the keyworder strips "cuối kỳ"/"giá trị" from `query`, so slot-matching the
    # bare keyword would never fire. Wording/figure matching still uses `query`.
    slot_query = str(intent or query)
    slot_norm = _normalize_text(slot_query)
    query_norm = _normalize_text(query)
    item_norm = _normalize_text(item_name)
    row_label = item_name.split("|")[-1].strip() if "|" in item_name else ""
    row_norm = _normalize_text(row_label)
    query_tokens = _text_tokens(query)
    item_tokens = _text_tokens(item_name)
    row_tokens = _text_tokens(row_label)
    doc_tokens = _text_tokens(doc)

    score = 0.0

    # Favor exact item-name matches first; token overlap then rescues common
    # financial abbreviations and slightly different Vietnamese wording.
    if item_norm == query_norm:
        score += 100.0
    if item_norm.startswith(query_norm) and query_norm:
        score += 40.0
    if query_norm and query_norm in item_norm:
        score += 30.0
    if row_norm and query_norm and row_norm == query_norm:
        score += 80.0
    if row_norm and query_norm and query_norm in row_norm:
        score += 45.0

    if query_tokens and item_tokens:
        overlap = len(query_tokens & item_tokens)
        coverage = overlap / len(query_tokens)
        precision = overlap / len(item_tokens)
        score += coverage * 25.0
        score += precision * 10.0

    if query_tokens and row_tokens:
        row_overlap = len(query_tokens & row_tokens)
        score += (row_overlap / len(query_tokens)) * 30.0
        score += (row_overlap / max(len(row_tokens), 1)) * 15.0

    # The keyworder's query can be a wrong near-synonym of what the user asked
    # ("chi phí phải trả" for "chi phí xây dựng cơ bản dở dang"; note-16 totals
    # for "phải trả CTCP Tập đoàn Apec"). Score coverage of the full-question
    # tokens too, so the row matching the QUESTION can outrank rows that only
    # match the derived query; when the keyworder was right the two coincide
    # and nothing changes.
    if slot_norm != query_norm:
        intent_tokens = _text_tokens(slot_query)
        if intent_tokens and item_tokens:
            overlap = len(intent_tokens & item_tokens)
            score += (overlap / len(intent_tokens)) * 25.0
            score += (overlap / len(item_tokens)) * 10.0
        if intent_tokens and row_tokens:
            row_overlap = len(intent_tokens & row_tokens)
            score += (row_overlap / len(intent_tokens)) * 20.0

    if query_tokens and doc_tokens:
        overlap = len(query_tokens & doc_tokens)
        score += (overlap / len(query_tokens)) * 5.0
        # note_text facts (policies, disclosures) carry the answer in the prose,
        # not the item_name (e.g. "Mệnh giá cổ phiếu đang lưu hành: 10.000 VND"
        # under "19b. Cổ phiếu"). Reward query coverage in the doc for them so
        # prose answers are not buried under same-section table rows.
        if item_code == "note_text":
            score += (overlap / len(query_tokens)) * 35.0

    # When the query cites a distinctive figure (long VND amount / ID), reward
    # the fact whose value actually contains it — exact-number matches are where
    # dense embeddings are weakest.
    query_figures = {f for f in _FIGURE_RE.findall(str(query or "")) if len(re.sub(r"\D", "", f)) >= 7}
    if query_figures:
        doc_digits = re.sub(r"\D", " ", doc)
        if any(re.sub(r"\D", "", fig) in doc_digits.replace(" ", "") for fig in query_figures):
            score += 60.0

    if item_code == "note_table":
        score += 8.0

    if item_code == "note_table" and row_norm in _GENERIC_TABLE_LABELS:
        # Recall-first: a group total ("Cộng") is the answer to aggregate /
        # closing-balance questions, so boost it for those and never bury it
        # otherwise. A specific sub-component query still wins on its own exact
        # item/row match above; the total just stays in the candidate pool.
        query_is_aggregate = bool(query_tokens & _AGGREGATE_QUERY_TOKENS) or any(
            marker in query_norm for marker in _CLOSING_BALANCE_MARKERS
        )
        score += 14.0 if query_is_aggregate else 0.0
    elif (
        item_code == "note_text"
        and item_norm.startswith("thuyết minh 2.")
        and not (query_tokens & _POLICY_QUERY_TOKENS)
    ):
        score -= 10.0
    elif item_code == "note_text" and item_norm.startswith("thuyết minh 2."):
        score += 20.0

    # Slot disambiguation: when the query targets a specific period (cuối/đầu) or
    # value type (nguyên giá / giá trị còn lại / hao mòn), reward the row whose
    # slot matches and penalize the siblings that only differ by that slot — the
    # near-duplicates the figure heuristic cannot separate (the query cites no
    # figure). Prefer the metadata field; fall back to parsing item_name/subheading
    # so it works pre-reindex and for facts without the field.
    # The metadata fields are already canonical ("cuối"/"đầu", "nguyên giá"…); use
    # them directly. Only fall back to parsing item_name when the field is absent
    # (pre-reindex / non-table facts) — re-canonicalizing a bare "cuối" would fail
    # since canonical_period now requires a period unit.
    fact_meta = meta if isinstance(meta, dict) else {}
    q_period = canonical_period(slot_query)
    if q_period:
        fact_period = str(fact_meta.get("period", "") or "").strip() or canonical_period(item_name)
        if fact_period:
            if fact_period == q_period:
                score += 35.0
            else:
                # A single-period-phrased comparison ("không còn số dư CUỐI kỳ")
                # still needs the opposite-period row as the other leg of the
                # change — soften the penalty there (canonical_period already
                # returns "" when both periods are named).
                penalty_scale = (
                    _PERIOD_PENALTY_COMPARISON if is_comparison_query(slot_query) else 1.0
                )
                score -= 30.0 * penalty_scale

    # Only disambiguate value type when the query names exactly ONE — a question
    # comparing several ("thay đổi nguyên giá VÀ hao mòn") needs all of them.
    q_value_types = query_value_types(slot_query)
    fact_value_type = str(fact_meta.get("value_type", "") or "").strip() or canonical_value_type(item_name)
    if len(q_value_types) == 1:
        q_value_type = next(iter(q_value_types))
        if fact_value_type:
            if fact_value_type == q_value_type:
                score += 35.0
                # H2: reward rows matching BOTH the value type AND the asked line
                # item, so "Đầu tư vào Công ty con | Dự phòng" beats "Dự phòng
                # chứng khoán" / "Dự phòng phải thu" which share only the type.
                slot_tokens = _text_tokens(slot_query)
                if slot_tokens and item_tokens and len(slot_tokens & item_tokens) / len(slot_tokens) >= 0.4:
                    score += 25.0
            else:
                score -= 30.0
    elif not q_value_types and ("giá trị" in slot_norm or "gia tri" in slot_norm):
        # Bare "giá trị [tài sản]" (no explicit value type) means net book value —
        # the balance-sheet line (value_type empty), not the nguyên giá / hao mòn
        # breakdown rows, whose richer "Tài sản … — Nguyên giá" subheading otherwise
        # outscores the net row on wording. Penalty must beat that wording edge.
        if fact_value_type in ("nguyên giá", "hao mòn"):
            score -= 50.0

    # Section-total alias: "tổng tài sản ngắn hạn" ↔ the BS line "A - TÀI SẢN NGẮN
    # HẠN" (and B-/TỔNG CỘNG …). Lift the real total above its component rows.
    q_section = section_total_key(slot_query)
    if q_section and section_total_key(item_name) == q_section:
        score += 50.0

    return score


def _is_value_lookup_query(query) -> bool:
    """A direct figure/ratio lookup whose answer is a number the query never cites.

    On these the +60 exact-figure heuristic cannot fire (no figure in the query),
    so the LLM reranker is unconstrained and tends to demote the gold balance-sheet
    row. Analytical questions are excluded — the reranker helps those.
    """
    text = str(query or "")
    return bool(_VALUE_LOOKUP_RE.search(text)) and not _ANALYTICAL_RE.search(text)


def _is_note_detail_query(query):
    """True when the question asks for investment detail that lives ONLY in the
    notes (bảng 2a/2c): dự phòng / giá gốc / giá trị hợp lý of an investment.

    The balance sheet carries only the net total, so on these questions the gold
    row (e.g. note 2c "Đầu tư vào Công ty con | Dự phòng") is absent from the
    BS-routed pool. H1 folds a note-table lexical query into the candidate pool.
    Markers keep their diacritics because _normalize_text lowercases but does not
    strip Vietnamese accents.

    "giá trị còn lại" / "khấu hao trong năm" belong here for the same reason:
    per-asset-class rows exist only in the fixed-asset note schedule (V.9); the
    BS carries just nguyên giá + hao mòn lũy kế totals. Bare "nguyên giá" /
    "khấu hao" stay out — too common in simple value lookups.
    """
    norm = _normalize_text(str(query or ""))
    return any(
        marker in norm
        for marker in (
            "dự phòng", "giá gốc", "giá gôc", "hợp lý",
            "giá trị còn lại", "khấu hao trong năm",
        )
    )


# note_ref schedule expansion: how many top reranked rows to read the winning
# schedule off, and how many distinct schedules to pull in full (bounded).
_NOTE_REF_PROBE_TOP = 5
_NOTE_REF_EXPAND_MAX = 2
# Widened rerank cut for list/superlative questions so a whole note schedule (the
# largest per-entity ones run ~20 rows) survives instead of being sliced to top-K.
_SCHEDULE_LIMIT = int(os.getenv("SCHEDULE_LIMIT", "24"))

# List / superlative / per-entity phrasings whose answer needs EVERY row of a note
# schedule (rank/compare across projects, companies, loans…), not a top-K slice.
_SCHEDULE_MARKERS = (
    "nào", "liệt kê", "danh sách", "mỗi", "từng", "các khoản", "các công ty",
    "các đơn vị", "các dự án", "nhiều nhất", "lớn nhất", "cao nhất", "thấp nhất",
    "nhỏ nhất", "giảm nhiều", "tăng nhiều", "bao nhiêu khoản", "những",
)


def needs_full_schedule(query):
    """True when the question ranks/lists across a note schedule's per-entity rows,
    or asks for note-only investment detail — the cases that need the full schedule
    (pulled via note_ref, and kept whole through the evidence-pack fact caps)
    rather than a similarity top-K."""
    norm = _normalize_text(str(query or ""))
    return _is_note_detail_query(query) or any(m in norm for m in _SCHEDULE_MARKERS)


# Internal alias kept for the retrieval call sites below.
_needs_schedule = needs_full_schedule


def _note_schedule_docs(collection, note_ref):
    """All note rows sharing a 'V.<n>' note_ref — i.e. one full note schedule."""
    ref = str(note_ref or "").strip()
    if not ref:
        return [], []
    try:
        res = collection.get(
            where={"heading": TABLE_NOTE, "note_ref": ref},
            include=["documents", "metadatas"],
        )
        return _extract_flat_docs_and_metas(res)
    except Exception:
        return [], []


def _rerank_matches(query, docs, metas, limit=5, intent=None):
    docs = list(docs or [])
    metas = list(metas or [])
    n = len(docs)

    # Heuristic (+ optional PhoBERT neural blend, flag-gated/off by default). Neural
    # sims are weak/clustered on VN line-items, so they only nudge near-ties; the
    # heuristic still dominates exact item/figure matches. Kept separate from the
    # blended score so the union recall guard below can reference it.
    neural = neural_similarity(query, docs) if neural_rerank_enabled() else []
    heuristic = []
    for idx, (doc, meta) in enumerate(zip(docs, metas)):
        score = _item_match_score(query, meta, doc, intent=intent)
        if neural and idx < len(neural):
            score += _NEURAL_RERANK_WEIGHT * float(neural[idx])
        heuristic.append(score)

    scores = list(heuristic)

    # Optional LLM listwise rerank (flag-gated, off by default). Hand the top-K
    # heuristic candidates to the chat model and blend its relevance ordering back
    # in. Gated off for value-lookup questions (see _is_value_lookup_query), bounded
    # by _LLM_RERANK_CANDIDATES, and a no-op when disabled or on error.
    llm_ran = False
    if (
        llm_rerank_enabled()
        and n > 1
        and not (_LLM_RERANK_GATE and _is_value_lookup_query(intent or query))
    ):
        order = sorted(range(n), key=lambda i: (-heuristic[i], i))[:_LLM_RERANK_CANDIDATES]
        positions = llm_rerank_order(query, [docs[i] for i in order])
        if positions:
            llm_ran = True
            k = len(order)
            for rank, local_idx in enumerate(positions):
                if 0 <= local_idx < k:
                    scores[order[local_idx]] += _LLM_RERANK_WEIGHT * (k - rank) / k

    ranked = sorted(range(n), key=lambda i: (-scores[i], i))

    # Union recall guard: when the reranker ran, force the heuristic's top picks
    # into the final cut even if the reranker demoted them, taking the reserved
    # slots from the lowest blended docs. Protects exact-figure recall.
    if llm_ran and _LLM_RERANK_PROTECT > 0 and n > limit:
        protected = sorted(range(n), key=lambda i: (-heuristic[i], i))[:_LLM_RERANK_PROTECT]
        top = ranked[:limit]
        missing = [i for i in protected if i not in top]
        if missing:
            kept = top[: max(0, limit - len(missing))]
            forced = kept + [i for i in missing if i not in kept]
            ranked = forced + [i for i in ranked if i not in forced]

    chosen = ranked[:limit]
    return [docs[i] for i in chosen], [metas[i] for i in chosen]


def get_related_info(query: str, table: str, collection, strict_table: bool = False, limit: int = 5, cross_table: bool = True, intent: str = ""):
    requested_table = normalize_table_heading(table)
    # Value-lookup questions have many near-duplicate rows (same line item across
    # periods/value-types); widen the final cut so the right slot is not dropped.
    # Detect on the original question (`intent`) since the keyworder strips
    # "bao nhiêu/giá trị" from the keyword `query`.
    if _is_value_lookup_query(intent or query) and limit < _VALUE_LOOKUP_LIMIT:
        limit = _VALUE_LOOKUP_LIMIT
    primary_n_results = 100 if strict_table else 50
    results = collection.query(
        query_embeddings=[embed_query_text(query)],
        n_results=primary_n_results,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)
    if strict_table:
        try:
            all_results = collection.get(
                where={"heading": requested_table},
                include=["documents", "metadatas"],
            )
            all_docs, all_metas = _extract_flat_docs_and_metas(all_results)
            docs, metas = _merge_docs_and_metas(docs, metas, all_docs, all_metas)
        except Exception:
            pass

    # Hybrid recall booster: fold in lexical (BM25) hits so facts the dense query
    # missed still reach the reranker. The cross-table pull is a routing safety
    # net — it catches facts that live outside the requested table because the
    # router picked the wrong default or because note schedules carry their own
    # ad-hoc headings (e.g. "18a. Vay ngắn hạn") unreachable by heading filter.
    # No extra collection .query calls; a no-op when the lexical index is empty.
    try:
        lexical = get_lexical_index(getattr(collection, "name", ""), collection)
        pairs = lexical.query(query, table=requested_table, top_n=primary_n_results)
        # Strict tables already merge their whole table into the candidate pool,
        # so the cross-table fallback only adds noise that can outrank the true
        # in-table fact (e.g. a generic-labelled report-section answer).
        if cross_table and not strict_table:
            pairs = pairs + lexical.query(query, table="", top_n=_CROSS_TABLE_TOP_N)
        # Intent fold: the keyworder often strips the discriminating tokens from
        # its query ("phải thu ngắn hạn KHÁC" -> "các khoản phải thu ngắn hạn",
        # "nguyên giá NHÀ CỬA VÀ VẬT KIẾN TRÚC" -> "tài sản cố định hữu hình"),
        # so when the full user intent carries extra signal, fold a BM25 query on
        # it across all tables. Subsumes the earlier dự-phòng/giá-gốc-gated note
        # fold; the slot-match reranker keeps the pool ranked.
        intent_text = str(intent or "").strip()
        if intent_text and _normalize_text(intent_text) != _normalize_text(query):
            pairs = pairs + lexical.query(intent_text, table="", top_n=_CROSS_TABLE_TOP_N)
        lex_docs = [doc for doc, _meta in pairs]
        lex_metas = [meta for _doc, meta in pairs]
        docs, metas = _merge_docs_and_metas(docs, metas, lex_docs, lex_metas)
    except Exception:
        pass

    # note_ref linkage: on list / superlative / per-entity / note-detail questions,
    # pull the FULL note schedule referenced by any candidate (a primary line's
    # note_ref, or a note row already in the pool) so the whole per-entity set is
    # present. Fixes (B) primary→note detail (e.g. BS "Phải thu về cho vay" V.5 →
    # the per-borrower loan schedule) and (A) ranking questions that must compare
    # every row of a schedule. Gated so plain value lookups keep their tight pool.
    effective_limit = limit
    if _needs_schedule(intent or query):
        try:
            # Rank first, then read the winning schedule off the top rows: the
            # heuristic item/value_type match disambiguates look-alikes that raw
            # dense/lexical frequency confuses (e.g. "cho vay" [lending, V.5] vs
            # "vay" [borrowing, V.18a]). Then pull that schedule in full.
            probe_docs, probe_metas = _rerank_matches(
                query, docs, metas, limit=_NOTE_REF_PROBE_TOP, intent=intent
            )
            counts = {}
            for m in probe_metas:
                r = str(m.get("note_ref", "")).strip() if isinstance(m, dict) else ""
                if r:
                    counts[r] = counts.get(r, 0) + 1
            refs = sorted(counts, key=lambda r: counts[r], reverse=True)
            for r in refs[:_NOTE_REF_EXPAND_MAX]:
                s_docs, s_metas = _note_schedule_docs(collection, r)
                if s_docs:
                    docs, metas = _merge_docs_and_metas(docs, metas, s_docs, s_metas)
                    # A ranking/list answer must see every per-entity row of the
                    # schedule, so widen the rerank cut past the caller's default.
                    effective_limit = max(effective_limit, _SCHEDULE_LIMIT)
        except Exception:
            pass

    docs, metas = _rerank_matches(query, docs, metas, limit=effective_limit, intent=intent)

    context = "\n".join(docs)
    return {
        "context": context,
        "source": _join_sources(metas),
        "documents": docs,
        "metadatas": metas,
    }


# Scoped tools retrieve facts with a deliberately wide cut: with the cross-table
# fallback the answer is reliably in the candidate pool, and a wider cut lets it
# survive the rerank when the router picked the wrong statement (recall
# 0.58 -> 0.84 offline). Recall-first ("thà thừa hơn thiếu"): we keep the total,
# the primary line and the component rows together rather than trimming, accepting
# lower context precision to avoid dropping the answer-bearing row.
_SCOPED_LIMIT = 12
# Wider cut for value-lookup questions: same line item repeats across periods /
# value-types, so a tight cut can drop the exact slot asked for.
_VALUE_LOOKUP_LIMIT = int(os.getenv("VALUE_LOOKUP_LIMIT", "20"))


def get_balance_sheet_info(query: str, collection, table: str = "", intent: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_BS, collection=collection, limit=_SCOPED_LIMIT, intent=intent)


def get_income_statement_info(query: str, collection, table: str = "", intent: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_IS, collection=collection, limit=_SCOPED_LIMIT, intent=intent)


def get_cashflow_info(query: str, collection, table: str = "", intent: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_CF, collection=collection, limit=_SCOPED_LIMIT, intent=intent)


def get_note_info(query: str, collection, table: str = "", intent: str = "", **_kwargs):
    # Strict tables already pull their whole table into the candidate pool, so the
    # cross-table fallback only adds noise that can outrank the true in-table fact.
    return get_related_info(query=query, table=TABLE_NOTE, collection=collection, strict_table=True, limit=_SCOPED_LIMIT, cross_table=False, intent=intent)


def get_report_section_info(query: str, collection, table: str = "", intent: str = "", **_kwargs):
    return get_related_info(
        query=query,
        table=TABLE_REPORT_SECTION,
        collection=collection,
        strict_table=True,
        limit=8,
        cross_table=False,
        intent=intent,
    )


def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}
