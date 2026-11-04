"use client";

import { FormEvent, useState } from "react";
import Select from "@/components/Select";
import Modal from "@/components/Modal";
import type { DiscoverySearch } from "@/lib/types";

type Period = "any" | "annual" | "quarter" | "half_year";

/** Search CafeF metadata only; nothing is downloaded before the user confirms. */
export default function DiscoveryModal({
  prefill = {}, onClose, onSubmit,
}: { prefill?: DiscoverySearch; onClose: () => void; onSubmit: (search: DiscoverySearch) => Promise<void> }) {
  const [subject, setSubject] = useState(prefill.subject ?? prefill.ticker ?? "");
  const [year, setYear] = useState(prefill.fiscal_year ? String(prefill.fiscal_year) : "");
  const [period, setPeriod] = useState<Period>((prefill.period as Period) ?? "any");
  const [quarter, setQuarter] = useState(prefill.fiscal_quarter ? String(prefill.fiscal_quarter) : "");
  const [half, setHalf] = useState(prefill.fiscal_half ? String(prefill.fiscal_half) : "");
  const [scope, setScope] = useState(prefill.scope ?? "unknown");
  const [audit, setAudit] = useState(prefill.audit_status ?? "unknown");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function changePeriod(value: Period) {
    setPeriod(value);
    if (value !== "quarter") setQuarter("");
    if (value !== "half_year") setHalf("");
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!subject.trim()) {
      setError("Hãy nhập mã CK hoặc tên công ty.");
      return;
    }
    if (year.trim() && !/^\d{4}$/.test(year.trim())) {
      setError("Năm tài chính không hợp lệ.");
      return;
    }
    const search: DiscoverySearch = { subject: subject.trim(), period, scope, audit_status: audit };
    if (year.trim()) search.fiscal_year = Number(year.trim());
    if (quarter) search.fiscal_quarter = Number(quarter);
    if (half) search.fiscal_half = Number(half);
    setBusy(true);
    setError("");
    try {
      await onSubmit(search);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không tìm được báo cáo.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Tìm báo cáo trên CafeF"
      wide
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <button type="button" className="btn btn-outline" onClick={onClose} disabled={busy}>Hủy</button>
          <button type="submit" form="discovery-form" className="btn btn-dark" disabled={busy}>
            {busy ? "Đang tìm…" : "Tìm báo cáo"}
          </button>
        </>
      }
    >
      <form id="discovery-form" className="form-grid" onSubmit={submit} noValidate>
        <p className="muted small">
          Hệ thống chỉ lấy danh sách báo cáo công khai. Không tải hay xử lý báo cáo nào trước khi bạn xác nhận.
        </p>
        <label className="fl">Mã CK hoặc tên công ty *
          <input className="field" autoFocus value={subject} maxLength={120} onChange={(e) => setSubject(e.target.value)}
            placeholder="VD: VNM hoặc Sữa Việt Nam" />
        </label>
        <div className="form-row">
          <label className="fl">Năm tài chính
            <input className="field" inputMode="numeric" value={year} maxLength={4} onChange={(e) => setYear(e.target.value)} placeholder="Mọi năm" />
          </label>
          <label className="fl">Loại kỳ
            <Select
              label="Loại kỳ"
              value={period}
              onChange={(v) => changePeriod(v as Period)}
              options={[
                { value: "any", label: "Mọi kỳ" },
                { value: "annual", label: "Báo cáo năm" },
                { value: "quarter", label: "Báo cáo quý" },
                { value: "half_year", label: "Bán niên" },
              ]}
            />
          </label>
        </div>
        <div className="form-row">
          <label className="fl">Quý
            <Select
              label="Quý"
              value={quarter}
              disabled={period !== "quarter"}
              onChange={(v) => setQuarter(v)}
              options={[
                { value: "", label: "Mọi quý" },
                { value: "1", label: "Quý 1" },
                { value: "2", label: "Quý 2" },
                { value: "3", label: "Quý 3" },
                { value: "4", label: "Quý 4" },
              ]}
            />
          </label>
          <label className="fl">Nửa năm
            <Select
              label="Nửa năm"
              value={half}
              disabled={period !== "half_year"}
              onChange={(v) => setHalf(v)}
              options={[
                { value: "", label: "Cả hai" },
                { value: "1", label: "6 tháng đầu" },
                { value: "2", label: "6 tháng cuối" },
              ]}
            />
          </label>
        </div>
        <div className="form-row">
          <label className="fl">Phạm vi
            <Select
              label="Phạm vi"
              value={scope}
              onChange={(v) => setScope(v)}
              options={[
                { value: "unknown", label: "Mọi phạm vi" },
                { value: "consolidated", label: "Hợp nhất" },
                { value: "separate", label: "Riêng / công ty mẹ" },
              ]}
            />
          </label>
          <label className="fl">Kiểm toán
            <Select
              label="Kiểm toán"
              value={audit}
              onChange={(v) => setAudit(v)}
              options={[
                { value: "unknown", label: "Mọi trạng thái" },
                { value: "audited", label: "Đã kiểm toán" },
                { value: "reviewed", label: "Đã soát xét" },
                { value: "unaudited", label: "Chưa kiểm toán" },
              ]}
            />
          </label>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
