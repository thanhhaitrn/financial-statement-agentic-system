import { COMPARISON, QUESTION, SAMPLE, SOURCES } from "./sample";

// Small pieces of the real interface for the feature grid, same data as the demo.

export function CitedAnswer() {
  return (
    <div className="fv fv-answer" aria-hidden="true">
      <p className="fv-ask">{QUESTION}</p>
      <p className="fv-text">
        … giảm từ <span className="mono">23,9%</span> xuống <mark className="mono">21,6%</mark>, do giá vốn tăng nhanh hơn doanh thu.
      </p>
      <div className="fv-sources">
        {SOURCES.map((source) => <span key={source} className="mono">{source}</span>)}
      </div>
    </div>
  );
}

export function PeriodComparison() {
  return (
    <div className="fv fv-table" aria-hidden="true">
      <div className="fv-row head mono">
        <span>Tỷ đồng</span><span>{SAMPLE.period}</span><span>{SAMPLE.previous}</span><span>Thay đổi</span>
      </div>
      {COMPARISON.map((row) => (
        <div key={row.item} className="fv-row">
          <span>{row.item}</span>
          <span className="mono">{row.now}</span>
          <span className="mono muted">{row.before}</span>
          <span className="mono">{row.change}</span>
        </div>
      ))}
    </div>
  );
}

export function VerifiedNews() {
  return (
    <div className="fv fv-news" aria-hidden="true">
      <div className="fv-news-head">
        <span className="mono">{SAMPLE.ticker}</span>
        <span className="fv-level">Mức xác nhận: kế hoạch/dự báo</span>
      </div>
      <p className="fv-news-title">Khởi công dây chuyền đóng gói mới</p>
      <blockquote>“Dây chuyền dự kiến vận hành từ quý 1/2027, nâng công suất thêm 20%.”</blockquote>
      <p className="mono muted fv-news-meta">Đưa tin 05/10/2026</p>
    </div>
  );
}
