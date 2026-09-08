"""Versioned domain catalog: how users name things vs how the system stores them.

These tables are data, not logic: they map the many Vietnamese spellings of a
report, an analysis axis or a period to the one canonical value the pipeline
uses. They live together and behind a version so a wording change is a
reviewable catalog edit with a fingerprint, not an inline literal in whichever
module happened to need it first.

Bump ``DOMAIN_CATALOG_VERSION`` whenever an entry changes meaning; the version
is part of the run fingerprint, so a resumed batch cannot mix two catalogs.
"""
# Code note: Config modules centralize constants used by routing, ingestion, and retrieval.

from __future__ import annotations

import json
from hashlib import sha256

DOMAIN_CATALOG_VERSION = "agentfinx-domain-catalog-v1"

# --- Report table aliases ---------------------------------------------------
TABLE_CANON = {
    "bảng cân đối kế toán": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "báo cáo kết quả hoạt động kinh doanh": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "báo cáo lưu chuyển tiền tệ": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "bcdkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "bcđkt": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "kqhđkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "kqhdkd": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "lctt": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
    "thuyết minh báo cáo tài chính": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh bao cao tai chinh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyết minh bctc": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh bctc": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyết minh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "thuyet minh": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
    "phần đầu báo cáo tài chính": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "phan dau bao cao tai chinh": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo của ban tổng giám đốc": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao cua ban tong giam doc": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo của ban giám đốc": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao cua ban giam doc": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo kiểm toán độc lập": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao kiem toan doc lap": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo kiểm toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao kiem toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo soát xét": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao soat xet": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "báo cáo soát xét báo cáo tài chính": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "bao cao soat xet bao cao tai chinh": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "thông tin công ty": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "thong tin cong ty": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "khái quát về công ty": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "khai quat ve cong ty": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "địa chỉ": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "dia chi": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "địa chỉ trụ sở chính": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "dia chi tru so chinh": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "trụ sở chính": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "tru so chinh": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "chuẩn mực kế toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "chuan muc ke toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "chuẩn mực kế toán áp dụng": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "chuan muc ke toan ap dung": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "chế độ kế toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "che do ke toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "công ty kiểm toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "cong ty kiem toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "hãng kiểm toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "hang kiem toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "đơn vị kiểm toán": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    "don vi kiem toan": "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
}

# --- Analysis axis aliases ------------------------------------------------
ANALYSIS_AXIS_ALIASES = {
    "profitability": "agent_profitability",
    "profit": "agent_profitability",
    "earnings": "agent_profitability",
    "agent_profitability": "agent_profitability",
    "liquidity": "agent_liquidity_solvency",
    "solvency": "agent_liquidity_solvency",
    "leverage": "agent_liquidity_solvency",
    "liquidity_solvency": "agent_liquidity_solvency",
    "agent_liquidity_solvency": "agent_liquidity_solvency",
    "cashflow": "agent_cashflow_analysis",
    "cash_flow": "agent_cashflow_analysis",
    "cashflow_analysis": "agent_cashflow_analysis",
    "cash_flow_analysis": "agent_cashflow_analysis",
    "cash_flow_quality": "agent_cashflow_analysis",
    "cashflow_quality": "agent_cashflow_analysis",
    "agent_cashflow_analysis": "agent_cashflow_analysis",
    "efficiency": "agent_efficiency",
    "capital_efficiency": "agent_efficiency",
    "operating_efficiency": "agent_efficiency",
    "asset_efficiency": "agent_efficiency",
    "agent_efficiency": "agent_efficiency",
}

# --- Period markers ---------------------------------------------------------
# ASCII-folded surface forms; the comparison side folds before matching.
CURRENT_PERIOD_MARKERS = (
    "nam nay",
    "nam hien tai",
    "ky nay",
    "hien tai",
    "cuoi ky",
    "cuoi nam",
)
PREVIOUS_PERIOD_MARKERS = (
    "nam truoc",
    "ky truoc",
    "truoc do",
    "dau ky",
    "dau nam",
)


def catalog_fingerprint() -> str:
    """Digest of the catalog contents, for the run identity."""

    payload = json.dumps(
        {
            "version": DOMAIN_CATALOG_VERSION,
            "table_canon": TABLE_CANON,
            "analysis_axis_aliases": ANALYSIS_AXIS_ALIASES,
            "current_period_markers": list(CURRENT_PERIOD_MARKERS),
            "previous_period_markers": list(PREVIOUS_PERIOD_MARKERS),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:16]
