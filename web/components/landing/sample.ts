// Illustrative data for the landing page. "Công ty Cổ phần Mẫu" and the
// ticker MAU are made up (MAU is not a listed ticker in the CafeF catalog);
// the page labels them as illustrative wherever they appear.
//
// The figures are consistent with one another (tỷ đồng):
//   revenue 1.248,0 (Q2/2025: 1.151,0)  → +8,4%
//   cost of goods sold 978,4 (875,9)     → +11,7%
//   gross profit 269,6 (275,1)           → −2,0%
//   gross margin 21,6% (23,9%)           → −2,3 điểm

export const SAMPLE = {
  ticker: "MAU",
  company: "Công ty Cổ phần Mẫu",
  period: "Q2/2026",
  previous: "Q2/2025",
  report: "MAU — BCTC Q2/2026",
  reportDetail: "Báo cáo tài chính hợp nhất quý 2/2026 · đã soát xét",
  file: "MAU_BCTC_Q2_2026.pdf",
};

export const METRICS = [
  { label: "Doanh thu thuần", value: "1.248", unit: "tỷ đồng", change: "+8,4%", direction: "up" },
  { label: "Biên lợi nhuận gộp", value: "21,6", unit: "%", change: "−2,3 điểm", direction: "down" },
  { label: "ROE 4 quý", value: "14,2", unit: "%", change: "−0,6 điểm", direction: "down" },
  { label: "Nợ vay / VCSH", value: "0,62", unit: "lần", change: "+0,05", direction: "up" },
] as const;

export const ACTIVITY = [
  { step: "Đọc file PDF", detail: `${SAMPLE.file} · 42 trang`, time: "09:41" },
  { step: "Trích xuất bảng", detail: "Bảng cân đối, kết quả kinh doanh, lưu chuyển tiền tệ · 186 dòng số liệu", time: "09:42" },
  { step: "Đối chiếu cân đối", detail: "Tổng tài sản khớp tổng nguồn vốn", time: "09:42" },
  { step: "So sánh cùng kỳ", detail: `12 chỉ tiêu so với ${SAMPLE.previous}`, time: "09:43" },
] as const;

export const QUESTION = "Vì sao biên lợi nhuận giảm so với cùng kỳ?";

export const SOURCES = ["Báo cáo KQKD · tr. 6", "Thuyết minh 24 · tr. 31"] as const;

export const COMPARISON = [
  { item: "Doanh thu thuần", now: "1.248,0", before: "1.151,0", change: "+8,4%" },
  { item: "Giá vốn", now: "978,4", before: "875,9", change: "+11,7%" },
  { item: "Lợi nhuận gộp", now: "269,6", before: "275,1", change: "−2,0%" },
  { item: "Biên LN gộp", now: "21,6%", before: "23,9%", change: "−2,3 điểm" },
] as const;
