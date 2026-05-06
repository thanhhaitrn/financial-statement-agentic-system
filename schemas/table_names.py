"""Canonical financial-statement table names and heading normalization."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

import re


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
TABLE_CF = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
TABLE_NOTE = "THUYẾT MINH BÁO CÁO TÀI CHÍNH"

_SPACE_RE = re.compile(r"\s+")

_TABLE_PATTERNS = {
    TABLE_BS: {
        "contains": [
            "bảng cân đối kế toán",
            "báo cáo tình hình tài chính",
            "bcđkt",
            "bcdkt",
        ],
        "exact": {
            "tài sản",
            "nguồn vốn",
        },
    },
    TABLE_IS: {
        "contains": [
            "báo cáo kết quả hoạt động kinh doanh",
            "kết quả hoạt động kinh doanh",
            "kqhđkd",
            "kqhdkd",
        ],
        "exact": set(),
    },
    TABLE_CF: {
        "contains": [
            "báo cáo lưu chuyển tiền tệ",
            "lưu chuyển tiền tệ",
            "lctt",
        ],
        "exact": set(),
    },
    TABLE_NOTE: {
        "contains": [
            "thuyết minh báo cáo tài chính",
            "thuyet minh bao cao tai chinh",
            "thuyết minh bctc",
            "thuyet minh bctc",
            "bản thuyết minh",
            "ban thuyet minh",
            "các thuyết minh",
            "cac thuyet minh",
            "thông tin thuyết minh",
            "thong tin thuyet minh",
        ],
        "exact": {
            "thuyết minh",
            "thuyet minh",
        },
    },
}


def normalize_table_heading(value: str) -> str:
    text = _SPACE_RE.sub(" ", str(value or "").strip().lower())
    if not text:
        return ""

    for canonical_name, matcher in _TABLE_PATTERNS.items():
        exact_aliases = matcher.get("exact", set())
        contains_aliases = matcher.get("contains", [])
        if text in exact_aliases:
            return canonical_name
        if any(pattern in text for pattern in contains_aliases):
            return canonical_name

    return str(value or "").strip()
