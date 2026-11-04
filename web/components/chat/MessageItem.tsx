"use client";

import { useId, useState } from "react";
import Select from "@/components/Select";
import { LogoMark } from "@/components/Logo";
import { newIdempotencyKey } from "@/lib/api";
import {
  AUDIT_TEXT,
  SCOPE_TEXT,
  VERIFICATION_TEXT,
  WARNING_TEXT,
  clarificationText,
  formatTime,
} from "@/lib/labels";
import type { Candidate, DiscoverySearch, Message, RunStep } from "@/lib/types";
import { StepTrace } from "./RunSteps";
import AnswerContent from "./AnswerContent";
import { Icon } from "./Icons";
import ReportSources from "./ReportSources";

// Report sources are untrusted CafeF data: only these hosts become links.
const SAFE_SOURCE_URL = /^https:\/\/([a-z0-9-]+\.)*(cafef\.vn|mediacdn\.vn)\//i;

export type MessageActions = {
  reportDiscovery: boolean;
  /** The open conversation has a report and is writable. */
  canOverride: boolean;
  /** Confirming an import opens a new conversation (this one has a report). */
  importOpensNewConversation: boolean;
  onDiscover: (search: DiscoverySearch) => Promise<void>;
  onEditSearch: (prefill: DiscoverySearch) => void;
  onConfirmCandidate: (candidate: Candidate, sourceSessionId: string, consent: boolean, key: string) => Promise<void>;
  onAnswerWithReport: (runId: string) => Promise<void>;
  onChooseDataset: () => void;
  onOpenUpload: () => void;
  onOpenDiscovery: () => void;
};

function errorText(err: unknown) {
  return err instanceof Error ? err.message : "Đã có lỗi xảy ra.";
}

/** A button whose async action shows its own busy state and error. */
function ActionButton({
  label, onRun, className = "chip-btn", once = false,
}: { label: string; onRun: () => Promise<void> | void; className?: string; once?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");
  return (
    <>
      <button
        type="button"
        className={className}
        disabled={busy || done}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            await onRun();
            if (once) setDone(true);
          } catch (err) {
            setError(errorText(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        {label}
      </button>
      {error && <span className="inline-error" role="alert">{error}</span>}
    </>
  );
}

function OverrideButton({ payload, actions }: { payload: Record<string, unknown>; actions: MessageActions }) {
  // A question the planner took as "find a report" can still be answered on
  // the report this conversation has — an explicit user choice.
  if (payload.origin !== "chat" || !payload.run_id || !actions.canOverride) return null;
  return (
    <ActionButton
      once
      className="chip-btn secondary"
      label="Trả lời trên báo cáo đang mở"
      onRun={() => actions.onAnswerWithReport(String(payload.run_id))}
    />
  );
}

function candidatePeriod(c: Candidate) {
  if (!c.period_verified) return `Chưa xác minh kỳ${c.period_label ? ` (CafeF ghi: ${c.period_label})` : ""}`;
  if (c.period_type === "annual") return `Báo cáo năm ${c.fiscal_year}`;
  if (c.period_type === "quarter") return `Quý ${c.fiscal_quarter}/${c.fiscal_year}`;
  if (c.period_type === "half_year") return `Bán niên ${c.fiscal_year}`;
  return "Chưa rõ kỳ";
}

function isExpired(c: Candidate) {
  const at = Date.parse(c.expires_at || "");
  return Number.isFinite(at) && at <= Date.now();
}

function CandidateRow({
  candidate: c, payload, actions, visible = true,
}: { candidate: Candidate; payload: Record<string, any>; actions: MessageActions; visible?: boolean }) {
  const [open, setOpen] = useState(false);
  // One key per confirmation: a network retry replays the same import and
  // never starts a second one.
  const [key] = useState(newIdempotencyKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const expired = isExpired(c);

  async function confirm() {
    setBusy(true);
    setError("");
    try {
      // Imports always allow cloud parsing when the PDF has no text layer.
      await actions.onConfirmCandidate(c, String(payload.source_session_id || ""), true, key);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <li className={`cand-row${c.period_verified ? "" : " unverified"}`} hidden={!visible}>
      <p className="cand-title">{c.title}</p>
      <p className="cand-facts">
        {[candidatePeriod(c), SCOPE_TEXT[c.scope] || c.scope, AUDIT_TEXT[c.audit_status] || c.audit_status].join(" · ")}
      </p>
      {!c.period_verified && VERIFICATION_TEXT[c.verification_note] && (
        <p className="rc-warn">{VERIFICATION_TEXT[c.verification_note]}</p>
      )}
      <div className="cand-actions">
        {SAFE_SOURCE_URL.test(c.source_url || "") && (
          <a className="cand-source" href={c.source_url} target="_blank" rel="noopener noreferrer">
            Nguồn: {c.source_host || "CafeF"}
          </a>
        )}
        {expired ? (
          <ActionButton label="Đã hết hạn — tìm lại" onRun={() => actions.onDiscover(payload.search_request || {})} />
        ) : (
          <button type="button" className="chip-btn" onClick={() => setOpen(true)} disabled={open}>
            Chọn báo cáo này
          </button>
        )}
      </div>
      {open && !expired && (
        <div className="confirm-panel">
          <p className="confirm-title">Xác nhận: {c.title}</p>
          <p className="rc-note">
            {actions.importOpensNewConversation
              ? "Báo cáo sẽ mở trong một cuộc trò chuyện mới; cuộc trò chuyện này giữ nguyên báo cáo đang có."
              : "Báo cáo sẽ được xử lý trong cuộc trò chuyện này."}
          </p>
          <p className="rc-note">
            Nguồn công khai trên CafeF. Kết quả trích xuất có thể dùng chung giữa các tài khoản; báo cáo,
            quyền truy cập và lịch sử trò chuyện của bạn vẫn riêng. Chi phí hiển thị sau khi nhà cung cấp xác nhận.
          </p>
          {!c.period_verified && (
            <p className="rc-warn">Kỳ của báo cáo này chưa được xác minh; hãy kiểm tra tiêu đề trước khi xác nhận.</p>
          )}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="confirm-actions">
            <button type="button" className="btn btn-outline" onClick={() => setOpen(false)} disabled={busy}>Hủy</button>
            <button type="button" className="btn btn-dark" onClick={confirm} disabled={busy}>
              {busy ? "Đang gửi…" : "Xác nhận tải và xử lý"}
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

function CandidatesCard({ payload, actions }: { payload: Record<string, any>; actions: MessageActions }) {
  const search = payload.search || {};
  const who = search.ticker || search.subject || "";
  const candidates: Candidate[] = payload.candidates || [];
  const meta = [search.company, search.exchange].filter(Boolean).join(" · ");
  const listId = useId();
  const [expanded, setExpanded] = useState(candidates.length <= 3);
  const [query, setQuery] = useState("");
  const [year, setYear] = useState("");
  const [period, setPeriod] = useState("");
  const [scope, setScope] = useState("");
  const fold = (value: string) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase();
  const terms = fold(query).trim().split(/\s+/).filter(Boolean);
  const yearOf = (c: Candidate) => c.period_verified && c.fiscal_year ? String(c.fiscal_year) : "unverified";
  const periodOf = (c: Candidate) => c.period_verified ? c.period_type : "unverified";
  const periodLabels: Record<string, string> = { annual: "Cả năm", quarter: "Theo quý", half_year: "Bán niên", unknown: "Chưa rõ kỳ", unverified: "Chưa xác minh kỳ" };
  const years = [...new Set(candidates.map(yearOf))].sort((a, b) => b.localeCompare(a));
  const periods = [...new Set(candidates.map(periodOf))];
  const scopes = [...new Set(candidates.map((c) => c.scope || "unknown"))];
  const matching = candidates.filter((c) => {
    const text = fold([c.title, c.ticker, c.company, candidatePeriod(c), c.period_label, SCOPE_TEXT[c.scope], AUDIT_TEXT[c.audit_status]].filter(Boolean).join(" "));
    return (!year || yearOf(c) === year) && (!period || periodOf(c) === period)
      && (!scope || (c.scope || "unknown") === scope) && terms.every((term) => text.includes(term));
  });
  const visibleIds = new Set(matching.map((c) => c.candidate_id));
  const filtered = Boolean(query.trim() || year || period || scope);
  const truncated = payload.truncated || (payload.warnings || []).includes("results_truncated");

  function clearFilters() {
    setQuery(""); setYear(""); setPeriod(""); setScope("");
  }

  return (
    <div className="report-card report-results">
      <p className="rc-title">{candidates.length ? `Báo cáo tìm thấy cho ${who}` : `Không có báo cáo nào khớp cho ${who}`}</p>
      {meta && <p className="rc-meta">{meta}</p>}
      {(payload.warnings || []).map((code: string) =>
        code !== "results_truncated" && WARNING_TEXT[code] ? <p key={code} className="rc-warn">{WARNING_TEXT[code]}</p> : null,
      )}
      {!candidates.length && (
        <p className="rc-note">CafeF trả lời bình thường nhưng không có báo cáo khớp bộ lọc. Hãy nới bộ lọc hoặc tải PDF lên.</p>
      )}
      {candidates.length > 0 && <>
        <button type="button" className="cand-toggle" aria-expanded={expanded} aria-controls={listId}
          onClick={() => setExpanded(!expanded)}>
          <span>{expanded ? "Thu gọn danh sách" : `Xem ${candidates.length} báo cáo`}</span>
          <Icon name="chevron" size={15} />
        </button>
        {truncated && <p className="rc-note">{candidates.length} kết quả mới nhất. Có thể tìm lại theo năm hoặc kỳ để xem báo cáo khác.</p>}
        <div id={listId} className="cand-browser" hidden={!expanded}>
          <label className="cand-search">
            <Icon name="search" size={16} />
            <input type="search" aria-label="Tìm trong danh sách báo cáo" placeholder={`Tìm trong ${candidates.length} báo cáo…`}
              maxLength={160} value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <div className="cand-filters">
            <Select label="Lọc năm báo cáo" value={year} onChange={setYear}
              options={[{ value: "", label: "Tất cả năm" }, ...years.map((value) => ({ value, label: value === "unverified" ? "Chưa xác minh năm" : value }))]} />
            <Select label="Lọc kỳ báo cáo" value={period} onChange={setPeriod}
              options={[{ value: "", label: "Tất cả kỳ" }, ...periods.map((value) => ({ value, label: periodLabels[value] || value }))]} />
            <Select label="Lọc phạm vi báo cáo" value={scope} onChange={setScope}
              options={[{ value: "", label: "Mọi phạm vi" }, ...scopes.map((value) => ({ value, label: SCOPE_TEXT[value] || value }))]} />
          </div>
          <div className="cand-filter-summary">
            <span className="rc-note" role="status">Hiển thị {matching.length} / {candidates.length} báo cáo</span>
            {filtered && <button type="button" className="cand-clear" onClick={clearFilters}>Xoá bộ lọc</button>}
          </div>
          {!matching.length && <p className="cand-empty">Không có báo cáo khớp bộ lọc trong danh sách này. Thử xoá bộ lọc hoặc tìm lại trên CafeF.</p>}
          <ul className="cand-list cand-scroll" aria-label="Danh sách báo cáo tìm thấy" tabIndex={0}>
            {candidates.map((c) => (
              <CandidateRow key={c.candidate_id} candidate={c} payload={payload} actions={actions} visible={visibleIds.has(c.candidate_id)} />
            ))}
          </ul>
        </div>
      </>}
      <div className="choice-list">
        <button type="button" className="chip-btn secondary"
          onClick={() => actions.onEditSearch(payload.search_request || { subject: who })}>Tìm lại theo năm hoặc kỳ</button>
        <OverrideButton payload={payload} actions={actions} />
      </div>
    </div>
  );
}

function ClarificationCard({ payload, actions }: { payload: Record<string, any>; actions: MessageActions }) {
  const clarification = payload.clarification || {};
  const request: DiscoverySearch = payload.search_request || {};
  return (
    <div className="report-card">
      <p className="rc-title">{clarificationText(clarification.reason, clarification.subject || "")}</p>
      {clarification.more && <p className="rc-note">Còn nhiều kết quả khác; hãy nhập tên đầy đủ hơn hoặc mã CK.</p>}
      <div className="choice-list">
        {(clarification.choices || []).map((choice: { ticker: string; company: string; exchange: string }) => (
          <ActionButton
            key={choice.ticker}
            label={[choice.ticker, choice.company, choice.exchange].filter(Boolean).join(" · ")}
            onRun={() => actions.onDiscover({ ...request, ticker: choice.ticker })}
          />
        ))}
        <button type="button" className="chip-btn secondary" onClick={() => actions.onEditSearch(request)}>
          Sửa thông tin tìm kiếm
        </button>
        <OverrideButton payload={payload} actions={actions} />
      </div>
    </div>
  );
}

function SearchErrorCard({ payload, actions }: { payload: Record<string, any>; actions: MessageActions }) {
  return (
    <div className="report-card is-error">
      <p className="rc-title">{payload.error?.message || "Chưa tìm được báo cáo trên CafeF."}</p>
      <div className="choice-list">
        <ActionButton label="Thử lại" onRun={() => actions.onDiscover(payload.search_request || {})} />
        <button type="button" className="chip-btn secondary" onClick={actions.onOpenUpload}>Tải PDF lên</button>
        <OverrideButton payload={payload} actions={actions} />
      </div>
    </div>
  );
}

function DatasetRequiredCard({ actions }: { actions: MessageActions }) {
  return (
    <div className="report-card">
      <p className="rc-title">Cuộc trò chuyện này chưa có báo cáo nên chưa trả lời được câu hỏi tài chính.</p>
      <p className="rc-note">Hãy chọn một báo cáo có sẵn, tìm báo cáo trên CafeF hoặc tải PDF lên.</p>
      <div className="choice-list">
        <button type="button" className="chip-btn" onClick={actions.onChooseDataset}>Chọn báo cáo có sẵn</button>
        {actions.reportDiscovery && (
          <button type="button" className="chip-btn" onClick={actions.onOpenDiscovery}>Tìm trên CafeF</button>
        )}
        <button type="button" className="chip-btn secondary" onClick={actions.onOpenUpload}>Tải PDF lên</button>
      </div>
    </div>
  );
}

function BotRow({ children, time, caption, steps }: {
  children: React.ReactNode; time?: string; caption?: string; steps?: RunStep[];
}) {
  return (
    <div className="row bot">
      <span className="row-mark" aria-hidden="true"><LogoMark size={22} /></span>
      <div className="row-body">
        {steps && steps.length > 0 && <StepTrace steps={steps} />}
        {children}
        {caption && <span className="msg-caption">{caption}</span>}
        <span className="msg-time">{formatTime(time)}</span>
      </div>
    </div>
  );
}

/** One stored message. Structured payloads are rendered as text, never HTML. */
export default function MessageItem({
  message: m, caption, actions, steps,
}: { message: Message; caption?: string; actions: MessageActions; steps?: RunStep[] }) {
  const kind = m.kind || "text";
  const payload = m.payload;
  const interpreted =
    m.context_status === "resolved" && m.interpreted_question ? `Đã hiểu câu hỏi là: ${m.interpreted_question}` : caption;

  if (m.role === "user" || kind === "report_search_request") {
    return (
      <div className="row user">
        <div className="bubble user">{m.content}</div>
        <span className="msg-time">{formatTime(m.created_at)}</span>
      </div>
    );
  }
  if (kind === "context_clarification" || kind === "context_error") {
    return (
      <BotRow time={m.created_at}>
        <div className="report-card context-card"><p>{m.content || "Không làm rõ được câu hỏi."}</p></div>
      </BotRow>
    );
  }
  if (payload && kind === "report_candidates") {
    return <BotRow time={m.created_at}><CandidatesCard payload={payload} actions={actions} /></BotRow>;
  }
  if (payload && kind === "report_search_clarification") {
    return <BotRow time={m.created_at}><ClarificationCard payload={payload} actions={actions} /></BotRow>;
  }
  if (payload && kind === "report_search_error") {
    return <BotRow time={m.created_at}><SearchErrorCard payload={payload} actions={actions} /></BotRow>;
  }
  if (payload && kind === "dataset_required") {
    return <BotRow time={m.created_at}><DatasetRequiredCard actions={actions} /></BotRow>;
  }
  return (
    <BotRow time={m.created_at} caption={interpreted} steps={steps}>
      <div className="bubble bot rich"><AnswerContent text={m.content || ""} /></div>
      <ReportSources sources={m.report_sources || []} messageId={m.id} />
    </BotRow>
  );
}
