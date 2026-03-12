from __future__ import annotations
from typing import Dict, Set

# Canonical table names (must match your system exactly)
TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
TABLE_CF = "BÁO CÁO LƯU CHUYỂN TIỀN TỆ"

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
}

# ---- Simple alias map (normalize common variants to canonical) ----
ALIASES: Dict[str, str] = {
    # BS
    "tiền và tương đương tiền": "tiền và các khoản tương đương tiền",
    "phải thu khách hàng ngắn hạn": "phải thu ngắn hạn của khách hàng",
    "chi phí qldn": "chi phí quản lý doanh nghiệp",
    "lnst": "lợi nhuận sau thuế thu nhập doanh nghiệp",  
    # IS
    "doanh thu thuần": "doanh thu thuần về bán hàng và cung cấp dịch vụ",
    "lợi nhuận sau thuế": "lợi nhuận sau thuế thu nhập doanh nghiệp",

    # CF
    "tiền cuối kỳ": "tiền và tương đương tiền cuối kỳ",
    "tiền đầu kỳ": "tiền và tương đương tiền đầu kỳ",
    "lctt hđkd": "lưu chuyển tiền thuần từ hoạt động kinh doanh",

}