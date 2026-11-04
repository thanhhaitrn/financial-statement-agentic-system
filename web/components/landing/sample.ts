// Data shown on the landing page, taken from a real report already imported
// into AgentFinX: the audited 2025 separate (parent company) financial
// statements of Công ty Cổ phần Sữa Việt Nam (Vinamilk, HOSE: VNM).
//
// Income statement, page 9 (VND → tỷ đồng):
//   net revenue (code 10)   52.991,5  (2024: 50.676,7)  → +4,6%
//   cost of sales (code 11) 29.430,1  (27.659,3)        → +6,4%
//   gross profit (code 20)  23.561,4  (23.017,4)        → +2,4%
//   gross margin            44,46%    (45,42%)          → −0,96 điểm
// Note VI.2: cost of finished goods sold 27.805,5 (26.264,7) → +5,9%.
// The converted report has no page markers after page 17, so the note is
// cited without a page.

export const SAMPLE = {
  ticker: "VNM",
  company: "Công ty Cổ phần Sữa Việt Nam",
  shortName: "Vinamilk",
  period: "2025",
  previous: "2024",
  report: "VNM — BCTC riêng 2025",
  reportDetail: "Báo cáo tài chính riêng năm 2025 · đã kiểm toán",
  file: "VNM_Baocaotaichinh_2025_Kiemtoan_Congtyme.pdf",
};

export const QUESTION = "Vì sao biên lợi nhuận gộp năm 2025 giảm so với năm 2024?";

export const SOURCES = ["Báo cáo KQKD riêng · tr. 9", "Thuyết minh VI.2"] as const;

// A Vietstock article cached by the news collector. The quote is the start of
// a sentence in it, verbatim; the page shows it with an ellipsis.
export const NEWS = {
  event: "Vinh danh tại IR Awards 2026",
  confirmation: "tin có dẫn nguồn",
  quote: "IR Awards 2026 vinh danh DCM, GMD, MSN, MWG, VIC, VNM ở nhóm Non-Financial Large Cap",
  publisher: "Vietstock",
  published: "30/09/2026",
};

// Page 9 of the same report: the separate income statement (Mẫu B 02 – DN),
// copied as printed, in VND; formulas in the line names are left out.
export const INCOME_STATEMENT = [
  { name: "Doanh thu bán hàng và cung cấp dịch vụ", code: "01", note: "VI.1", now: "53.044.988.379.207", before: "50.699.270.292.593" },
  { name: "Các khoản giảm trừ doanh thu", code: "02", note: "VI.1", now: "53.492.069.944", before: "22.562.380.401" },
  { name: "Doanh thu thuần về bán hàng và cung cấp dịch vụ", code: "10", note: "VI.1", now: "52.991.496.309.263", before: "50.676.707.912.192" },
  { name: "Giá vốn hàng bán và dịch vụ cung cấp", code: "11", note: "VI.2", now: "29.430.136.161.346", before: "27.659.342.054.688" },
  { name: "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ", code: "20", note: "", now: "23.561.360.147.917", before: "23.017.365.857.504" },
  { name: "Doanh thu hoạt động tài chính", code: "21", note: "VI.3", now: "1.632.198.937.753", before: "1.846.711.116.527" },
  { name: "Chi phí tài chính", code: "22", note: "VI.4", now: "433.708.788.247", before: "560.095.970.564" },
  { name: "Trong đó: Chi phí lãi vay", code: "23", note: "", now: "289.972.001.368", before: "227.618.556.891" },
  { name: "Chi phí bán hàng", code: "25", note: "VI.5", now: "12.227.930.844.901", before: "12.046.762.642.558" },
  { name: "Chi phí quản lý doanh nghiệp", code: "26", note: "VI.6", now: "1.155.129.339.145", before: "1.102.644.072.931" },
  { name: "Lợi nhuận thuần từ hoạt động kinh doanh", code: "30", note: "", now: "11.376.790.113.377", before: "11.154.574.287.978" },
  { name: "Thu nhập khác", code: "31", note: "VI.7", now: "116.653.846.969", before: "110.508.071.750" },
  { name: "Chi phí khác", code: "32", note: "VI.8", now: "26.649.261.729", before: "21.592.652.515" },
  { name: "Kết quả từ hoạt động khác", code: "40", note: "", now: "90.004.585.240", before: "88.915.419.235" },
  { name: "Lợi nhuận kế toán trước thuế", code: "50", note: "", now: "11.466.794.698.617", before: "11.243.489.707.213" },
  { name: "Chi phí thuế TNDN hiện hành", code: "51", note: "VI.10", now: "2.128.415.483.304", before: "2.058.534.807.543" },
  { name: "Lợi ích thuế TNDN hoãn lại", code: "52", note: "VI.10", now: "(20.970.420.316)", before: "(77.458.923.279)" },
  { name: "Lợi nhuận sau thuế TNDN", code: "60", note: "", now: "9.359.349.635.629", before: "9.262.413.822.949" },
] as const;

// The gross margin as AgentFinX computes it: code 20 / code 10 × 100, rounded to 0,01.
export const MARGINS = [
  { name: "Biên lợi nhuận gộp", formula: "Lợi nhuận gộp ÷ Doanh thu thuần", now: "44,46%", before: "45,42%", change: "−0,96 điểm" },
] as const;

// The three Vietstock articles about VNM the news collector has cached.
export const NEWS_ARTICLES = [
  { title: "Bà Mai Kiều Liên ba năm liên tiếp được Fortune vinh danh trong Top 100 Phụ nữ Quyền lực nhất Châu Á", date: "06/10/2026" },
  { title: "“Bầu sữa” Vinamilk của F&N", date: "02/10/2026" },
  { title: "6 Non-Financial Large Cap tỏa sáng tại IR Awards 2026: DCM, GMD, MSN, MWG, VIC, VNM", date: "30/09/2026" },
] as const;
