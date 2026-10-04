"use client";

import { useEffect, useRef, useState } from "react";

const Q = "Biên lợi nhuận gộp quý 2 thay đổi thế nào?";
const A = "Biên lợi nhuận gộp đạt 38,4%, tăng 2,1 điểm phần trăm so với cùng kỳ, chủ yếu nhờ giá vốn nguyên liệu giảm.";
const BARS: [string, number][] = [["Q2/25", 36.3], ["Q3/25", 36.8], ["Q4/25", 37.4], ["Q1/26", 37.9], ["Q2/26", 38.4]];
const ROWS = [
  ["Doanh thu thuần", "4.812"],
  ["Giá vốn hàng bán", "(2.964)"],
  ["Lợi nhuận gộp", "1.848"],
  ["Chi phí bán hàng", "(612)"],
  ["Chi phí quản lý", "(398)"],
];

type Stage = "idle" | "think" | "answer";

export default function HeroDemo() {
  const [q, setQ] = useState("");
  const [a, setA] = useState("");
  const [stage, setStage] = useState<Stage>("idle");
  const frame = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    let raf = 0;
    const update = () => {
      const r = el.getBoundingClientRect();
      const p = Math.min(1, Math.max(0, (window.innerHeight - r.top) / (window.innerHeight * 0.95)));
      el.style.transform = `perspective(1800px) rotateX(${(1 - p) * 9}deg) scale(${0.95 + 0.05 * p})`;
      raf = 0;
    };
    const on = () => { if (!raf) raf = requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      window.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
      cancelAnimationFrame(raf);
    };
  }, []);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setQ(Q); setA(A); setStage("answer");
      return;
    }
    let dead = false;
    const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
    (async () => {
      await wait(1200);
      while (!dead) {
        setQ(""); setA(""); setStage("idle");
        await wait(700);
        for (let i = 1; i <= Q.length && !dead; i++) { setQ(Q.slice(0, i)); await wait(34); }
        await wait(300); setStage("think");
        await wait(1200); setStage("answer");
        for (let i = 1; i <= A.length && !dead; i++) { setA(A.slice(0, i)); await wait(16); }
        await wait(5200);
      }
    })();
    return () => { dead = true; };
  }, []);

  return (
    <div className="demo" ref={frame}>
      <div className="demo-bar">
        <span className="tab">BCTC_hop_nhat_Q2_2026.pdf</span>
        <span className="mono muted">AgentFinX</span>
      </div>
      <div className="demo-body">
        <div className="doc">
          <p className="mono muted">Đơn vị: tỷ đồng</p>
          <h4>Kết quả hoạt động kinh doanh</h4>
          <div className={`scan ${stage === "think" ? "on" : ""}`} />
          {ROWS.map(([k, v]) => (
            <div key={k} className={`doc-row ${k === "Lợi nhuận gộp" && stage === "answer" ? "hl" : ""}`}>
              <span>{k}</span><span className="mono">{v}</span>
            </div>
          ))}
          <div className="doc-src mono">{stage === "answer" ? "Nguồn: trang 14, mục KQKD" : "\u00a0"}</div>
        </div>
        <div className="talk">
          <div className="msg user">{q}{stage === "idle" && <i className="caret" />}</div>
          {stage === "think" && <div className="msg bot dots"><i /><i /><i /></div>}
          {stage === "answer" && (
            <div className="msg bot">
              {a}
              <div className="chart">
                {BARS.map(([l, v], i) => (
                  <div className="bar" key={l}>
                    <span className="fill" style={{ height: `${(v - 34) * 20}px`, transitionDelay: `${i * 90}ms` }} />
                    <em className="mono">{l}</em>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
