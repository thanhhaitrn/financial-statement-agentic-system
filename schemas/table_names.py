import re


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
TABLE_CF = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"

_SPACE_RE = re.compile(r"\s+")

_TABLE_PATTERNS = {
    TABLE_BS: [
        "bảng cân đối kế toán",
        "bcđkt",
        "bcdkt",
    ],
    TABLE_IS: [
        "báo cáo kết quả hoạt động kinh doanh",
        "kết quả hoạt động kinh doanh",
        "kqhđkd",
        "kqhdkd",
    ],
    TABLE_CF: [
        "báo cáo lưu chuyển tiền tệ",
        "lưu chuyển tiền tệ",
        "lctt",
    ],
}


def normalize_table_heading(value: str) -> str:
    text = _SPACE_RE.sub(" ", str(value or "").strip().lower())
    if not text:
        return ""

    for canonical_name, patterns in _TABLE_PATTERNS.items():
        if any(pattern in text for pattern in patterns):
            return canonical_name

    return str(value or "").strip()
