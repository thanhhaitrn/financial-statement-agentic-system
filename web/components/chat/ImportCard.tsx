"use client";

import { useEffect, useState } from "react";
import { BILLING_TEXT, PROCESSING_TEXT } from "@/lib/labels";
import type { ImportJob } from "@/lib/types";

const STAGE_TEXT: Record<ImportJob["status"], string> = {
  queued: "Tiếp nhận yêu cầu xử lý",
  downloading: "Tải báo cáo từ CafeF",
  parsing: "Trích xuất nội dung báo cáo",
  validating: "Kiểm tra chất lượng nội dung",
  building: "Tạo dữ liệu tra cứu cho báo cáo",
  ready: "Báo cáo sẵn sàng để hỏi đáp",
  failed: "Xử lý báo cáo không thành công",
};

function eventTime(at?: string) {
  const value = Date.parse(at || "");
  return Number.isFinite(value)
    ? new Date(value).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "";
}

/** Worker status history only; billing remains independent of report readiness. */
export default function ImportCard({ job }: { job: ImportJob }) {
  const active = job.status !== "ready" && job.status !== "failed";
  const [now, setNow] = useState(() => Date.now());
  const [showHistory, setShowHistory] = useState(false);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const start = Date.parse(job.created_at || "");
  const elapsed = Number.isFinite(start) ? Math.max(0, Math.floor((now - start) / 1000)) : null;
  const history = job.progress?.length ? job.progress : [{ status: job.status, at: job.updated_at || job.created_at || "" }];
  const expanded = active || job.status === "failed" || showHistory;
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
    note = "Bạn đã có thể hỏi trên báo cáo này. Lần tải báo cáo tiếp theo sẽ mở khi chi phí được xác nhận.";
  } else if (job.status === "ready") {
    note = "Bạn đã có thể đặt câu hỏi trên báo cáo này.";
  }
  return (
    <div className={`import-card import-progress${job.status === "failed" ? " failed" : ""}`} role="status" aria-live="polite">
      <div className="steps-head import-progress-head">
        {active && <span className="step-spinner" aria-hidden="true" />}
        <span className="import-title">{active ? "Đang chuẩn bị báo cáo…" : job.status === "ready" ? "Báo cáo đã sẵn sàng" : "Xử lý báo cáo chưa hoàn tất"}</span>
        {active && elapsed !== null && <span className="steps-elapsed">{elapsed < 60 ? `${elapsed} giây` : `${Math.floor(elapsed / 60)} phút ${elapsed % 60} giây`}</span>}
      </div>
      <p className="import-processing">{PROCESSING_TEXT[job.status] || job.status}{pages}</p>
      <p className="import-origin">{job.origin === "cafef_discovery" ? "Báo cáo từ CafeF" : "Báo cáo tải lên"}</p>
      {!active && job.status === "ready" && <button type="button" className="steps-toggle" aria-expanded={showHistory}
        onClick={() => setShowHistory(!showHistory)}>
        <span className="steps-caret" aria-hidden="true" />
        {showHistory ? "Ẩn nhật ký xử lý" : `Xem nhật ký xử lý · ${history.length} bước`}
      </button>}
      {expanded && <ol className="steps-list import-progress-list" aria-label="Nhật ký xử lý báo cáo">
        {history.map((event, index) => {
          const last = index === history.length - 1;
          const state = event.status === "failed" || history[index + 1]?.status === "failed"
            ? "failed" : last && active ? "running" : "done";
          return <li key={`${event.at}-${index}`} className={`step step-${state}`}>
            <span className="step-icon" aria-hidden="true" />
            <span className="step-text"><span className="step-label">{STAGE_TEXT[event.status]}</span></span>
            {eventTime(event.at) && <time className="step-time" dateTime={event.at}>{eventTime(event.at)}</time>}
          </li>;
        })}
      </ol>}
      <p className="import-billing">Chi phí trích xuất: {billing}</p>
      {note && <p className="import-note">{note}</p>}
    </div>
  );
}
