"use client";

import { useMemo, useState } from "react";
import Modal from "@/components/Modal";
import Select from "@/components/Select";
import { AUDIT_TEXT, SCOPE_TEXT } from "@/lib/labels";
import type { Dataset } from "@/lib/types";

// Choose the report to ask about by the metadata the registry stores for it:
// company (and ticker), fiscal year, period, scope and audit status, the same
// fields and labels as the upload form. Every option comes from the ready
// reports that still match the earlier choices, so no combination is empty.

type Field = "company" | "year" | "period" | "scope" | "audit";
type Choice = Record<Field, string>;

const EMPTY: Choice = { company: "", year: "", period: "", scope: "", audit: "" };
const FIELDS: { key: Field; label: string }[] = [
  { key: "company", label: "Doanh nghiệp" },
  { key: "year", label: "Năm tài chính" },
  { key: "period", label: "Kỳ" },
  { key: "scope", label: "Phạm vi" },
  { key: "audit", label: "Kiểm toán" },
];

function valueOf(d: Dataset, key: Field): string {
  switch (key) {
    case "company": return d.company;
    case "year": return d.fiscal_year ? String(d.fiscal_year) : "";
    case "period": return d.fiscal_quarter ? String(d.fiscal_quarter) : "year";
    case "scope": return d.scope || "unknown";
    case "audit": return d.audit_status || "unknown";
  }
}

function labelOf(key: Field, value: string, sample?: Dataset): string {
  switch (key) {
    case "company": return sample?.ticker ? `${value} (${sample.ticker})` : value;
    case "year": return value || "Không rõ";
    case "period": return value === "year" ? "Cả năm" : `Quý ${value}`;
    case "scope": return SCOPE_TEXT[value] || value;
    case "audit": return AUDIT_TEXT[value] || value;
  }
}

function matches(d: Dataset, choice: Choice, upTo: number) {
  return FIELDS.slice(0, upTo).every(({ key }) => !choice[key] || valueOf(d, key) === choice[key]);
}

type Props = {
  datasets: Dataset[];
  selectedId: string;
  canClear: boolean;
  onPick: (dataset: Dataset) => void;
  onClear: () => void;
  onClose: () => void;
};

export default function ReportPicker({ datasets, selectedId, canClear, onPick, onClear, onClose }: Props) {
  const ready = useMemo(() => datasets.filter((d) => d.ready), [datasets]);
  const [choice, setChoice] = useState<Choice>(() => {
    const current = ready.find((d) => d.id === selectedId);
    return current ? { ...EMPTY, company: current.company } : EMPTY;
  });

  // Options of each field: values found among reports matching the fields before it.
  const options = useMemo(() => FIELDS.map(({ key }, i) => {
    const seen = new Map<string, Dataset>();
    for (const d of ready) if (matches(d, choice, i) && !seen.has(valueOf(d, key))) seen.set(valueOf(d, key), d);
    return [...seen.entries()]
      .map(([value, sample]) => ({ value, label: labelOf(key, value, sample) }))
      .sort((a, b) => (key === "year" ? b.value.localeCompare(a.value) : a.label.localeCompare(b.label, "vi")));
  }), [ready, choice]);

  const results = ready.filter((d) => matches(d, choice, FIELDS.length));

  function set(key: Field, value: string) {
    // A changed field resets the ones after it, whose options depend on it.
    const i = FIELDS.findIndex((f) => f.key === key);
    const next = { ...choice, [key]: value };
    for (const f of FIELDS.slice(i + 1)) next[f.key] = "";
    setChoice(next);
  }

  return (
    <Modal
      title="Chọn báo cáo"
      onClose={onClose}
      wide
      footer={canClear ? <button className="btn btn-outline" onClick={onClear}>Bỏ chọn báo cáo</button> : undefined}
    >
      {ready.length === 0 ? (
        <p className="muted">Chưa có báo cáo nào sẵn sàng cho tài khoản này.</p>
      ) : (
        <>
          <div className="picker-fields">
            {FIELDS.map(({ key, label }, i) => (
              <div key={key} className="fl">
                <span>{label}</span>
                <Select label={label} value={choice[key]} onChange={(v) => set(key, v)}
                  options={[{ value: "", label: "Tất cả" }, ...options[i]]} />
              </div>
            ))}
          </div>
          <p className="picker-count muted small">{results.length} báo cáo khớp</p>
          <div className="picker-results search-list">
            {results.map((d) => (
              <button
                key={d.id}
                className={`search-item ${d.id === selectedId ? "on" : ""}`}
                data-dataset-id={d.id}
                onClick={() => onPick(d)}
              >
                <span>{d.label}</span>
                <span className="muted small">
                  {[labelOf("scope", valueOf(d, "scope")), labelOf("audit", valueOf(d, "audit")),
                    d.access === "shared" ? "Dùng chung" : "Của bạn"].join(" · ")}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}
