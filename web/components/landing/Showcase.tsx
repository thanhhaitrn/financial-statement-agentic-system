import Reveal from "@/components/Reveal";
import { LogoMark } from "@/components/Logo";
import { docsPage } from "@/lib/site";
import type { ReactNode } from "react";
import { INCOME_STATEMENT, MARGINS, NEWS, NEWS_ARTICLES, QUESTION, SAMPLE } from "./sample";

// Three strengths of the system, each over a painted panel with the product
// working on the real Vinamilk report (see sample.ts): one whole window, two
// overlapping windows, and one window cropped by the panel edge.

function DocIcon() {
  return (
    <svg width={14} height={14} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.4} strokeLinejoin="round" aria-hidden="true">
      <path d="M4 1.5h5.5l3 3v10H4zM9.5 1.5v3h3" />
    </svg>
  );
}

function ZoomIcon() {
  return (
    <svg width={18} height={18} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" aria-hidden="true">
      <path d="M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zM10.4 10.4 14 14M5 7h4M7 5v4" />
    </svg>
  );
}

function TitleBar({ children }: { children?: ReactNode }) {
  return <div className="sc-titlebar"><i /><i /><i />{children}</div>;
}

/** One whole window: the conversation list and an answer with its source page. */
function WholeWindow() {
  const recent = [
    ["Tuần này", ["Biên lợi nhuận gộp 2025", `Dòng tiền ${SAMPLE.shortName} 2025`, `Tin tức ${SAMPLE.shortName}`]],
    ["Tháng này", ["Nợ vay Hòa Phát 2025", "Tồn kho Nhựa Đà Nẵng", "Chi phí bán hàng 2025"]],
  ] as const;
  return (
    <div className="sc-window sc-whole">
      <TitleBar><span className="sc-address">AgentFinX · Cuộc trò chuyện</span></TitleBar>
      <div className="sc-app">
        <aside className="sc-side">
          <LogoMark size={20} />
          {recent.map(([label, items]) => (
            <div key={label}>
              <p className="sc-side-label">{label}</p>
              {items.map((item, i) => <p key={item} className={label === "Tuần này" && i === 0 ? "on" : undefined}>{item}</p>)}
            </div>
          ))}
        </aside>
        <div className="sc-thread">
          <p className="sc-topic">Biên lợi nhuận gộp 2025</p>
          <p className="sc-ask">{QUESTION}</p>
          <p className="sc-step">Đã đọc <span>2 bảng, 1 thuyết minh</span></p>
          <p className="sc-answer">
            Biên lợi nhuận gộp giảm từ 45,42% xuống 44,46% vì giá vốn tăng 6,4%, nhanh hơn doanh thu (4,6%). Đây là
            trang được trích dẫn:
          </p>
          <div className="sc-page">
            <div className="sc-page-sheet">
              <i className="w60" /><i className="w40" />
              {[0, 1, 2, 3, 4, 5, 6, 7].map((n) => <b key={n} className={n >= 2 && n <= 4 ? "hl" : undefined} />)}
            </div>
            <span className="sc-page-zoom"><ZoomIcon /></span>
            <span className="sc-page-caption">Báo cáo KQKD riêng · tr. 9</span>
          </div>
          <p className="sc-summary-title">Tóm tắt</p>
          <p className="sc-summary">Doanh thu thuần +4,6%, giá vốn +6,4%, lợi nhuận gộp chỉ +2,4%.</p>
          <p className="sc-input">Hỏi tiếp…<span>Agent ▾</span></p>
        </div>
      </div>
    </div>
  );
}

/** Two windows: the report page behind, the agent working on it in front. */
function OverlappingWindows() {
  const rows = INCOME_STATEMENT.filter((row) => ["01", "02", "10", "11", "20", "21", "22"].includes(row.code));
  const [gross] = MARGINS;
  return (
    <>
      <div className="sc-window sc-back">
        <TitleBar><span>{SAMPLE.report}</span></TitleBar>
        <div className="sc-back-body">
          <p className="sc-back-title">Báo cáo kết quả hoạt động kinh doanh riêng</p>
          <p className="sc-back-sub">Mẫu B 02 – DN · tr. 9 · VND</p>
          {rows.map((row) => (
            <p key={row.code} className={["10", "11", "20"].includes(row.code) ? "cited" : undefined}>
              <span>{row.name}</span><em>{row.code}</em><b>{row.now}</b>
            </p>
          ))}
        </div>
      </div>
      <div className="sc-window sc-front">
        <TitleBar><span>AgentFinX · Khả năng sinh lời</span></TitleBar>
        <div className="sc-front-body">
          <p className="sc-prompt">so sánh biên lợi nhuận gộp {SAMPLE.period} với {SAMPLE.previous}</p>
          <ul className="sc-trace">
            <li>Đọc <span>Báo cáo KQKD riêng · tr. 9</span></li>
            <li>Lấy <span>Doanh thu thuần · mã 10</span></li>
            <li>Lấy <span>Lợi nhuận gộp · mã 20</span></li>
            <li>Tính <span>theo công thức cố định</span></li>
          </ul>
          <div className="sc-box">
            <p className="sc-box-label">{gross.name}</p>
            <p>{gross.formula} × 100</p>
            <p><span>{SAMPLE.period}</span> 23.561,4 ÷ 52.991,5 = <b>{gross.now}</b></p>
            <p><span>{SAMPLE.previous}</span> 23.017,4 ÷ 50.676,7 = <b>{gross.before}</b></p>
          </div>
          <p className="sc-prompt sc-follow">→ Hỏi tiếp</p>
          <p className="sc-mode">◉ Agent khả năng sinh lời</p>
        </div>
      </div>
    </>
  );
}

/** One window cropped by the panel edge: the news events beside the conversation. */
function CroppedWindow() {
  return (
    <div className="sc-window sc-cropped">
      <TitleBar><span className="sc-title-centre">AgentFinX</span></TitleBar>
      <div className="sc-split">
        <div className="sc-chat">
          <p className="sc-topic">Tin tức {SAMPLE.shortName}</p>
          <p className="sc-ask">Gần đây có tin gì về {SAMPLE.shortName}?</p>
          <ul className="sc-steps">
            <li>Tìm <span>trên Vietstock · {NEWS_ARTICLES.length} bài</span></li>
            <li>Gom <span>thành sự kiện</span></li>
            <li>Kiểm tra <span>trích dẫn nguyên văn</span></li>
          </ul>
          <div className="sc-card"><DocIcon /><b>Tin doanh nghiệp</b><span>{SAMPLE.ticker} · {NEWS_ARTICLES.length} bài</span></div>
          <p className="sc-answer">
            Có {NEWS_ARTICLES.length} bài từ 30/09 đến 06/10/2026; đáng chú ý là {SAMPLE.shortName} được vinh danh tại IR
            Awards 2026.
          </p>
          <p className="sc-input">Hỏi tiếp…</p>
        </div>
        <div className="sc-doc">
          <div className="sc-tabs"><span className="on">Tin doanh nghiệp <i>×</i></span></div>
          <p className="sc-crumb">{SAMPLE.ticker} › Sự kiện</p>
          <h4>{NEWS.event}</h4>
          <p className="sc-level">Mức xác nhận: {NEWS.confirmation}</p>
          <blockquote className="sc-quote">“{NEWS.quote}…”</blockquote>
          <p className="sc-sub">Bài viết ({NEWS_ARTICLES.length})</p>
          <ul className="sc-articles">
            {NEWS_ARTICLES.map((a) => (
              <li key={a.title}><span>{a.title}</span><em>{NEWS.publisher} · {a.date}</em></li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

const ROWS = [
  {
    title: "Kiểm chứng đến từng con số",
    body: "Mọi kết luận dẫn về đúng bảng, thuyết minh và trang trong báo cáo gốc để bạn đối chiếu.",
    link: docsPage("architecture"),
    panel: "sc-whole-panel paint-valley",
    content: <WholeWindow />,
  },
  {
    title: "Tính toán minh bạch",
    body: "Mỗi chỉ số kèm công thức và số liệu đầu vào, tính trên cùng cơ sở giữa các kỳ.",
    link: docsPage("architecture"),
    panel: "sc-stack-panel paint-grey",
    content: <OverlappingWindows />,
  },
  {
    title: "Theo sát tin doanh nghiệp",
    body: "Tin Vietstock được gom thành sự kiện, có nhãn tích cực hoặc tiêu cực, kèm trích dẫn nguyên văn và mức xác nhận.",
    link: docsPage("architecture"),
    panel: "sc-crop-panel paint-sunset",
    content: <CroppedWindow />,
  },
];

export default function Showcase() {
  return (
    <div className="sc">
      {ROWS.map((row) => (
        <Reveal key={row.title} className="sc-row">
          <div className="sc-copy">
            <h3>{row.title}</h3>
            <p>{row.body}</p>
            <a href={row.link} target="_blank" rel="noopener noreferrer">Tìm hiểu thêm <span aria-hidden="true">→</span></a>
          </div>
          <div className={`sc-panel ${row.panel}`} aria-hidden="true">{row.content}</div>
        </Reveal>
      ))}
    </div>
  );
}
