"use client";

import { ReactNode, useEffect, useRef } from "react";

type Props = { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean };

export default function Modal({ title, onClose, children, footer, wide = false }: Props) {
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Move focus into the dialog, and give it back to the trigger on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = dialog.current?.querySelector<HTMLElement>(
      "[autofocus], input:not([type=hidden]):not([hidden]), select, textarea, button",
    );
    first?.focus();
    return () => {
      previous?.focus?.();
    };
  }, []);

  return (
    <div className="overlay" onClick={onClose}>
      <div
        ref={dialog}
        className={`modal${wide ? " wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="icon-btn" onClick={onClose} aria-label="Đóng">✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
