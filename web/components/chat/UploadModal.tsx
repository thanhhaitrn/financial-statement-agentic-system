"use client";

import { FormEvent, useState } from "react";
import Modal from "@/components/Modal";

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export type UploadMetadata = {
  company: string;
  ticker: string;
  fiscal_year?: number;
  fiscal_quarter?: number;
  scope: string;
  audit_status: string;
};

export type UploadRequest = { file: File; metadata: UploadMetadata; consent: boolean };

/** One PDF (≤ 25 MB, ≤ 200 pages) plus its metadata; cloud parsing is opt-in. */
export default function UploadModal({
  onClose, onSubmit,
}: { onClose: () => void; onSubmit: (request: UploadRequest) => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [company, setCompany] = useState("");
  const [ticker, setTicker] = useState("");
  const [year, setYear] = useState("");
  const [quarter, setQuarter] = useState("");
  const [scope, setScope] = useState("unknown");
  const [audit, setAudit] = useState("unknown");
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function problem(): string {
    if (!file) return "Hãy chọn file PDF.";
    const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name || "");
    if (!isPdf) return "Chỉ nhận file PDF.";
    if (file.size > MAX_UPLOAD_BYTES) return "PDF vượt quá giới hạn 25 MB.";
    if (!company.trim()) return "Hãy nhập tên công ty.";
    if (year.trim() && !/^\d{4}$/.test(year.trim())) return "Năm tài chính không hợp lệ.";
    return "";
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const issue = problem();
    if (issue) {
      setError(issue);
      return;
    }
    const metadata: UploadMetadata = {
      company: company.trim(),
      ticker: ticker.trim().toUpperCase(),
      scope,
      audit_status: audit,
    };
    if (year.trim()) metadata.fiscal_year = Number(year.trim());
    if (quarter) metadata.fiscal_quarter = Number(quarter);
    setBusy(true);
    setError("");
    try {
      await onSubmit({ file: file as File, metadata, consent });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không tải được báo cáo.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Tải báo cáo PDF"
      wide
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <button type="button" className="btn btn-outline" onClick={onClose} disabled={busy}>Hủy</button>
          <button type="submit" form="upload-form" className="btn btn-dark" disabled={busy}>
            {busy ? "Đang tải lên…" : "Tải lên và xử lý"}
          </button>
        </>
      }
    >
      <form id="upload-form" className="form-grid" onSubmit={submit} noValidate>
        <p className="muted small">Chỉ nhận PDF, tối đa 25 MB và 200 trang. Báo cáo bạn tải lên chỉ tài khoản của bạn xem được.</p>
        <label className="dropzone">
          <input
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <strong>{file ? file.name : "Chọn file PDF"}</strong>
          <span className="muted">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : "Một báo cáo tài chính dạng PDF"}</span>
        </label>
        <label className="fl">Tên công ty *
          <input className="field" value={company} maxLength={160} onChange={(e) => setCompany(e.target.value)}
            placeholder="VD: Công ty Cổ phần Sữa Việt Nam" />
        </label>
        <div className="form-row">
          <label className="fl">Mã CK
            <input className="field" value={ticker} maxLength={16} onChange={(e) => setTicker(e.target.value)} placeholder="VNM" />
          </label>
          <label className="fl">Năm tài chính
            <input className="field" inputMode="numeric" value={year} maxLength={4} onChange={(e) => setYear(e.target.value)} placeholder="2025" />
          </label>
          <label className="fl">Kỳ
            <select className="field" value={quarter} onChange={(e) => setQuarter(e.target.value)}>
              <option value="">Cả năm</option>
              <option value="1">Quý 1</option>
              <option value="2">Quý 2</option>
              <option value="3">Quý 3</option>
              <option value="4">Quý 4</option>
            </select>
          </label>
        </div>
        <div className="form-row">
          <label className="fl">Phạm vi
            <select className="field" value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="unknown">Không rõ</option>
              <option value="consolidated">Hợp nhất</option>
              <option value="separate">Riêng / công ty mẹ</option>
            </select>
          </label>
          <label className="fl">Kiểm toán
            <select className="field" value={audit} onChange={(e) => setAudit(e.target.value)}>
              <option value="unknown">Không rõ</option>
              <option value="audited">Đã kiểm toán</option>
              <option value="reviewed">Đã soát xét</option>
              <option value="unaudited">Chưa kiểm toán</option>
            </select>
          </label>
        </div>
        <label className="consent">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>
            Đồng ý gửi PDF tới dịch vụ trích xuất đám mây LlamaParse nếu không đọc được lớp văn bản của file. Việc
            này phát sinh chi phí xử lý. Không đồng ý thì chỉ dùng trích xuất cục bộ.
          </span>
        </label>
        {error && <p className="form-error" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
