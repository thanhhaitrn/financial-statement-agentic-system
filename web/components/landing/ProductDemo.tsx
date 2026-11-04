import { LogoMark } from "@/components/Logo";
import { INCOME_STATEMENT, QUESTION, SAMPLE, SOURCES } from "./sample";

// A static picture of AgentFinX answering on a real report (see sample.ts):
// the report fills the main window; the agent's window sits on top of it.

// Line icons drawn for this window on a 16 px grid; the stroke follows the text colour.
const ICONS = {
  search: "M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zM10.4 10.4 14 14",
  compose: "M7 2.5H3.5v10h10V9M12.2 2.3l1.5 1.5L8 9.5 6 10l.5-2z",
  chat: "M2.5 3.5h11V11H7l-3 2.5V11H2.5z",
  report: "M4 1.5h5.5l3 3v10H4zM9.5 1.5v3h3M6.5 8.5h3.5M6.5 11h3.5",
  news: "M2.5 3h9v10.5h-7a2 2 0 0 1-2-2zM11.5 6h2v6a1.5 1.5 0 0 1-3 0M5 6h4M5 8.5h4M5 11h2.5",
  chevron: "M4.5 6.5 8 10l3.5-3.5",
  up: "M8 13V3M4 7l4-4 4 4",
  minimize: "M4 8.5h8",
  expand: "M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9.5 6.5M2.5 13.5l4-4",
  close: "M4 4l8 8M12 4l-8 8",
  zoom: "M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zM10.4 10.4 14 14M5 7h4M7 5v4",
  clip: "M10.5 5 6 9.5a1.5 1.5 0 0 0 2 2L13 6.5a3 3 0 0 0-4.2-4.2L3.5 7.5a4.5 4.5 0 0 0 6.4 6.4l3-3",
  cube: "M8 1.5l5.5 3v7L8 14.5l-5.5-3v-7zM2.5 4.5 8 7.5l5.5-3M8 7.5v7",
} as const;

function Icon({ name, size = 15 }: { name: keyof typeof ICONS; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.4}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  );
}

function Dots() {
  return (
    <svg width={15} height={15} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <circle cx="3.5" cy="8" r="1.2" /><circle cx="8" cy="8" r="1.2" /><circle cx="12.5" cy="8" r="1.2" />
    </svg>
  );
}

/** A half-filled ring: the conversation is being analysed. */
function Status({ tone = "var(--w-yellow)", size = 16 }: { tone?: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ color: tone }}>
      <circle cx="8" cy="8" r="5.6" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4.6a3.4 3.4 0 0 1 0 6.8z" fill="currentColor" />
    </svg>
  );
}

function Star() {
  return (
    <svg width={15} height={15} viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z" fill="var(--w-yellow)" />
    </svg>
  );
}

function Sidebar() {
  return (
    <aside className="pd-side">
      <div className="pd-team">
        <LogoMark size={18} />
        <span>AgentFinX</span>
        <Icon name="chevron" size={13} />
        <span className="pd-team-tools"><Icon name="search" /><i><Icon name="compose" /></i></span>
      </div>
      <nav className="pd-nav">
        <span><Icon name="report" />Báo cáo</span>
        <span><Icon name="news" />Tin doanh nghiệp</span>
      </nav>
      <p className="pd-label">Gần đây <small>▾</small></p>
      <nav className="pd-nav">
        <span className="on"><Status size={15} />Biên lợi nhuận gộp 2025</span>
        <span><Icon name="chat" />Dòng tiền {SAMPLE.shortName} 2025</span>
        <span><Icon name="news" />Tin tức {SAMPLE.shortName} tháng 9</span>
        <span><Icon name="chat" />Nợ vay Hòa Phát 2025</span>
        <span><Icon name="chat" />Tồn kho Nhựa Đà Nẵng</span>
        <span><Icon name="chat" />Chi phí bán hàng 2025</span>
      </nav>
    </aside>
  );
}

/** Page 9 of the report as printed; the rows the answer cites are highlighted. */
function Statement() {
  const cited = new Set(["10", "11", "20"]);
  const totals = new Set(["10", "20", "30", "40", "50", "60"]);
  return (
    <article className="pd-sheet">
      <div className="pd-sheet-head">
        <div>
          <p className="pd-sheet-company">{SAMPLE.company}</p>
          <h3>Báo cáo kết quả hoạt động kinh doanh riêng</h3>
          <p>cho năm kết thúc ngày 31 tháng 12 năm {SAMPLE.period}</p>
        </div>
        <p className="pd-sheet-form">Mẫu B 02 – DN</p>
      </div>
      <table className="pd-is">
        <thead>
          <tr><th /><th>Mã số</th><th>Thuyết minh</th><th>{SAMPLE.period} VND</th><th>{SAMPLE.previous} VND</th></tr>
        </thead>
        <tbody>
          {INCOME_STATEMENT.map((row) => (
            <tr
              key={row.code}
              className={[cited.has(row.code) && "cited", totals.has(row.code) && "total", row.code === "23" && "sub"].filter(Boolean).join(" ")}
            >
              <td>{row.name}</td><td>{row.code}</td><td>{row.note}</td><td>{row.now}</td><td>{row.before}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="pd-sheet-page">9</p>
    </article>
  );
}

function Chat() {
  return (
    <div className="pd-chat">
      <div className="pd-chat-head">
        <LogoMark size={16} />
        <b>AgentFinX</b>
        <span className="pd-badge">{SAMPLE.ticker} · {SAMPLE.period}</span>
        <span className="pd-chat-tools"><Icon name="minimize" /><Icon name="expand" /><Icon name="close" /></span>
      </div>
      <p className="pd-ask">{QUESTION}</p>
      <p className="pd-context"><Status tone="var(--w-muted)" size={15} /><span><b>{SAMPLE.report}</b> đã thêm vào ngữ cảnh</span></p>
      <p className="pd-worked">Các bước phân tích <small>▸</small></p>
      <p className="pd-answer">
        Biên lợi nhuận gộp năm 2025 giảm còn 44,46%, từ mức 45,42% năm 2024 (giảm 2,1%). Doanh thu thuần vẫn tăng
        4,6%, nhưng giá vốn hàng bán tăng nhanh hơn, ở mức 6,4%, nên trên mỗi đồng doanh thu công ty giữ lại được ít lợi
        nhuận gộp hơn.
      </p>
      <div className="pd-result">
        <p className="pd-result-head">
          <b>Nguồn</b> <span>{SOURCES.length} trích dẫn</span>
          <span className="pd-preview"><Icon name="zoom" size={14} />Xem</span>
        </p>
        <p className="pd-result-row"><Icon name="report" size={14} /><b>{SOURCES[0]}</b></p>
        <p className="pd-result-ref mono">{SOURCES[1]}</p>
      </div>
      <div className="pd-input">
        <span className="pd-placeholder">Đặt câu hỏi về báo cáo…</span>
        <div className="pd-input-tools">
          <span className="pd-agent"><Icon name="cube" size={14} />Agent <Icon name="chevron" size={12} /></span>
          <span className="pd-input-icons"><Icon name="clip" /><i><Icon name="up" size={14} /></i></span>
        </div>
      </div>
    </div>
  );
}

/** The report window with the agent window on top of it, as one static illustration. */
export default function ProductDemo() {
  return (
    <div className="pd-stage">
      <div
        className="pd-scene"
        role="img"
        aria-label={`Giao diện AgentFinX: báo cáo tài chính riêng năm ${SAMPLE.period} của ${SAMPLE.shortName} và cửa sổ agent trả lời câu hỏi về báo cáo`}
      >
        <div className="pd-window">
          <Sidebar />
          <section className="pd-main">
            <header className="pd-bar">
              <Icon name="report" />
              <span className="pd-id">{SAMPLE.ticker}</span>
              <b>BCTC riêng {SAMPLE.period}</b>
              <span className="pd-crumb">/ Kết quả kinh doanh · tr. 9</span>
              <Star />
              <Dots />
            </header>
            <div className="pd-content">
              <Statement />
            </div>
          </section>
        </div>
        <Chat />
      </div>
    </div>
  );
}
