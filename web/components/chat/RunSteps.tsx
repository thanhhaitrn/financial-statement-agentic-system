"use client";

import { useEffect, useState } from "react";
import type { RunStep } from "@/lib/types";
import Markdown from "./Markdown";

/** Seconds between two server timestamps, or null when one is missing. */
function seconds(from?: string | null, to?: string | null): number | null {
  if (!from || !to) return null;
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) && ms >= 0 ? ms / 1000 : null;
}

function fmt(s: number): string {
  return s < 10 ? `${s.toFixed(1)} giây` : `${Math.round(s)} giây`;
}

function StepIcon({ status }: { status: RunStep["status"] }) {
  if (status === "running") return <span className="step-icon step-pulse" aria-hidden="true" />;
  const path = status === "done" ? "M3.5 8.5 6.5 11.5 12.5 4.5" : status === "failed" ? "M4.5 4.5l7 7m0-7-7 7" : "";
  return (
    <svg className="step-icon" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {path ? <path d={path} /> : <circle cx="8" cy="8" r="2.5" />}
    </svg>
  );
}

function StepRow({ step, live }: { step: RunStep; live: boolean }) {
  const [open, setOpen] = useState(false);
  // In a finished run nothing spins: a step left open counts as done.
  const status = !live && step.status === "running" ? "skipped" : step.status;
  const took = seconds(step.started_at, step.finished_at);
  const output = status === "done" ? (step.output || "").trim() : "";
  const head = (
    <>
      <StepIcon status={status} />
      <span className="step-text">
        <span className="step-label">{step.label}</span>
        {step.detail && <span className="step-detail">{step.detail}</span>}
      </span>
      {took !== null && status !== "running" && <span className="step-time">{fmt(took)}</span>}
      {output && <span className="step-caret" aria-hidden="true" />}
    </>
  );
  return (
    <li className={`step step-${status}${open ? " open" : ""}`}>
      {output ? (
        <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {head}
        </button>
      ) : (
        <div className="step-head">{head}</div>
      )}
      {output && open && (
        <div className="step-output md">
          <Markdown text={output} />
        </div>
      )}
    </li>
  );
}

/** The steps of the run being answered, updated by each poll. */
export function LiveSteps({ steps }: { steps: RunStep[] }) {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const current = [...steps].reverse().find((s) => s.status === "running");
  return (
    <div className="steps live" role="status" aria-live="polite">
      <div className="steps-head">
        <span className="shimmer">{current ? current.label : "Đang xử lý"}…</span>
        <span className="steps-elapsed">{Math.floor((now - started) / 1000)} giây</span>
      </div>
      <ol className="steps-list">
        {steps.map((s) => <StepRow key={s.key} step={s} live />)}
      </ol>
    </div>
  );
}

/** A finished run's steps, folded above its answer. */
export function StepTrace({ steps }: { steps: RunStep[] }) {
  const [open, setOpen] = useState(false);
  if (!steps.length) return null;
  const first = steps[0]?.started_at;
  const last = steps.map((s) => s.finished_at || s.started_at).sort().at(-1);
  const total = seconds(first, last);
  return (
    <div className={`steps trace${open ? " open" : ""}`}>
      <button type="button" className="steps-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="steps-caret" aria-hidden="true" />
        Đã thực hiện {steps.length} bước{total !== null ? ` · ${fmt(total)}` : ""}
      </button>
      {open && (
        <ol className="steps-list">
          {steps.map((s) => <StepRow key={s.key} step={s} live={false} />)}
        </ol>
      )}
    </div>
  );
}

/** Before the first step arrives (sending, queued): the same quiet line. */
export function PendingLine({ text }: { text: string }) {
  return (
    <div className="steps live" role="status" aria-live="polite">
      <div className="steps-head"><span className="shimmer">{text}</span></div>
    </div>
  );
}
