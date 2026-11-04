"use client";

import { useEffect, useState } from "react";
import Modal from "@/components/Modal";
import { api } from "@/lib/api";
import type { ReportSource } from "@/lib/types";
import { Icon } from "./Icons";

type SourceInfo = { source: ReportSource; company: string; has_pdf: boolean; page_count: number; page: number };

function SourceViewer({ source, messageId, onClose }: {
  source: ReportSource; messageId: string; onClose: () => void;
}) {
  const [info, setInfo] = useState<SourceInfo | null>(null);
  const [error, setError] = useState("");
  const [page, setPage] = useState(source.page || 1);
  const [imageState, setImageState] = useState<"loading" | "ready" | "error">("loading");
  const [zoom, setZoom] = useState(false);
  const base = `/api/messages/${encodeURIComponent(messageId)}/sources/${encodeURIComponent(source.id)}`;

  useEffect(() => {
    let stopped = false;
    api<SourceInfo>(base).then((data) => {
      if (!stopped) { setInfo(data); setPage(data.page); }
    }).catch((err) => { if (!stopped) setError(err instanceof Error ? err.message : "Chưa mở được nguồn báo cáo."); });
    return () => { stopped = true; };
  }, [base]);

  function go(next: number) {
    setImageState("loading"); setZoom(false); setPage(next);
  }

  return <Modal title="Đối chiếu báo cáo" onClose={onClose} wide className="source-modal">
    <div className="source-summary">
      <p className="source-metric">{source.label}</p>
      <p className="source-number">{source.value} <span>{source.unit}</span></p>
      <p className="source-period">{source.period}{info?.company ? ` · ${info.company}` : ""}</p>
    </div>
    {error && <p className="form-error" role="alert">{error}</p>}
    {!info && !error && <p className="source-loading" role="status">Đang mở báo cáo gốc…</p>}
    {info && !info.has_pdf && <div className="source-no-pdf">
      <p>Đây là dữ liệu đã trích xuất từ báo cáo. Báo cáo này chưa có bản PDF gốc để xem trang.</p>
      {source.section && <p>{source.section}</p>}
    </div>}
    {info?.has_pdf && <>
      <div className="source-toolbar">
        <div className="source-pages">
          <button type="button" className="chip-btn secondary" aria-label="Trang trước" disabled={page <= 1} onClick={() => go(page - 1)}>‹</button>
          <span>Trang PDF <strong>{page}</strong> / {info.page_count}</span>
          <button type="button" className="chip-btn secondary" aria-label="Trang sau" disabled={page >= info.page_count} onClick={() => go(page + 1)}>›</button>
        </div>
        <div className="source-tools">
          <button type="button" className="chip-btn secondary" aria-pressed={zoom} onClick={() => setZoom(!zoom)}>{zoom ? "Vừa khung" : "Phóng to"}</button>
          <a href={`${base}/pdf#page=${page}`} className="source-pdf-link" target="_blank" rel="noopener noreferrer">Mở PDF gốc ↗</a>
        </div>
      </div>
      {!source.page && <p className="source-hint">Chưa xác định được trang của số liệu này. Bạn có thể lật trang để đối chiếu.</p>}
      <div className={`source-page-scroll${zoom ? " is-zoomed" : ""}`}>
        {imageState === "loading" && <p className="source-loading" role="status">Đang tải trang {page}…</p>}
        {imageState === "error" && <p className="form-error" role="alert">Chưa hiển thị được trang này. Hãy bấm “Mở PDF gốc” để xem báo cáo.</p>}
        {/* This image is rendered from the source PDF by the authenticated API. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img key={page} src={`${base}/page?page=${page}`} alt={`Trang ${page} của báo cáo gốc`}
          className="source-page-image" hidden={imageState !== "ready"}
          onLoad={() => setImageState("ready")} onError={() => setImageState("error")} />
      </div>
      <p className="source-hint">Trang hiển thị trực tiếp từ PDF gốc. Số trang PDF có thể khác số in trên tài liệu.</p>
    </>}
  </Modal>;
}

export default function ReportSources({ sources, messageId }: { sources: ReportSource[]; messageId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<ReportSource | null>(null);
  if (!sources.length) return null;
  const visible = expanded ? sources : sources.slice(0, 3);
  return <div className="answer-sources">
    <p className="answer-sources-title"><Icon name="report" size={14} />Đối chiếu báo cáo</p>
    <div className="source-chips">
      {visible.map((source) => <button type="button" key={source.id} className="source-chip"
        onClick={() => setSelected(source)} aria-label={`Đối chiếu ${source.label}: ${source.value}${source.page ? `, trang PDF ${source.page}` : ""}`}>
        <span className="source-chip-label" title={source.label}>{source.label}</span>
        <span className="source-chip-value">{source.value}</span>
        <span className="source-chip-page">{source.page ? `Trang ${source.page} ↗` : "Xem nguồn ↗"}</span>
      </button>)}
    </div>
    {sources.length > 3 && <button type="button" className="source-more" aria-expanded={expanded}
      onClick={() => setExpanded(!expanded)}>{expanded ? "Thu gọn nguồn" : `Xem thêm ${sources.length - 3} nguồn`}</button>}
    {selected && <SourceViewer source={selected} messageId={messageId} onClose={() => setSelected(null)} />}
  </div>;
}
