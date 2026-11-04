"use client";

import { useEffect, useRef, useState } from "react";
import Modal from "@/components/Modal";
import { Icon } from "@/components/chat/Icons";
import { api } from "@/lib/api";

type Agent = { id: string; label: string; description: string };
type Config = { agents: Agent[]; prompts: Record<string, string>; max_prompt_length: number };

const SHORT_LABELS: Record<string, string> = {
  agent_planner: "Lập kế hoạch",
  agent_router: "Điều phối",
  agent_profitability: "Sinh lời",
  agent_liquidity_solvency: "Thanh khoản",
  agent_cashflow_analysis: "Dòng tiền",
  agent_efficiency: "Hiệu quả",
  agent_synth: "Tổng hợp",
  news_select: "Chọn và gom tin",
  news_score: "Phân tích tin",
};
const EXAMPLES: Record<string, string> = {
  agent_planner: "Ví dụ: Với câu hỏi đánh giá tổng quan, ưu tiên xem xét rủi ro tài chính và so sánh với kỳ trước.",
  agent_router: "Ví dụ: Ưu tiên tìm số liệu của hai kỳ gần nhất và các thuyết minh liên quan đến khoản mục đang hỏi.",
  agent_profitability: "Ví dụ: Tập trung vào biên lợi nhuận ròng, ROE và chất lượng lợi nhuận. Giải thích các biến động lớn.",
  agent_liquidity_solvency: "Ví dụ: Chú ý nợ vay ngắn hạn, khả năng trả lãi và rủi ro mất cân đối nguồn vốn.",
  agent_cashflow_analysis: "Ví dụ: Ưu tiên so sánh dòng tiền kinh doanh với lợi nhuận, làm rõ dấu hiệu lợi nhuận chưa chuyển thành tiền.",
  agent_efficiency: "Ví dụ: Tập trung vào vòng quay tồn kho, khoản phải thu và hiệu quả sử dụng tài sản.",
  agent_synth: "Ví dụ: Trả lời ngắn gọn bằng tiếng Việt. Mở đầu bằng 3 nhận định chính, sau đó nêu số liệu và rủi ro cần chú ý.",
  news_select: "Ví dụ: Ưu tiên tin về kết quả kinh doanh, thay đổi ban lãnh đạo và các dự án đầu tư lớn.",
  news_score: "Ví dụ: Nêu rõ tác động có thể quan sát từ tin tức, trích nguồn và chỉ ra những điểm chưa được xác nhận.",
};
const ANALYSTS = ["agent_profitability", "agent_liquidity_solvency", "agent_cashflow_analysis", "agent_efficiency"];

/** One line icon per agent (16px, stroke = currentColor), so each node reads at a glance. */
const AGENT_ICONS: Record<string, React.ReactNode> = {
  agent_planner: <><circle cx="8" cy="8" r="6" /><path d="M10.5 5.5 9 9 5.5 10.5 7 7z" /></>,
  agent_router: <><path d="M3 8h4m0 0 3-3.5H13M7 8l3 3.5H13" /><path d="m11.5 3 1.5 1.5L11.5 6M11.5 10l1.5 1.5-1.5 1.5" /></>,
  agent_profitability: <><path d="M2.5 12 6 8.5l2.5 2.5L13.5 6" /><path d="M10 6h3.5v3.5" /></>,
  agent_liquidity_solvency: <><path d="M8 2.5 13 4.5v3.5c0 3-2.2 5-5 5.5-2.8-.5-5-2.5-5-5.5V4.5z" /><path d="m6 8 1.5 1.5L10.5 6.5" /></>,
  agent_cashflow_analysis: <><path d="M3 6h9l-2.5-2.5M13 10H4l2.5 2.5" /></>,
  agent_efficiency: <><path d="M3 11a5 5 0 1 1 10 0" /><path d="M8 11 10.5 7.5" /></>,
  news_select: <><path d="M2.5 3.5h11l-4.2 5v4l-2.6-1.3V8.5z" /></>,
  news_score: <><rect x="2.5" y="2.5" width="8" height="11" rx="1" /><path d="M4.5 5.5h4M4.5 8h2.5" /><circle cx="11" cy="10.5" r="2" /><path d="m12.5 12 1.5 1.5" /></>,
  agent_synth: <><path d="M3 13h3l7-7-3-3-7 7z" /><path d="m9 4 3 3" /></>,
};

function AgentIcon({ id }: { id: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{AGENT_ICONS[id]}</svg>
  );
}

const Conn = () => <div className="flow-conn" aria-hidden="true" />;

function same(left: Record<string, string>, right: Record<string, string>) {
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every((key) => left[key] === right[key]);
}

export default function AgentPromptsModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [config, setConfig] = useState<Config | null>(null);
  const [prompts, setPrompts] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState("agent_planner");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [confirmClose, setConfirmClose] = useState(false);
  const editorPanel = useRef<HTMLElement>(null);
  const workflowPanel = useRef<HTMLElement>(null);

  useEffect(() => {
    let live = true;
    api<Config>("/api/me/agent-prompts").then((data) => {
      if (!live) return;
      setConfig(data);
      setPrompts(data.prompts);
      setSaved(data.prompts);
    }).catch((err) => { if (live) setError(err.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);

  const agent = config?.agents.find((item) => item.id === selected);
  const value = prompts[selected] ?? "";
  const dirty = !same(prompts, saved);
  const invalid = Object.values(prompts).some((text) => !text.trim() || text.length > (config?.max_prompt_length ?? 60000));
  const count = Object.keys(prompts).length;

  function close() {
    if (busy) return;
    if (dirty) setConfirmClose(true);
    else onClose();
  }

  function edit(text: string) {
    setPrompts((previous) => {
      const next = { ...previous };
      if (!text.trim()) delete next[selected];
      else next[selected] = text;
      return next;
    });
    setConfirmClose(false);
  }

  function choose(id: string) {
    setSelected(id);
    if (window.matchMedia("(max-width: 700px)").matches) {
      requestAnimationFrame(() => editorPanel.current?.scrollIntoView({ block: "start", behavior: "instant" }));
    }
  }

  function node(id: string) {
    const item = config?.agents.find((entry) => entry.id === id);
    if (!item) return null;
    const configured = prompts[id] !== undefined;
    return (
      <button key={id} type="button" className={`agent-node${selected === id ? " selected" : ""}`}
        aria-pressed={selected === id} aria-label={item.label} aria-controls="agent-preference-panel"
        data-agent-id={id} disabled={busy} onClick={() => choose(id)}>
        <span className="agent-node-icon"><AgentIcon id={id} /></span>
        <span className="agent-node-label">{SHORT_LABELS[id]}</span>
        {configured && <span className="agent-node-dot" title="Đã cá nhân hoá" aria-label="Đã cá nhân hoá" />}
      </button>
    );
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api("/api/me/agent-prompts", { method: "PUT", body: { prompts } });
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không lưu được yêu cầu cá nhân hoá.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Cá nhân hoá agents" className="agent-personalization" onClose={close} footer={
      <>
        <span className="personal-save-note muted small">Áp dụng từ câu hỏi tiếp theo</span>
        <button className="btn btn-outline" onClick={close} disabled={busy}>Đóng</button>
        <button className="btn btn-dark" onClick={save} disabled={loading || busy || !config || !dirty || invalid}>
          {busy ? "Đang lưu…" : "Lưu thay đổi"}
        </button>
      </>
    }>
      <p className="muted small">Chọn một agent trong workflow và cho biết bạn muốn agent đó làm gì.</p>
      {loading && <p role="status">Đang tải cấu hình…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {config && agent && <div className="personal-layout">
        <section ref={workflowPanel} className="personal-workflow" aria-label="Workflow agents">
          <div className="personal-section-head"><span>Workflow</span><span className="muted small">{count ? `${count} agent đã cá nhân hoá` : "Bấm vào agent để bắt đầu"}</span></div>
          <div className="agent-flow">
            <div className="flow-fixed">Câu hỏi của bạn</div>
            <Conn />
            <div className="flow-single">{node("agent_planner")}</div>
            <Conn />
            <div className="flow-single">{node("agent_router")}</div>
            <Conn />
            <div className="flow-fixed"><Icon name="search" size={13} /> Truy xuất bằng chứng</div>
            <Conn />
            <div className="flow-branches">
              <div className="flow-group">
                <span className="flow-group-head">Phân tích báo cáo</span>
                <div className="flow-analysts">{ANALYSTS.map(node)}</div>
              </div>
              <div className="flow-group flow-news">
                <span className="flow-group-head">Tin doanh nghiệp</span>
                {node("news_select")}
                <div className="flow-conn short" aria-hidden="true" />
                {node("news_score")}
              </div>
            </div>
            <Conn />
            <div className="flow-single">{node("agent_synth")}</div>
            <Conn />
            <div className="flow-fixed">Câu trả lời</div>
          </div>
          <p className="muted small flow-note">Các nhánh được chọn tuỳ theo câu hỏi và dữ liệu có sẵn.</p>
        </section>
        <section ref={editorPanel} id="agent-preference-panel" className="personal-editor" aria-labelledby="personal-agent-title">
          <button type="button" className="personal-back" onClick={() => workflowPanel.current?.scrollIntoView({ block: "start", behavior: "instant" })}>← Chọn agent khác</button>
          <span className="personal-eyebrow">AGENT ĐANG CHỌN</span>
          <h4 id="personal-agent-title">
            <span className="personal-title-icon"><AgentIcon id={selected} /></span>
            {agent.label}
            {saved[selected] !== undefined && <span className="personal-badge">Đã cá nhân hoá</span>}
          </h4>
          <p className="muted small">{agent.description}</p>
          <label className="fl" htmlFor="agent-preference-input">Bạn muốn agent này làm gì?
            <textarea id="agent-preference-input" className="field personal-input" value={value}
              maxLength={config.max_prompt_length} disabled={busy} placeholder={EXAMPLES[selected]}
              onChange={(event) => edit(event.target.value)} />
          </label>
          <div className="personal-input-foot">
            <span className="muted small">{value.length.toLocaleString("vi-VN")} ký tự</span>
            {!value && EXAMPLES[selected] && (
              <button className="personal-example" disabled={busy}
                onClick={() => edit(EXAMPLES[selected].replace(/^Ví dụ:\s*/, ""))}>Dùng ví dụ</button>
            )}
            <button className="personal-clear" disabled={busy || !value} onClick={() => edit("")}>Xoá yêu cầu</button>
          </div>
          <div className="personal-hint"><Icon name="compose" size={16} /><p>Yêu cầu của bạn sẽ được ưu tiên bổ sung vào hướng dẫn của agent. Chỉ áp dụng cho tài khoản của bạn.</p></div>
        </section>
      </div>}
      {confirmClose && <div className="prompt-discard" role="alert">
        <p>Bạn có thay đổi chưa lưu.</p>
        <button className="btn btn-outline" onClick={() => setConfirmClose(false)}>Tiếp tục chỉnh sửa</button>
        <button className="btn btn-outline" onClick={onClose}>Bỏ thay đổi và đóng</button>
      </div>}
    </Modal>
  );
}
