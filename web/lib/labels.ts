// User-facing Vietnamese text for backend status codes.

export const PROCESSING_TEXT: Record<string, string> = {
  queued: "Đang chờ tới lượt xử lý",
  downloading: "Đang tải báo cáo",
  parsing: "Đang trích xuất nội dung",
  validating: "Đang kiểm tra chất lượng trích xuất",
  building: "Đang dựng chỉ mục tìm kiếm",
  ready: "Sẵn sàng để hỏi",
  failed: "Không xử lý được",
};

export const BILLING_TEXT: Record<string, string> = {
  not_submitted: "Không phát sinh chi phí trích xuất đám mây",
  pending: "Đang chờ nhà cung cấp xác nhận",
  confirmed: "Đã xác nhận",
  estimated_legacy: "Ước tính (dữ liệu cũ)",
};

export const SCOPE_TEXT: Record<string, string> = {
  consolidated: "Hợp nhất",
  separate: "Riêng/công ty mẹ",
  unknown: "Phạm vi chưa rõ",
};

export const AUDIT_TEXT: Record<string, string> = {
  audited: "Đã kiểm toán",
  reviewed: "Đã soát xét",
  unaudited: "Chưa kiểm toán",
  unknown: "Kiểm toán chưa rõ",
};

export const VERIFICATION_TEXT: Record<string, string> = {
  missing_structured_period: "CafeF không ghi kỳ báo cáo có cấu trúc cho tài liệu này.",
  inconsistent_structured_period: "Thông tin kỳ báo cáo CafeF ghi không nhất quán.",
  title_conflicts_year: "Năm trong tiêu đề khác với năm CafeF ghi nhận.",
  title_conflicts_period: "Kỳ trong tiêu đề khác với kỳ CafeF ghi nhận.",
  half_year_without_structured_code: "Tiêu đề ghi bán niên nhưng CafeF không có mã kỳ bán niên.",
};

export const WARNING_TEXT: Record<string, string> = {
  catalog_stale: "Danh mục mã CK đã cũ hơn 24 giờ.",
  catalog_unavailable: "Danh mục mã CK chưa sẵn sàng; tên công ty và sàn có thể thiếu.",
  ticker_not_in_catalog: "Mã này không có trong danh mục CafeF hiện tại.",
  results_truncated: "Chỉ hiển thị 30 kết quả mới nhất; hãy lọc thêm theo năm hoặc kỳ.",
};

export function clarificationText(reason: string, subject: string): string {
  switch (reason) {
    case "ambiguous":
      return `Có nhiều doanh nghiệp khớp “${subject}”. Hãy chọn một:`;
    case "not_found":
      return `Không tìm thấy “${subject}” trong danh mục CafeF. Hãy nhập mã CK.`;
    case "catalog_unavailable":
      return "Danh mục mã CK chưa sẵn sàng. Hãy nhập mã CK (VD: VNM).";
    case "needs_input":
      return "Hãy cho biết mã CK hoặc tên doanh nghiệp cần tìm.";
    case "confirm_ticker":
      return `“${subject}” không khớp trực tiếp mã nào. Có phải bạn muốn tìm:`;
    case "ambiguous_period":
      return "Kỳ báo cáo chưa rõ. Hãy chọn lại kỳ trong form tìm kiếm.";
    default:
      return "Cần thêm thông tin để tìm báo cáo.";
  }
}

export function formatTime(iso?: string): string {
  try {
    const d = iso ? new Date(iso) : new Date();
    return d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}
