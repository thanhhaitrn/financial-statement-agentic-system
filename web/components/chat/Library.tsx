"use client";

import { useMemo, useState } from "react";
import { AUDIT_TEXT, SCOPE_TEXT } from "@/lib/labels";
import type { Dataset } from "@/lib/types";
import { Icon } from "./Icons";

// A library page in the chat's main panel: the reports this account can ask
// about, or the companies whose news it can ask about. Picking one starts a
// conversation; the page itself only filters what /api/datasets returned.

export type LibraryKind = "reports" | "news";

type Props = {
  kind: LibraryKind;
  datasets: Dataset[];
  loaded: boolean;
  discovery: boolean;
  newsEnabled: boolean;
  collapsed: boolean;
  onOpenSidebar: () => void;
  onPick: (dataset: Dataset) => void;
  onUpload: () => void;
  onDiscover: () => void;
};

const TABS = [["all", "Tất cả"], ["shared", "Dùng chung"], ["owned", "Của bạn"]] as const;
type Tab = (typeof TABS)[number][0];
const COVERS = ["#e9e4da", "#dfe4e2", "#e6e1e8", "#e3e7dc", "#ece2d8", "#dde2ea"];

const fold = (text: string) => text.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase();
const companyOf = (d: Dataset) => d.company || d.label;

/** The ticker, or initials of the company name without its legal form. */
function monogram(d: Dataset) {
  if (d.ticker) return d.ticker.toUpperCase();
  const name = companyOf(d).replace(/^(công ty|ctcp)\s+(cổ phần|tnhh)?\s*(tập đoàn)?/i, "").trim();
  return name.split(/\s+/).filter(Boolean).slice(0, 3).map((w) => w[0]).join("").toUpperCase();
}

function cover(d: Dataset) {
  let h = 0;
  for (const ch of companyOf(d)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COVERS[h % COVERS.length];
}

function reportMeta(d: Dataset) {
  const period = d.fiscal_quarter ? `Quý ${d.fiscal_quarter}/${d.fiscal_year}` : d.fiscal_year ? `Năm ${d.fiscal_year}` : "";
  const scope = d.scope && d.scope !== "unknown" ? SCOPE_TEXT[d.scope] : "";
  const audit = d.audit_status && d.audit_status !== "unknown" ? AUDIT_TEXT[d.audit_status] : "";
  return [period, scope, audit].filter(Boolean).join(" · ");
}

export default function Library(props: Props) {
  const { kind, datasets, loaded } = props;
  const [tab, setTab] = useState<Tab>("all");
  const [query, setQuery] = useState("");
  const [layout, setLayout] = useState<"grid" | "list">("grid");
  const [menu, setMenu] = useState(false);
  const isNews = kind === "news";

  const counts = useMemo(() => {
    const byCompany = new Map<string, number>();
    for (const d of datasets) if (d.ready) byCompany.set(companyOf(d), (byCompany.get(companyOf(d)) || 0) + 1);
    return byCompany;
  }, [datasets]);

  const items = useMemo(() => {
    let list = datasets;
    if (isNews) {
      // One ready report per company: the conversation opens on it.
      list = list.filter((d, i) => d.ready && list.findIndex((o) => o.ready && companyOf(o) === companyOf(d)) === i);
    } else if (tab !== "all") {
      list = list.filter((d) => d.access === tab);
    }
    const q = fold(query.trim());
    if (q) list = list.filter((d) => fold(`${d.label} ${d.company} ${d.ticker}`).includes(q));
    return list;
  }, [datasets, isNews, tab, query]);

  return (
    <div className="lib">
      <div className="lib-head">
        {props.collapsed && (
          <button className="icon-btn" onClick={props.onOpenSidebar} aria-label="Mở thanh bên">☰</button>
        )}
        <h1>{isNews ? "Tin doanh nghiệp" : "Báo cáo"}</h1>
        <div className="lib-tools">
          <div className="lib-layout" role="group" aria-label="Kiểu hiển thị">
            <button className={layout === "grid" ? "on" : ""} aria-pressed={layout === "grid"} aria-label="Dạng lưới" onClick={() => setLayout("grid")}>
              <Icon name="grid" />
            </button>
            <button className={layout === "list" ? "on" : ""} aria-pressed={layout === "list"} aria-label="Dạng danh sách" onClick={() => setLayout("list")}>
              <Icon name="list" />
            </button>
          </div>
          <label className="lib-search">
            <Icon name="search" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={isNews ? "Tìm doanh nghiệp" : "Tìm báo cáo"}
              aria-label={isNews ? "Tìm doanh nghiệp" : "Tìm báo cáo"}
            />
          </label>
          {!isNews && (
            <div className="lib-new">
              <button className="btn btn-dark" onClick={() => setMenu(!menu)} aria-haspopup="menu" aria-expanded={menu}>
                Thêm báo cáo <Icon name="chevron" size={14} />
              </button>
              {menu && (
                <div className="dropdown" role="menu">
                  <button role="menuitem" onClick={() => { setMenu(false); props.onUpload(); }}>Tải PDF lên</button>
                  {props.discovery && (
                    <button role="menuitem" onClick={() => { setMenu(false); props.onDiscover(); }}>Tìm trên CafeF</button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {isNews ? (
        <p className="lib-note">
          {props.newsEnabled
            ? "Chọn doanh nghiệp: câu hỏi về tin tức gần đây được điền sẵn, AgentFinX tìm tin trên Vietstock khi bạn gửi."
            : "Máy chủ này chưa bật phân tích tin doanh nghiệp; câu trả lời sẽ chỉ dựa trên báo cáo."}
        </p>
      ) : (
        <div className="lib-tabs" role="tablist">
          {TABS.map(([value, label]) => (
            <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "on" : ""} onClick={() => setTab(value)}>
              {label}
            </button>
          ))}
        </div>
      )}

      {loaded && items.length === 0 && (
        <p className="lib-empty">
          {query ? "Không có kết quả." : isNews ? "Chưa có báo cáo nào sẵn sàng." : "Chưa có báo cáo nào trong mục này."}
        </p>
      )}
      <div className={layout === "grid" ? "lib-grid" : "lib-list"}>
        {items.map((d) => (
          <button key={d.id} className="lib-card" disabled={!d.ready} onClick={() => props.onPick(d)}>
            <span className="lib-cover" style={{ background: cover(d) }} aria-hidden="true">
              <span className="lib-sheet"><b>{monogram(d)}</b><i /><i /><i /></span>
            </span>
            <span className="lib-info">
              <span className="lib-name">{companyOf(d)}</span>
              <span className="lib-meta">
                {isNews ? `${counts.get(companyOf(d)) || 1} báo cáo · hỏi tin tức gần đây` : reportMeta(d) || d.label}
              </span>
              {!isNews && (
                <span className={`lib-badge ${d.ready ? d.access : "pending"}`}>
                  {!d.ready ? "Chưa sẵn sàng" : d.access === "shared" ? "Dùng chung" : "Của bạn"}
                </span>
              )}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
