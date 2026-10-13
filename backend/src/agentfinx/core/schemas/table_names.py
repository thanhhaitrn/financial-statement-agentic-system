"""Canonical financial-statement table names and heading normalization."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

import re


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
TABLE_CF = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
TABLE_NOTE = "THUYẾT MINH BÁO CÁO TÀI CHÍNH"
TABLE_REPORT_SECTION = "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH"

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
    TABLE_REPORT_SECTION: {
        "contains": [
            "phần đầu báo cáo tài chính",
            "phan dau bao cao tai chinh",
            "báo cáo của ban tổng giám đốc",
            "bao cao cua ban tong giam doc",
            "báo cáo của ban giám đốc",
            "bao cao cua ban giam doc",
            "báo cáo kiểm toán độc lập",
            "bao cao kiem toan doc lap",
            "báo cáo kiểm toán",
            "bao cao kiem toan",
            "báo cáo soát xét",
            "bao cao soat xet",
            "thông tin công ty",
            "thong tin cong ty",
            "khái quát về công ty",
            "khai quat ve cong ty",
            "địa chỉ trụ sở",
            "dia chi tru so",
            "trụ sở chính",
            "tru so chinh",
            "trụ sở hoạt động",
            "tru so hoat dong",
            "hoạt động kinh doanh chính",
            "hoat dong kinh doanh chinh",
            "giấy chứng nhận đăng ký doanh nghiệp",
            "giay chung nhan dang ky doanh nghiep",
            "chuẩn mực kế toán",
            "chuan muc ke toan",
            "chuẩn mực kế toán áp dụng",
            "chuan muc ke toan ap dung",
            "chế độ kế toán",
            "che do ke toan",
            "chế độ kế toán áp dụng",
            "che do ke toan ap dung",
            "tuyên bố tuân thủ chuẩn mực kế toán",
            "tuyen bo tuan thu chuan muc ke toan",
            "ý kiến kiểm toán",
            "y kien kiem toan",
            "kết luận của kiểm toán viên",
            "ket luan cua kiem toan vien",
            "kết luận soát xét",
            "ket luan soat xet",
            "vấn đề cần nhấn mạnh",
            "van de can nhan manh",
        ],
        "exact": {
            "mục lục",
            "muc luc",
            "kiểm toán viên",
            "kiem toan vien",
            "công ty kiểm toán",
            "cong ty kiem toan",
            "hãng kiểm toán",
            "hang kiem toan",
            "đơn vị kiểm toán",
            "don vi kiem toan",
            "địa chỉ",
            "dia chi",
            "trụ sở",
            "tru so",
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
