import { LogoMark } from "@/components/Logo";
import { ACTIVITY, METRICS, QUESTION, SAMPLE, SOURCES } from "./sample";

// Small glyphs drawn for this window; 14 px, stroke follows the text colour.
function Glyph({ name }: { name: "overview" | "reports" | "alerts" | "agent" | "company" | "sector" }) {
  const common = { width: 14, height: 14, viewBox: "0 0 14 14", fill: "none", stroke: "currentColor", strokeWidth: 1.3, "aria-hidden": true };
  switch (name) {
    case "overview":
      return <svg {...common}><rect x="1.5" y="1.5" width="4.5" height="4.5" rx="1" /><rect x="8" y="1.5" width="4.5" height="4.5" rx="1" /><rect x="1.5" y="8" width="4.5" height="4.5" rx="1" /><rect x="8" y="8" width="4.5" height="4.5" rx="1" /></svg>;
    case "reports":
      return <svg {...common}><path d="M3 1.5h5.5L11 4v8.5H3z" /><path d="M5 7h4M5 9.5h4" /></svg>;
    case "alerts":
      return <svg {...common}><path d="M7 1.5 12.5 11H1.5z" /><path d="M7 5.5v2.5M7 9.6v.2" /></svg>;
    case "agent":
      return <svg {...common}><path d="M7 1.2 12 4.1v5.8L7 12.8 2 9.9V4.1z" /><path d="M5.3 6h3.4v2.2H5.3z" /></svg>;
    case "company":
      return <svg {...common}><path d="M2 12.5V4l4-2v10.5M6 5.5h6v7" /><path d="M8.2 8h1.6M8.2 10h1.6" /></svg>;
    default:
      return <svg {...common}><path d="M1.5 12.5h11M3 12.5V7h2v5.5M6.5 12.5V4h2v8.5M10 12.5V9h1.5v3.5" /></svg>;
  }
}

function Sidebar() {
  return (
    <aside className="pd-side" aria-hidden="true">
      <div className="pd-team"><LogoMark size={18} /><span>Nhóm phân tích</span></div>
      <nav className="pd-nav">
        <span><Glyph name="overview" />Tổng quan</span>
        <span className="on"><Glyph name="reports" />Báo cáo đã tải</span>
        <span><Glyph name="alerts" />Cảnh báo<b className="pd-count">2</b></span>
        <span><Glyph name="agent" />Agent</span>
      </nav>
      <p className="pd-label">Không gian</p>
      <nav className="pd-nav">
        <span><Glyph name="company" />Công ty theo dõi</span>
        <span className="pd-sub"><i className="mono">{SAMPLE.ticker}</i>{SAMPLE.company}</span>
        <span><Glyph name="sector" />Ngành</span>
        <span className="pd-sub"><i className="mono">F&amp;B</i>Thực phẩm, đồ uống</span>
      </nav>
    </aside>
  );
}

function Report() {
  return (
    <div className="pd-main">
      <div className="pd-bar">
        <span className="muted">Báo cáo đã tải</span>
        <span className="muted">/</span>
        <span className="pd-crumb">{SAMPLE.report}</span>
        <span className="pd-sample">Dữ liệu minh họa</span>
      </div>
      <div className="pd-body">
        <p className="pd-ticker mono">{SAMPLE.ticker}</p>
        <h3 className="pd-title">{SAMPLE.report}</h3>
        <p className="pd-detail muted">{SAMPLE.company} · {SAMPLE.reportDetail}</p>
        <div className="pd-metrics">
          {METRICS.map((m) => (
            <div key={m.label} className="pd-metric">
              <span className="muted">{m.label}</span>
              <b className="mono">{m.value}<small>{m.unit}</small></b>
              <em className="mono">{m.direction === "up" ? "▲" : "▼"} {m.change} <span className="muted">so với {SAMPLE.previous}</span></em>
            </div>
          ))}
        </div>
        <p className="pd-section">Hoạt động</p>
        <ol className="pd-activity">
          {ACTIVITY.map((a) => (
            <li key={a.step}>
              <i aria-hidden="true" />
              <span><b>{a.step}</b><span className="muted">{a.detail}</span></span>
              <time className="mono muted">{a.time}</time>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function Chat() {
  return (
    <div className="pd-chat">
      <div className="pd-chat-head">
        <span><LogoMark size={16} /> Hỏi agent</span>
        <span className="mono muted">{SAMPLE.ticker} · {SAMPLE.period}</span>
      </div>
      <p className="pd-ask">{QUESTION}</p>
      <div className="pd-answer">
        <p>
          Biên lợi nhuận gộp {SAMPLE.period} là <b className="mono">21,6%</b>, giảm 2,3 điểm so với{" "}
          <span className="mono">23,9%</span> cùng kỳ. Giá vốn hàng bán tăng <span className="mono">11,7%</span> lên{" "}
          <span className="mono">978,4</span> tỷ đồng, nhanh hơn mức tăng <span className="mono">8,4%</span> của doanh
          thu, chủ yếu do chi phí nguyên vật liệu tăng.
        </p>
        <div className="pd-sources">
          {SOURCES.map((source) => <span key={source} className="mono">{source}</span>)}
        </div>
      </div>
      <div className="pd-input"><span className="muted">Hỏi thêm về báo cáo này…</span><i aria-hidden="true">↑</i></div>
    </div>
  );
}

/** The product shown over a landscape, as one static illustration. */
export default function ProductDemo() {
  return (
    <figure className="pd-stage">
      <div className="pd-canvas">
        <div className="pd-window" role="img" aria-label={`Giao diện AgentFinX mở ${SAMPLE.report}, dữ liệu minh họa`}>
          <Sidebar />
          <Report />
        </div>
        <Chat />
      </div>
      <figcaption className="muted">
        {SAMPLE.company} ({SAMPLE.ticker}) và mọi số liệu trong khung trên là dữ liệu minh họa.
      </figcaption>
    </figure>
  );
}
