import { BILLING_TEXT, PROCESSING_TEXT } from "@/lib/labels";
import type { ImportJob } from "@/lib/types";

/**
 * Processing and billing are separate: a report can be ready to question
 * while its parsing cost is still being confirmed. Never merge the two lines.
 */
export default function ImportCard({ job }: { job: ImportJob }) {
  const pages = job.page_count ? ` · ${job.page_count} trang` : "";
  let billing = BILLING_TEXT[job.billing_status] || job.billing_status;
  if (job.billing_status === "confirmed" && job.confirmed_credits !== "") {
    billing = `${billing}: ${job.confirmed_credits} credit`;
  }
  let note = "";
  if (job.status === "failed") {
    note = job.error_message || "Không xử lý được báo cáo này.";
  } else if (job.status === "ready" && !job.session_attached) {
    note = "Báo cáo đã sẵn sàng nhưng chưa gắn được vào cuộc trò chuyện này; hãy chọn nó trong danh sách báo cáo.";
  } else if (job.status === "ready" && job.billing_blocking) {
    // Only a blocking pending charge holds back the next import.
    note = "Bạn đã có thể hỏi trên báo cáo này. Lần tải báo cáo tiếp theo sẽ mở khi chi phí được xác nhận.";
  } else if (job.status === "ready" && job.billing_pending) {
    note = "Bạn đã có thể hỏi trên báo cáo này.";
  }
  return (
    <div className={`import-card${job.status === "failed" ? " failed" : ""}`} role="status">
      <p className="import-title">{job.origin === "cafef_discovery" ? "Báo cáo từ CafeF" : "Báo cáo tải lên"}</p>
      <p className="import-processing">Xử lý: {PROCESSING_TEXT[job.status] || job.status}{pages}</p>
      <p className="import-billing">Chi phí trích xuất: {billing}</p>
      {note && <p className="import-note">{note}</p>}
    </div>
  );
}
