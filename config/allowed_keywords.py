"""Allowed financial statement keywords used for safe routing."""
# Code note: Config modules centralize constants used by routing, ingestion, and retrieval.

from __future__ import annotations
import json
from typing import Dict, Iterable, Set

# Canonical table names (must match your system exactly)
TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
TABLE_CF = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"
TABLE_NOTE = "THUYẾT MINH BÁO CÁO TÀI CHÍNH"
TABLE_REPORT_SECTION = "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH"

# ---- Allowed keywords (canonical Vietnamese line-items) ----
ALLOWED_KEYWORDS: Dict[str, Set[str]] = {
    TABLE_BS: {
        # Balance Sheet - core
        "tài sản ngắn hạn",
        "tài sản dài hạn",
        "tổng cộng tài sản",
        "tiền và các khoản tương đương tiền",
        "tiền",
        "các khoản tương đương tiền",
        "các khoản phải thu ngắn hạn",
        "phải thu ngắn hạn của khách hàng",
        "phải thu ngắn hạn khác",
        "hàng tồn kho",
        "đầu tư tài chính ngắn hạn",
        "đầu tư tài chính dài hạn",
        "tài sản cố định",
        "tài sản cố định hữu hình",
        "tài sản cố định vô hình",
        "bất động sản đầu tư",
        "chi phí trả trước ngắn hạn",
        "chi phí trả trước dài hạn",

        "nợ phải trả",
        "nợ ngắn hạn",
        "nợ dài hạn",
        "các khoản phải trả ngắn hạn",
        "vay và nợ thuê tài chính ngắn hạn",
        "vay và nợ thuê tài chính dài hạn",
        "phải trả người bán ngắn hạn",
        "phải trả ngắn hạn khác",
        "thuế và các khoản phải nộp nhà nước",
        "chi phí phải trả ngắn hạn",
        "chi phí phải trả dài hạn",
        "người mua trả tiền trước ngắn hạn",
        "người mua trả tiền trước dài hạn",

        "vốn chủ sở hữu",
        "vốn góp của chủ sở hữu",
        "lợi nhuận sau thuế chưa phân phối",
        "tổng cộng nguồn vốn",
    },

    TABLE_IS: {
        # Income Statement - core
        "doanh thu bán hàng và cung cấp dịch vụ",
        "các khoản giảm trừ doanh thu",
        "doanh thu thuần về bán hàng và cung cấp dịch vụ",
        "giá vốn hàng bán",
        "lợi nhuận gộp về bán hàng và cung cấp dịch vụ",
        "doanh thu hoạt động tài chính",
        "chi phí tài chính",
        "chi phí lãi vay",
        "chi phí bán hàng",
        "chi phí quản lý doanh nghiệp",
        "lợi nhuận thuần từ hoạt động kinh doanh",
        "thu nhập khác",
        "chi phí khác",
        "lợi nhuận khác",
        "tổng lợi nhuận kế toán trước thuế",
        "chi phí thuế tndn hiện hành",
        "chi phí thuế tndn hoãn lại",
        "lợi nhuận sau thuế thu nhập doanh nghiệp",
        "lãi cơ bản trên cổ phiếu",
        "lãi suy giảm trên cổ phiếu",
    },

    TABLE_CF: {
        # Cash Flow - core (direct + common)
        "tiền thu từ bán hàng, cung cấp dịch vụ và doanh thu khác",
        "tiền chi trả cho người cung cấp hàng hóa và dịch vụ",
        "tiền chi trả cho người lao động",
        "tiền lãi vay đã trả",
        "thuế thu nhập doanh nghiệp đã nộp",
        "tiền thu khác từ hoạt động kinh doanh",
        "tiền chi khác cho hoạt động kinh doanh",
        "lưu chuyển tiền thuần từ hoạt động kinh doanh",

        "tiền chi để mua sắm, xây dựng tscđ và các tài sản dài hạn khác",
        "tiền thu từ thanh lý, nhượng bán tscđ và các tài sản dài hạn khác",
        "tiền chi cho vay, mua các công cụ nợ của đơn vị khác",
        "tiền thu hồi cho vay, bán lại các công cụ nợ của đơn vị khác",
        "tiền chi đầu tư góp vốn vào đơn vị khác",
        "tiền thu hồi đầu tư góp vốn vào đơn vị khác",
        "tiền thu lãi cho vay, cổ tức và lợi nhuận được chia",
        "lưu chuyển tiền thuần từ hoạt động đầu tư",

        "tiền thu từ phát hành cổ phiếu, nhận vốn góp của chủ sở hữu",
        "tiền thu từ đi vay",
        "tiền trả nợ gốc vay",
        "cổ tức, lợi nhuận đã trả cho chủ sở hữu",
        "lưu chuyển tiền thuần từ hoạt động tài chính",

        "lưu chuyển tiền thuần trong kỳ",
        "tiền và tương đương tiền đầu kỳ",
        "tiền và tương đương tiền cuối kỳ",
        "ảnh hưởng của thay đổi tỷ giá hối đoái quy đổi ngoại tệ",
    },

    TABLE_NOTE: {
        # Notes to financial statements - common disclosure topics
        "chính sách kế toán",
        "cơ sở lập báo cáo tài chính",
        "đơn vị tiền tệ sử dụng trong kế toán",
        "ước tính kế toán",
        "tiền và các khoản tương đương tiền",
        "các khoản phải thu",
        "hàng tồn kho",
        "tài sản cố định hữu hình",
        "tài sản cố định vô hình",
        "bất động sản đầu tư",
        "chi phí trả trước",
        "đầu tư tài chính",
        "vay và nợ thuê tài chính",
        "phải trả người bán",
        "thuế và các khoản phải nộp nhà nước",
        "vốn chủ sở hữu",
        "doanh thu bán hàng và cung cấp dịch vụ",
        "giá vốn hàng bán",
        "chi phí tài chính",
        "chi phí bán hàng",
        "chi phí quản lý doanh nghiệp",
        "thuế thu nhập doanh nghiệp",
        "lãi cơ bản trên cổ phiếu",
        "giao dịch với các bên liên quan",
        "cam kết và nghĩa vụ tiềm tàng",
        "công cụ tài chính",
        "quản lý rủi ro tài chính",
        "sự kiện sau ngày kết thúc kỳ kế toán",
    },

    TABLE_REPORT_SECTION: {
        # Front sections before the primary financial statements
        "mục lục",
        "thông tin công ty",
        "khái quát về công ty",
        "địa chỉ công ty",
        "địa chỉ trụ sở chính",
        "trụ sở chính",
        "trụ sở hoạt động",
        "hoạt động kinh doanh chính",
        "giấy chứng nhận đăng ký doanh nghiệp",
        "chuẩn mực kế toán",
        "chuẩn mực kế toán áp dụng",
        "chế độ kế toán",
        "chế độ kế toán áp dụng",
        "tuyên bố tuân thủ chuẩn mực kế toán",
        "báo cáo của ban tổng giám đốc",
        "báo cáo của ban giám đốc",
        "hội đồng quản trị",
        "ban điều hành",
        "ban điều hành quản lý",
        "ban tổng giám đốc",
        "ban giám đốc",
        "ban kiểm soát",
        "kế toán trưởng",
        "người đại diện theo pháp luật",
        "kiểm toán viên",
        "đơn vị kiểm toán",
        "công ty kiểm toán",
        "hãng kiểm toán",
        "công ty thực hiện kiểm toán",
        "đơn vị thực hiện kiểm toán",
        "công ty thực hiện kế toán kiểm toán",
        "báo cáo kiểm toán độc lập",
        "báo cáo soát xét",
        "trách nhiệm của ban tổng giám đốc",
        "trách nhiệm của ban giám đốc",
        "trách nhiệm của kiểm toán viên",
        "trách nhiệm của kiểm toán viên hành nghề",
        "cơ sở đưa ra ý kiến kiểm toán",
        "cơ sở đưa ra kết luận soát xét",
        "ý kiến của kiểm toán viên",
        "ý kiến kiểm toán",
        "kết luận của kiểm toán viên",
        "kết luận soát xét",
        "vấn đề cần nhấn mạnh",
        "hoạt động liên tục",
        "nghi ngờ đáng kể về khả năng hoạt động liên tục",
        "người ký báo cáo tài chính",
        "ngày ký báo cáo tài chính",
        "ngày lập báo cáo",
    },
}

def _selected_allowed_keyword_tables(selected_tables: Iterable[str] | None = None) -> list[str]:
    if selected_tables is None:
        return list(ALLOWED_KEYWORDS.keys())

    normalized = []
    seen = set()
    for table in selected_tables:
        text = str(table or "").strip()
        if not text or text in seen or text not in ALLOWED_KEYWORDS:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def build_allowed_keywords_payload(selected_tables: Iterable[str] | None = None) -> str:
    allowed = {
        table: sorted(ALLOWED_KEYWORDS.get(table, set()))
        for table in _selected_allowed_keyword_tables(selected_tables)
    }
    return json.dumps(allowed, ensure_ascii=False)
