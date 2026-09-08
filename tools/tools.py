"""Retrieval and web-search tool implementations exposed to agents."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

import json
import logging
import os
import re
from itertools import product

from config.allowed_keywords import normalize_keyword_synonyms
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
from ingestion.period_normalize import (
    canonical_period,
    canonical_value_type,
    query_section_total_key,
    section_total_key,
)
from tools.query_routing import (
    coverage_legs_for_fact,
    fact_matches_required_slots,
    fact_period_labels,
    fact_period_role,
    fact_sibling_group_key,
    fact_slot_score,
    parse_query_slots,
)
from vectorstore.llm_reranker import llm_rerank_enabled, llm_rerank_order


logger = logging.getLogger(__name__)


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)
# Distinctive figures: long VND amounts / registration & tax IDs (>=7 digits).
_FIGURE_RE = re.compile(r"\d[\d.,]*\d")
# How many cross-table lexical hits to fold in as a routing safety net.
_CROSS_TABLE_TOP_N = 25
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
def neural_rerank_enabled() -> bool:
    """Compatibility hook for the removed heavyweight experimental reranker."""
    return False


def neural_similarity(_query: str, _docs: list[str]) -> list[float]:
    return []


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
    distances = results.get("distances", [[]])
    similarities = results.get("similarities", [[]])
    docs = documents[0] if documents else []
    raw_metas = metadatas[0] if metadatas else []
    distance_values = distances[0] if distances else []
    similarity_values = similarities[0] if similarities else []
    metas = []
    for index, raw_meta in enumerate(raw_metas):
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        if (
            index < len(distance_values)
            and distance_values[index] is not None
        ):
            meta["distance"] = float(distance_values[index])
        if (
            index < len(similarity_values)
            and similarity_values[index] is not None
        ):
            meta["similarity_score"] = float(similarity_values[index])
        metas.append(meta)
    return list(docs), metas


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
    metas = [
        dict(meta) if isinstance(meta, dict) else {}
        for meta in (primary_metas or [])
    ]
    key_to_index = {
        _match_key(doc, meta): index
        for index, (doc, meta) in enumerate(zip(docs, metas))
    }

    for doc, meta in zip(extra_docs or [], extra_metas or []):
        key = _match_key(doc, meta)
        if key in key_to_index:
            existing = metas[key_to_index[key]]
            if isinstance(meta, dict):
                for field in (
                    "distance",
                    "similarity_score",
                    "retrieval_score",
                ):
                    value = meta.get(field)
                    if (
                        value not in ("", None)
                        and existing.get(field) in ("", None)
                    ):
                        existing[field] = value
            continue
        docs.append(doc)
        metas.append(dict(meta) if isinstance(meta, dict) else {})
        key_to_index[key] = len(docs) - 1

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


def _collection_generation(collection):
    generation = getattr(collection, "generation", "")
    if callable(generation):
        generation = generation()
    return str(generation or "").strip()


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

    query_slots = parse_query_slots(slot_query)
    score = fact_slot_score(query_slots, meta, doc)

    # Directional lending-income phrases are easily confused with borrowing
    # interest expense because both contain "lãi" and "vay".  Preserve the
    # semantic direction explicitly for deterministic factual retrieval.
    if "lãi cho vay" in slot_norm or "lai cho vay" in slot_norm:
        if "lãi cho vay" in item_norm or "lai cho vay" in item_norm:
            score += 120.0
        if "chi phí lãi vay" in item_norm or "chi phi lai vay" in item_norm:
            score -= 100.0

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
    # ("chi phí phải trả" for "chi phí xây dựng cơ bản dở dang"; an aggregate
    # liability schedule for a named counterparty). Score coverage of the full-question
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
    fact_meta = meta if isinstance(meta, dict) else {}
    if (
        getattr(query_slots, "policy_topic", "")
        and str(fact_meta.get("policy_topic", "") or "").strip()
        and fact_matches_required_slots(query_slots, fact_meta, doc)
    ):
        # Policy sections are numbered differently across filings.  Reward a
        # policy fact only when the query and fact agree through their typed
        # topic/entity/scope contract; never infer policy relevance from a note
        # number or a company-specific layout.
        score += 20.0

    # A bare "giá trị [tài sản]" (no explicit typed slot) means net book value —
    # retain this domain convention on top of the centralized typed-slot score.
    fact_value_type = (
        str(fact_meta.get("value_type", "") or "").strip()
        or canonical_value_type(item_name)
    )
    if not query_slots.value_type and ("giá trị" in slot_norm or "gia tri" in slot_norm):
        # Bare "giá trị [tài sản]" (no explicit value type) means net book value —
        # the balance-sheet line (value_type empty), not the nguyên giá / hao mòn
        # breakdown rows, whose richer "Tài sản … — Nguyên giá" subheading otherwise
        # outscores the net row on wording. Penalty must beat that wording edge.
        if fact_value_type in ("nguyên giá", "hao mòn"):
            score -= 50.0

    # Section-total alias: "tổng tài sản ngắn hạn" ↔ the BS line "A - TÀI SẢN NGẮN
    # HẠN" (and B-/TỔNG CỘNG …). Lift the real total above its component rows.
    q_section = (
        str(getattr(query_slots, "section_key", "") or "").strip()
        or query_section_total_key(slot_query)
    )
    fact_section = str(fact_meta.get("section_key", "") or "").strip()
    if q_section and (fact_section or section_total_key(item_name)) == q_section:
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
    notes: dự phòng / giá gốc / giá trị hợp lý of an investment.

    The balance sheet carries only the net total, so on these questions the gold
    per-entity row is absent from the BS-routed pool. H1 folds a note-table
    lexical query into the candidate pool.
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
# Exact metadata lookup is bounded independently from dense top-k.  It is used
# only when the question supplies typed slots, and never widens the final cut.
_STRUCTURED_SLOT_SCAN_LIMIT = 64
# A block is the smallest ingestion-owned schedule scope.  Sibling completion
# scans only that block and still returns at most the caller's existing limit.
_BLOCK_SIBLING_SCAN_LIMIT = 64

# List / superlative / per-entity phrasings whose answer needs EVERY row of a note
# schedule (rank/compare across projects, companies, loans…), not a top-K slice.
_SCHEDULE_MARKERS = (
    "nào", "liệt kê", "danh sách", "mỗi", "từng", "các khoản", "các công ty",
    "các đơn vị", "các dự án", "nhiều nhất", "lớn nhất", "cao nhất", "thấp nhất",
    "nhỏ nhất", "giảm nhiều", "tăng nhiều", "bao nhiêu khoản", "những",
)


def needs_full_schedule(query):
    """True only when the answer genuinely ranges over a closed schedule.

    Exact note-detail and two-slot comparison questions are served by structured
    matching plus requested siblings.  A shared ``note_ref`` alone must never
    widen retrieval to every row in that note.
    """
    norm = _normalize_text(str(query or ""))
    return any(m in norm for m in _SCHEDULE_MARKERS)


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
    except Exception as exc:
        logger.warning("note schedule retrieval failed for note_ref=%s: %s", ref, exc)
        return [], []


def _candidate_period(meta, doc=""):
    if isinstance(meta, dict):
        period = str(meta.get("period", "") or "").strip()
        if period:
            return period
        item_name = str(meta.get("item_name", "") or "")
    else:
        item_name = ""
    return canonical_period(f"{item_name} {doc}")


def _candidate_value_type(meta, doc=""):
    if isinstance(meta, dict):
        value_type = str(meta.get("value_type", "") or "").strip()
        if value_type:
            return value_type
        item_name = str(meta.get("item_name", "") or "")
    else:
        item_name = ""
    return canonical_value_type(f"{item_name} {doc}")


def _candidate_period_role(meta, doc=""):
    return fact_period_role(meta if isinstance(meta, dict) else {}, doc)


def _candidate_period_labels(meta, doc=""):
    return set(fact_period_labels(meta if isinstance(meta, dict) else {}, doc))


def _coverage_legs_for_fact(slots, meta, doc=""):
    """Compatibility wrapper for the shared retrieval/lifecycle classifier."""

    return set(coverage_legs_for_fact(slots, meta, doc))


def _needs_block_sibling_completion(slots):
    return bool(
        slots.period == "both"
        or slots.period_role == "both"
        or len(slots.period_labels) > 1
        or len(slots.value_type) > 1
        or (slots.coverage_template and slots.required_legs)
    )


def _block_sibling_docs(collection, anchor_meta):
    """Read one parser-owned schedule block, never an entire note reference."""

    meta = anchor_meta if isinstance(anchor_meta, dict) else {}
    block_id = str(meta.get("block_id", "") or "").strip()
    if not block_id or not callable(getattr(collection, "get", None)):
        return [], []
    try:
        result = collection.get(
            where={"block_id": block_id},
            include=["documents", "metadatas"],
        )
        docs, metas = _extract_flat_docs_and_metas(result)
    except Exception as exc:
        logger.warning("block sibling retrieval failed block_id=%s: %s", block_id, exc)
        return [], []

    company = str(meta.get("company", "") or "").strip()
    source = str(meta.get("source", "") or "").strip()
    kept = []
    for doc, sibling_meta in zip(docs, metas):
        sibling_meta = sibling_meta if isinstance(sibling_meta, dict) else {}
        if str(sibling_meta.get("block_id", "") or "").strip() != block_id:
            continue
        sibling_company = str(sibling_meta.get("company", "") or "").strip()
        if company and sibling_company and sibling_company != company:
            continue
        sibling_source = str(sibling_meta.get("source", "") or "").strip()
        if source and sibling_source and sibling_source != source:
            continue
        kept.append((doc, sibling_meta))
        if len(kept) >= _BLOCK_SIBLING_SCAN_LIMIT:
            break
    return (
        [doc for doc, _meta in kept],
        [sibling_meta for _doc, sibling_meta in kept],
    )


def _complete_required_block_siblings(collection, docs, metas, *, slots):
    if not docs or not _needs_block_sibling_completion(slots):
        return docs, metas
    anchor_index = next(
        (
            index
            for index, (doc, meta) in enumerate(zip(docs, metas))
            if str((meta or {}).get("block_id", "") or "").strip()
            and fact_matches_required_slots(slots, meta, doc)
        ),
        None,
    )
    if anchor_index is None:
        anchor_index = next(
            (
                index
                for index, meta in enumerate(metas)
                if str((meta or {}).get("block_id", "") or "").strip()
            ),
            None,
        )
    if anchor_index is None:
        return docs, metas
    sibling_docs, sibling_metas = _block_sibling_docs(
        collection, metas[anchor_index]
    )
    return _merge_docs_and_metas(
        docs,
        metas,
        sibling_docs,
        sibling_metas,
    )


def _inject_required_siblings(ranked, docs, metas, *, slots, limit):
    """Keep the exact logical-row sibling(s) required by typed query slots.

    The operation is bounded by ``limit``: it replaces the lowest ranked tail
    rather than widening the fact cut or loading an entire note schedule.
    """

    chosen = list(ranked[:limit])
    if not chosen:
        return chosen

    anchor = chosen[0]
    required = [anchor]
    closed_leg_candidates: dict[str, set[int]] = {}
    coverage_block_candidates: list[int] = []

    if slots.period == "both":
        anchor_key = fact_sibling_group_key(metas[anchor])
        present_periods = {
            _candidate_period(metas[index], docs[index])
            for index in chosen
            if fact_sibling_group_key(metas[index]) == anchor_key
        }
        for desired_period in ("cuối", "đầu"):
            if desired_period in present_periods:
                continue
            sibling = next(
                (
                    index
                    for index in ranked
                    if fact_sibling_group_key(metas[index]) == anchor_key
                    and _candidate_period(metas[index], docs[index]) == desired_period
                ),
                None,
            )
            if sibling is not None:
                required.append(sibling)
                present_periods.add(desired_period)

    if slots.period_role == "both":
        anchor_key = fact_sibling_group_key(metas[anchor])
        present_roles = {
            _candidate_period_role(metas[index], docs[index])
            for index in chosen
            if fact_sibling_group_key(metas[index]) == anchor_key
        }
        for desired_role in ("current", "previous"):
            if desired_role in present_roles:
                continue
            sibling = next(
                (
                    index
                    for index in ranked
                    if fact_sibling_group_key(metas[index]) == anchor_key
                    and _candidate_period_role(
                        metas[index], docs[index]
                    ) == desired_role
                ),
                None,
            )
            if sibling is not None:
                required.append(sibling)
                present_roles.add(desired_role)

    if len(slots.period_labels) > 1:
        anchor_key = fact_sibling_group_key(metas[anchor])
        present_labels = set()
        for index in chosen:
            if fact_sibling_group_key(metas[index]) == anchor_key:
                present_labels.update(
                    _candidate_period_labels(metas[index], docs[index])
                )
        for desired_label in slots.period_labels:
            if desired_label in present_labels:
                continue
            sibling = next(
                (
                    index
                    for index in ranked
                    if fact_sibling_group_key(metas[index]) == anchor_key
                    and desired_label
                    in _candidate_period_labels(metas[index], docs[index])
                ),
                None,
            )
            if sibling is not None:
                required.append(sibling)
                present_labels.update(
                    _candidate_period_labels(
                        metas[sibling], docs[sibling]
                    )
                )

    if len(slots.value_type) > 1:
        anchor_key = fact_sibling_group_key(metas[anchor], ignore_value_type=True)
        present_types = {
            _candidate_value_type(metas[index], docs[index])
            for index in chosen
            if fact_sibling_group_key(metas[index], ignore_value_type=True) == anchor_key
        }
        for desired_type in slots.value_type:
            if desired_type in present_types:
                continue
            sibling = next(
                (
                    index
                    for index in ranked
                    if fact_sibling_group_key(
                        metas[index], ignore_value_type=True
                    )
                    == anchor_key
                    and _candidate_value_type(metas[index], docs[index]) == desired_type
                ),
                None,
            )
            if sibling is not None:
                required.append(sibling)
                present_types.add(desired_type)

    anchor_block = str((metas[anchor] or {}).get("block_id", "") or "").strip()
    if slots.coverage_template and slots.required_legs and anchor_block:
        coverage_block_candidates = [
            index
            for index in ranked
            if str((metas[index] or {}).get("block_id", "") or "").strip()
            == anchor_block
        ]
        for desired_leg in slots.required_legs:
            leg_candidates = [
                index
                for index in coverage_block_candidates
                if desired_leg
                in _coverage_legs_for_fact(slots, metas[index], docs[index])
            ]
            # Component/category legs represent a closed set: retain all rows
            # available in this one block, while the normal result limit remains
            # the hard upper bound.
            if desired_leg in {"components_closed", "transaction_categories"}:
                required.extend(leg_candidates)
                closed_leg_candidates[desired_leg] = set(leg_candidates)
            elif leg_candidates:
                required.append(leg_candidates[0])

    required = list(dict.fromkeys(required))

    for sibling in required[1:]:
        if sibling in chosen:
            continue
        if len(chosen) < limit:
            chosen.append(sibling)
            continue
        replace_at = next(
            (
                index
                for index in range(len(chosen) - 1, 0, -1)
                if chosen[index] not in required
            ),
            None,
        )
        if replace_at is not None:
            chosen[replace_at] = sibling

    required_tail = [index for index in required[1:] if index in chosen]
    ordered = [anchor, *required_tail, *[
        index for index in chosen if index != anchor and index not in required_tail
    ]]

    # A component/category leg means the complete set from one parser-owned
    # block, not merely one convenient component.  Mark it complete only when
    # the bounded block scan itself was not truncated and every candidate from
    # that closed set survived the final result limit.  Requirement lifecycle
    # code consumes this marker; absence remains fail-closed.
    complete_closed_legs = [
        leg
        for leg, candidates in closed_leg_candidates.items()
        if (
            candidates
            and len(coverage_block_candidates) < _BLOCK_SIBLING_SCAN_LIMIT
            and candidates.issubset(set(ordered))
        )
    ]
    if complete_closed_legs:
        for index in ordered:
            if not isinstance(metas[index], dict):
                metas[index] = {}
            metas[index]["coverage_complete_legs"] = complete_closed_legs

    return ordered


def _rerank_matches(query, docs, metas, limit=5, intent=None):
    docs = list(docs or [])
    metas = [
        dict(meta) if isinstance(meta, dict) else {}
        for meta in (metas or [])
    ]
    if len(docs) != len(metas):
        raise ValueError(
            f"retrieval documents/metadatas length mismatch: {len(docs)} != {len(metas)}"
        )
    n = len(docs)

    # Deterministic heuristic scoring is the production baseline.  The former
    # PhoBERT blend was disabled by default, weak on financial line items, and
    # pulled a large dependency tree into every deployment.
    heuristic = []
    for doc, meta in zip(docs, metas):
        score = _item_match_score(query, meta, doc, intent=intent)
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

    # Structured candidates satisfying every explicit slot take precedence over
    # fuzzy near-neighbours.  If none is complete, retain the normal score order
    # so older/sparse facts still benefit from the retrieval fallback.
    query_slots = parse_query_slots(intent or query)
    exact_slot_matches = [
        index
        for index in ranked
        if fact_matches_required_slots(query_slots, metas[index], docs[index])
    ]
    if exact_slot_matches:
        exact_set = set(exact_slot_matches)
        ranked = exact_slot_matches + [index for index in ranked if index not in exact_set]

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

    chosen = _inject_required_siblings(
        ranked,
        docs,
        metas,
        slots=query_slots,
        limit=limit,
    )
    chosen_metas = []
    for index in chosen:
        meta = dict(metas[index])
        # Persist the deterministic production score.  The optional LLM blend
        # may affect ordering, but it is not a calibrated retrieval score and
        # therefore must not be exposed as one.
        meta["rerank_score"] = float(heuristic[index])
        chosen_metas.append(meta)
    return [docs[i] for i in chosen], chosen_metas


def _stamp_score_provenance(
    metas,
    *,
    score_query: str,
    score_intent: str,
    retrieval_origin: str,
):
    """Bind each persisted rerank score to the query and retrieval pass used."""

    stamped = []
    for raw_meta in metas or []:
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        if meta.get("rerank_score") not in ("", None):
            meta["score_query"] = str(score_query or "").strip()
            meta["score_intent"] = str(score_intent or "").strip()
            meta["retrieval_origin"] = str(retrieval_origin or "").strip()
        stamped.append(meta)
    return stamped


def _is_toc_row(doc: str) -> bool:
    """A mục lục (table-of-contents) row carries only a section name + page range
    (e.g. ``NỘI DUNG: BÁO CÁO KIỂM TOÁN ĐỘC LẬP | TRANG: 4 – 5``): it holds no
    fact, yet because its text repeats section headings it outranks the real
    front-section fact rows for heading-like queries ("số báo cáo kiểm toán"),
    starving the answer-bearing row of a retrieval slot. Both markers are
    required so related-party note columns ("Nội dung giao dịch", no ``TRANG:``)
    are not affected."""
    text = str(doc or "")
    return "NỘI DUNG:" in text and "TRANG:" in text


def _drop_toc_rows(docs: list[str], metas: list[dict]) -> tuple[list[str], list[dict]]:
    kept = [(doc, meta) for doc, meta in zip(docs, metas) if not _is_toc_row(doc)]
    if not kept or len(kept) == len(docs):
        return docs, metas
    return [doc for doc, _ in kept], [meta for _, meta in kept]


def _structured_slot_filters(slots, requested_table):
    """Build bounded exact metadata filters; never issue a heading-only scan."""

    periods = []
    if slots.period in {"cuối", "đầu"}:
        periods = [slots.period]
    elif slots.period == "both":
        periods = ["cuối", "đầu"]

    period_roles = []
    if slots.period_role in {"current", "previous"}:
        period_roles = [slots.period_role]
    elif slots.period_role == "both":
        period_roles = ["current", "previous"]

    value_types = list(slots.value_type)
    aggregation = (
        slots.aggregation
        if slots.aggregation in {"total", "component"}
        else ""
    )
    metric = str(getattr(slots, "metric", "") or "").strip()
    section_key = ""
    if requested_table == TABLE_BS:
        section_key = str(getattr(slots, "section_key", "") or "").strip()
    semantic_filters = {
        field: str(getattr(slots, field, "") or "").strip()
        for field in (
            "transaction_type",
            "movement_type",
        )
        if str(getattr(slots, field, "") or "").strip()
    }
    policy_topic = str(getattr(slots, "policy_topic", "") or "").strip()
    policy_topics = [policy_topic] if policy_topic else [""]
    if policy_topic in {
        "depreciation_period",
        "depreciation_method",
        "amortization_period",
        "amortization_method",
    }:
        # "Không khấu hao/phân bổ" is a valid terminal answer to a method or
        # useful-life question and must survive the exact payload prefilter.
        policy_topics.append("non_depreciation")
    if (
        not periods
        and not period_roles
        and not value_types
        and not aggregation
        and not section_key
        and not semantic_filters
        and not policy_topic
    ):
        return []
    # Metric/entity is checked exactly in Python after Qdrant applies the typed
    # payload filter. A canonical semantic enum is independently selective;
    # without either, a period-only/aggregation-only scan would still be broad.
    if (
        not slots.metric
        and not slots.entity
        and not semantic_filters
        and not policy_topic
    ):
        return []

    period_values = periods or [""]
    period_role_values = period_roles or [""]
    value_type_values = value_types or [""]
    filters = []
    for period, period_role, value_type, candidate_policy_topic in product(
        period_values,
        period_role_values,
        value_type_values,
        policy_topics,
    ):
        where = {"heading": requested_table}
        if period:
            where["period"] = period
        if period_role:
            where["period_role"] = period_role
        if value_type:
            where["value_type"] = value_type
        if aggregation and not section_key:
            where["aggregation_level"] = aggregation
        if section_key:
            where["section_key"] = section_key
        if candidate_policy_topic:
            where["policy_topic"] = candidate_policy_topic
        where.update(semantic_filters)
        filters.append(where)
    return filters


def _metadata_matches_where(meta, where):
    if not isinstance(meta, dict):
        return False
    return all(str(meta.get(key, "") or "") == str(value) for key, value in where.items())


def _structured_slot_candidates(collection, slots, requested_table):
    filters = _structured_slot_filters(slots, requested_table)
    if not filters or not callable(getattr(collection, "get", None)):
        return [], []

    docs = []
    metas = []
    for where in filters:
        result = collection.get(
            where=where,
            include=["documents", "metadatas"],
        )
        candidate_docs, candidate_metas = _extract_flat_docs_and_metas(result)
        for doc, meta in zip(candidate_docs, candidate_metas):
            if not _metadata_matches_where(meta, where):
                continue
            if not fact_matches_required_slots(slots, meta, doc):
                continue
            docs, metas = _merge_docs_and_metas(docs, metas, [doc], [meta])
            if len(docs) >= _STRUCTURED_SLOT_SCAN_LIMIT:
                return docs, metas
    return docs, metas


_GENERIC_STRUCTURED_METRICS = {
    "doanh thu",
    "chi phí",
    "lợi nhuận",
    "tài sản",
    "nợ",
    "vay",
    "tiền",
    "thuế",
    "dự phòng",
}


def _structured_metric_discriminant_satisfied(slots, metas):
    """Reject a structured early-return when its metric is only a broad prefix.

    Metadata probes are recall shortcuts, not proof that the user's complete
    semantic target was found.  A generic metric such as ``doanh thu`` may
    select dozens of schedules; unless another typed dimension disambiguates
    it, require at least one fact whose canonical metric is exactly that label
    and otherwise continue into dense/lexical retrieval.
    """

    metric = _normalize_text(getattr(slots, "metric", ""))
    if not metric or metric not in _GENERIC_STRUCTURED_METRICS:
        return True
    has_independent_discriminant = bool(
        getattr(slots, "entity", "")
        or getattr(slots, "scope_label", "")
        or getattr(slots, "counterparty", "")
        or getattr(slots, "transaction_type", "")
        or getattr(slots, "movement_type", "")
        or getattr(slots, "geography", "")
        or getattr(slots, "policy_topic", "")
        or getattr(slots, "value_type", ())
    )
    if has_independent_discriminant:
        return True
    for meta in metas:
        meta = meta if isinstance(meta, dict) else {}
        actual = _normalize_text(
            meta.get("metric_label", "")
            or str(meta.get("row_label", "") or "").split("|", 1)[0]
            or str(meta.get("item_name", "") or "").split("|", 1)[0]
        )
        if actual == metric:
            return True
    return False


def _structured_slots_satisfied(slots, docs, metas):
    if not docs:
        return False
    # A full ratio/share question spans two independently routed semantic
    # operands.  A single-table structured probe is not operand-aware, so it
    # must never return early after finding only the metric selected as the
    # query's headline slot.  The evidence plan executes each explicit leg
    # separately; direct callers fall through to the dense/lexical path.
    if slots.operation in {"ratio", "share"} and getattr(slots, "operands", ()):
        return False
    required_periods = {"cuối", "đầu"} if slots.period == "both" else set()
    required_period_roles = (
        {"current", "previous"}
        if slots.period_role == "both"
        else set()
    )
    required_period_labels = (
        set(slots.period_labels)
        if len(slots.period_labels) > 1
        else set()
    )
    required_types = set(slots.value_type) if len(slots.value_type) > 1 else set()
    if not _structured_metric_discriminant_satisfied(slots, metas):
        return False
    if (
        not required_periods
        and not required_period_roles
        and not required_period_labels
        and not required_types
    ):
        return True

    groups = {}
    ignore_value_type = bool(required_types)
    for doc, meta in zip(docs, metas):
        key = fact_sibling_group_key(meta, ignore_value_type=ignore_value_type)
        group = groups.setdefault(
            key,
            {
                "periods": set(),
                "period_roles": set(),
                "period_labels": set(),
                "types": set(),
                "pairs": set(),
            },
        )
        period = _candidate_period(meta, doc)
        period_role = _candidate_period_role(meta, doc)
        period_labels = _candidate_period_labels(meta, doc)
        value_type = _candidate_value_type(meta, doc)
        if period:
            group["periods"].add(period)
        if period_role:
            group["period_roles"].add(period_role)
        group["period_labels"].update(period_labels)
        if value_type:
            group["types"].add(value_type)
        if period or value_type:
            group["pairs"].add((period, value_type))

    for group in groups.values():
        if not required_periods.issubset(group["periods"]):
            continue
        if not required_period_roles.issubset(group["period_roles"]):
            continue
        if not required_period_labels.issubset(group["period_labels"]):
            continue
        if not required_types.issubset(group["types"]):
            continue
        if required_periods and required_types:
            expected = {
                (period, value_type)
                for period in required_periods
                for value_type in required_types
            }
            if not expected.issubset(group["pairs"]):
                continue
        return True
    return False


def get_related_info(
    query: str,
    table: str,
    collection,
    strict_table: bool = False,
    limit: int = 5,
    cross_table: bool = True,
    intent: str = "",
    structured_slots: bool = True,
):
    raw_query = str(query or "").strip()
    canonical_query = normalize_keyword_synonyms(raw_query) or raw_query
    raw_intent = str(intent or raw_query).strip()
    canonical_intent = normalize_keyword_synonyms(raw_intent) or raw_intent
    requested_table = normalize_table_heading(table)
    index_generation = _collection_generation(collection)
    # Value-lookup questions have many near-duplicate rows (same line item across
    # periods/value-types); widen the final cut so the right slot is not dropped.
    # Detect on the original question (`intent`) since the keyworder strips
    # "bao nhiêu/giá trị" from the keyword `query`.
    if _is_value_lookup_query(canonical_intent) and limit < _VALUE_LOOKUP_LIMIT:
        limit = _VALUE_LOOKUP_LIMIT
    # Typed semantic dimensions normally come from the user's/operand's surface
    # wording. Lexical synonym expansion can introduce words with a different
    # typed meaning (for example expanding ``doanh thu thuần`` to the primary
    # statement label adds ``bán hàng`` and fabricates transaction_type=sale).
    # A broad analytical intent, however, may name no typed metric at all while
    # the evidence route supplies a deterministic metric + period contract. In
    # that case bind structured retrieval to the raw scoped query; otherwise
    # every core profitability pair falls through to fallible dense top-k.
    intent_slots = parse_query_slots(raw_intent or raw_query)
    scoped_query_slots = parse_query_slots(raw_query)
    query_slots = (
        scoped_query_slots
        if scoped_query_slots.metric and not intent_slots.metric
        else intent_slots
    )

    structured_docs = []
    structured_metas = []
    if structured_slots:
        try:
            structured_docs, structured_metas = _structured_slot_candidates(
                collection,
                query_slots,
                requested_table,
            )
        except Exception as exc:
            logger.warning(
                "structured slot retrieval failed table=%s query=%r: %s",
                requested_table,
                canonical_query,
                exc,
            )
        structured_docs, structured_metas = _drop_toc_rows(
            structured_docs,
            structured_metas,
        )
    if structured_slots and _structured_slots_satisfied(
        query_slots,
        structured_docs,
        structured_metas,
    ):
        structured_docs, structured_metas = _rerank_matches(
            canonical_query,
            structured_docs,
            structured_metas,
            limit=limit,
            intent=canonical_intent,
        )
        structured_docs, structured_metas = _complete_required_block_siblings(
            collection,
            structured_docs,
            structured_metas,
            slots=query_slots,
        )
        structured_docs, structured_metas = _rerank_matches(
            canonical_query,
            structured_docs,
            structured_metas,
            limit=limit,
            intent=canonical_intent,
        )
        structured_metas = _stamp_score_provenance(
            structured_metas,
            score_query=canonical_query,
            score_intent=canonical_intent,
            retrieval_origin="structured_slots",
        )
        return {
            "context": "\n".join(structured_docs),
            "source": _join_sources(structured_metas),
            "documents": structured_docs,
            "metadatas": structured_metas,
            "canonical_query": canonical_query,
            "retrieval_mode": "structured_slots",
            "index_generation": index_generation,
        }

    primary_n_results = 100 if strict_table else 50
    results = collection.query(
        query_embeddings=[embed_query_text(canonical_query)],
        n_results=primary_n_results,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)
    docs, metas = _merge_docs_and_metas(
        structured_docs,
        structured_metas,
        docs,
        metas,
    )
    # Strict scope means no cross-table fallback, not "load the whole table".
    # Dense + lexical candidates remain bounded; exact note_ref schedules are
    # expanded separately below when the query explicitly requires a list.

    # Hybrid recall booster: fold in lexical (BM25) hits so facts the dense query
    # missed still reach the reranker. The cross-table pull is a routing safety
    # net — it catches facts that live outside the requested table because the
    # router picked the wrong default or because note schedules carry their own
    # ad-hoc headings (e.g. "18a. Vay ngắn hạn") unreachable by heading filter.
    # No extra collection .query calls; a no-op when the lexical index is empty.
    try:
        lexical = get_lexical_index(getattr(collection, "name", ""), collection)
        pairs = lexical.query(canonical_query, table=requested_table, top_n=primary_n_results)
        # Strict tables already merge their whole table into the candidate pool,
        # so the cross-table fallback only adds noise that can outrank the true
        # in-table fact (e.g. a generic-labelled report-section answer).
        if cross_table and not strict_table:
            pairs = pairs + lexical.query(canonical_query, table="", top_n=_CROSS_TABLE_TOP_N)
        # Intent fold: the keyworder often strips the discriminating tokens from
        # its query ("phải thu ngắn hạn KHÁC" -> "các khoản phải thu ngắn hạn",
        # "nguyên giá NHÀ CỬA VÀ VẬT KIẾN TRÚC" -> "tài sản cố định hữu hình"),
        # so when the full user intent carries extra signal, fold a BM25 query on
        # it across all tables. Subsumes the earlier dự-phòng/giá-gốc-gated note
        # fold; the slot-match reranker keeps the pool ranked.
        intent_text = canonical_intent
        if intent_text and _normalize_text(intent_text) != _normalize_text(canonical_query):
            pairs = pairs + lexical.query(intent_text, table="", top_n=_CROSS_TABLE_TOP_N)
        lex_docs = [doc for doc, _meta in pairs]
        lex_metas = [meta for _doc, meta in pairs]
        docs, metas = _merge_docs_and_metas(docs, metas, lex_docs, lex_metas)
    except Exception as exc:
        logger.warning(
            "lexical retrieval failed table=%s query=%r: %s",
            requested_table,
            canonical_query,
            exc,
        )

    # Mục lục rows are navigation noise that outranks real front-section facts;
    # drop them from the pool before ranking so the answer-bearing row survives.
    docs, metas = _drop_toc_rows(docs, metas)

    # note_ref linkage: on list / superlative / per-entity / note-detail questions,
    # pull the FULL note schedule referenced by any candidate (a primary line's
    # note_ref, or a note row already in the pool) so the whole per-entity set is
    # present. Fixes (B) primary→note detail (e.g. BS "Phải thu về cho vay" V.5 →
    # the per-borrower loan schedule) and (A) ranking questions that must compare
    # every row of a schedule. Gated so plain value lookups keep their tight pool.
    effective_limit = limit
    if _needs_schedule(canonical_intent):
        try:
            # Rank first, then read the winning schedule off the top rows: the
            # heuristic item/value_type match disambiguates look-alikes that raw
            # dense/lexical frequency confuses (e.g. "cho vay" [lending, V.5] vs
            # "vay" [borrowing, V.18a]). Then pull that schedule in full.
            probe_docs, probe_metas = _rerank_matches(
                canonical_query,
                docs,
                metas,
                limit=_NOTE_REF_PROBE_TOP,
                intent=canonical_intent,
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
        except Exception as exc:
            logger.warning("note_ref expansion failed query=%r: %s", canonical_query, exc)

    docs, metas = _rerank_matches(
        canonical_query,
        docs,
        metas,
        limit=effective_limit,
        intent=canonical_intent,
    )
    docs, metas = _complete_required_block_siblings(
        collection,
        docs,
        metas,
        slots=query_slots,
    )
    docs, metas = _rerank_matches(
        canonical_query,
        docs,
        metas,
        limit=effective_limit,
        intent=canonical_intent,
    )
    metas = _stamp_score_provenance(
        metas,
        score_query=canonical_query,
        score_intent=canonical_intent,
        retrieval_origin="hybrid",
    )

    context = "\n".join(docs)
    return {
        "context": context,
        "source": _join_sources(metas),
        "documents": docs,
        "metadatas": metas,
        "canonical_query": canonical_query,
        "index_generation": index_generation,
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
