"use client";

import { FormEvent, useState } from "react";
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
          Hệ thống chỉ lấy danh sách báo cáo công khai; không tải hay xử lý báo cáo nào trước khi bạn xác nhận.
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
            <select className="field" value={period} onChange={(e) => changePeriod(e.target.value as Period)}>
              <option value="any">Mọi kỳ</option>
              <option value="annual">Báo cáo năm</option>
              <option value="quarter">Báo cáo quý</option>
              <option value="half_year">Bán niên</option>
            </select>
          </label>
        </div>
        <div className="form-row">
          <label className="fl">Quý
            <select className="field" value={quarter} disabled={period !== "quarter"} onChange={(e) => setQuarter(e.target.value)}>
              <option value="">Mọi quý</option>
              <option value="1">Quý 1</option>
              <option value="2">Quý 2</option>
              <option value="3">Quý 3</option>
              <option value="4">Quý 4</option>
            </select>
          </label>
          <label className="fl">Nửa năm
            <select className="field" value={half} disabled={period !== "half_year"} onChange={(e) => setHalf(e.target.value)}>
              <option value="">Cả hai</option>
              <option value="1">6 tháng đầu</option>
              <option value="2">6 tháng cuối</option>
            </select>
          </label>
        </div>
        <div className="form-row">
          <label className="fl">Phạm vi
            <select className="field" value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="unknown">Mọi phạm vi</option>
              <option value="consolidated">Hợp nhất</option>
              <option value="separate">Riêng / công ty mẹ</option>
            </select>
          </label>
          <label className="fl">Kiểm toán
            <select className="field" value={audit} onChange={(e) => setAudit(e.target.value)}>
              <option value="unknown">Mọi trạng thái</option>
              <option value="audited">Đã kiểm toán</option>
              <option value="reviewed">Đã soát xét</option>
              <option value="unaudited">Chưa kiểm toán</option>
            </select>
          </label>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
