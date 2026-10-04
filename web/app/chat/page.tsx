"use client";

import PageLink from "@/components/PageLink";
import { useRouter } from "next/navigation";
import { go } from "@/lib/transition";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import Modal from "@/components/Modal";
import { LogoMark } from "@/components/Logo";
import { BRAND, INITIAL_CHATS } from "@/lib/site";

type Message = { id: number; role: "user" | "bot"; text: string };
type Chat = { id: number; title: string };
type ModalState =
  | { type: "upload" }
  | { type: "search" }
  | { type: "share" }
  | { type: "rename"; id: number }
  | { type: "delete"; id: number }
  | null;

export default function ChatPage() {
  const [chats, setChats] = useState<Chat[]>(INITIAL_CHATS);
  const [activeId, setActiveId] = useState<number>(INITIAL_CHATS[0].id);
  const [threads, setThreads] = useState<Record<number, Message[]>>({});
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [pending, setPending] = useState<File[]>([]);
  const [typing, setTyping] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [menuFor, setMenuFor] = useState<number | null>(null);
  const [modal, setModal] = useState<ModalState>(null);
  const [text, setText] = useState("");
  const [toast, setToast] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const [user, setUser] = useState<{ name: string; email: string } | null>(null);

  useEffect(() => {
    fetch("/api/auth/me").then((r) => (r.ok ? r.json() : null)).then((d) => d && setUser(d.user));
  }, []);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.refresh();
    go(router, "/login", true);
  }

  const messages = threads[activeId] ?? [];

  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [messages.length, typing]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 2200);
    return () => clearTimeout(t);
  }, [toast]);
  useEffect(() => {
    const close = () => setMenuFor(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, []);

  function send(value: string) {
    const v = value.trim();
    if (!v || typing) return;
    const id = Date.now();
    setThreads((t) => ({ ...t, [activeId]: [...(t[activeId] ?? []), { id, role: "user", text: v }] }));
    setInput("");
    setTyping(true);
    // TODO: thay bằng lời gọi API phân tích thật
    setTimeout(() => {
      const note = files.length ? ` (kèm ${files.length} tệp)` : "";
      setThreads((t) => ({
        ...t,
        [activeId]: [...(t[activeId] ?? []), { id: id + 1, role: "bot", text: `Đây là phản hồi mẫu cho: "${v}"${note}. Hãy nối API thật để nhận phân tích.` }],
      }));
      setTyping(false);
    }, 900);
  }

  function newChat() {
    const id = Date.now();
    setChats((c) => [{ id, title: "Cuộc trò chuyện mới" }, ...c]);
    setActiveId(id);
    setFiles([]);
  }

  function openModal(m: ModalState, initial = "") {
    setText(initial);
    setModal(m);
    setMenuFor(null);
  }

  function confirmRename(id: number) {
    const v = text.trim();
    if (v) setChats((c) => c.map((x) => (x.id === id ? { ...x, title: v } : x)));
    setModal(null);
    setToast("Đã đổi tên cuộc trò chuyện");
  }

  function confirmDelete(id: number) {
    const rest = chats.filter((c) => c.id !== id);
    setChats(rest);
    if (id === activeId && rest[0]) setActiveId(rest[0].id);
    setModal(null);
    setToast("Đã xóa cuộc trò chuyện");
  }

  function confirmUpload() {
    setFiles((f) => [...f, ...pending]);
    setToast(`Đã thêm ${pending.length} tệp`);
    setPending([]);
    setModal(null);
  }

  const results = chats.filter((c) => c.title.toLowerCase().includes(text.toLowerCase()));
  const target = modal && "id" in modal ? chats.find((c) => c.id === modal.id) : undefined;

  return (
    <div className={`chat ${collapsed ? "is-collapsed" : ""}`}>
      <aside className="chat-side">
        <div className="side-head">
          <PageLink href="/" className="logo"><LogoMark /><span>{BRAND}</span></PageLink>
          <button className="icon-btn" onClick={() => setCollapsed(true)} aria-label="Thu gọn" title="Thu gọn">‹</button>
        </div>
        <div className="side-actions">
          <button className="btn btn-outline btn-block" onClick={newChat}>+ Cuộc trò chuyện mới</button>
          <button className="btn btn-ghost btn-block left" onClick={() => openModal({ type: "search" })}>Tìm kiếm</button>
        </div>
        <p className="side-label">Gần đây</p>
        <div className="side-list">
          {chats.map((c) => (
            <div key={c.id} className={`history ${c.id === activeId ? "active" : ""}`}>
              <button className="history-title" onClick={() => setActiveId(c.id)}>{c.title}</button>
              <button className="icon-btn dots-btn" aria-label="Tùy chọn" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === c.id ? null : c.id); }}>⋯</button>
              {menuFor === c.id && (
                <div className="dropdown" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => openModal({ type: "rename", id: c.id }, c.title)}>Đổi tên</button>
                  <button onClick={() => openModal({ type: "share" })}>Chia sẻ</button>
                  <button className="danger" onClick={() => openModal({ type: "delete", id: c.id })}>Xóa</button>
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="side-user">
          <span className="avatar">{(user?.name ?? "U").charAt(0).toUpperCase()}</span>
          <span className="grow ellipsis">{user?.name ?? "Tài khoản"}</span>
          <button className="icon-btn" title="Đăng xuất" aria-label="Đăng xuất" onClick={logout}>⎋</button>
        </div>
      </aside>

      <section className="chat-main">
        <header className="chat-head">
          <div className="head-left">
            {collapsed && <button className="icon-btn" onClick={() => setCollapsed(false)} aria-label="Mở thanh bên">☰</button>}
            <span className="head-title">{BRAND}</span>
          </div>
          <button className="btn btn-ghost" onClick={() => openModal({ type: "share" })}>Chia sẻ</button>
        </header>

        <div className="chat-scroll">
          {messages.length === 0 ? (
            <div className="chat-empty">
              <LogoMark size={44} />
              <h2>Chào bạn, bạn muốn phân tích tài liệu nào hôm nay?</h2>
              <p className="muted">Tải lên báo cáo tài chính hoặc đặt câu hỏi để bắt đầu.</p>
            </div>
          ) : (
            <div className="chat-list">
              {messages.map((m) => <div key={m.id} className={`bubble ${m.role}`}>{m.text}</div>)}
              {typing && <div className="bubble bot dots"><i /><i /><i /></div>}
              <div ref={endRef} />
            </div>
          )}
        </div>

        <form className="composer" onSubmit={(e: FormEvent) => { e.preventDefault(); send(input); }}>
          {files.length > 0 && (
            <div className="file-chips">
              {files.map((f, i) => (
                <span key={i} className="file-chip">{f.name}
                  <button type="button" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="Bỏ tệp">✕</button>
                </span>
              ))}
            </div>
          )}
          <div className="composer-row">
            <button type="button" className="icon-btn" title="Tải tệp lên" aria-label="Tải tệp lên" onClick={() => openModal({ type: "upload" })}>＋</button>
            <textarea
              value={input}
              rows={1}
              placeholder="Đặt câu hỏi về báo cáo tài chính..."
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
              }}
            />
            <button type="submit" className="send" disabled={!input.trim() || typing} aria-label="Gửi">↑</button>
          </div>
        </form>
      </section>

      {modal?.type === "upload" && (
        <Modal title="Tải tài liệu lên" onClose={() => { setPending([]); setModal(null); }}
          footer={<>
            <button className="btn btn-outline" onClick={() => { setPending([]); setModal(null); }}>Hủy</button>
            <button className="btn btn-dark" disabled={!pending.length} onClick={confirmUpload}>Tải lên</button>
          </>}>
          <label className="dropzone">
            <input type="file" multiple accept=".pdf,.xlsx,.xls,.csv,.docx" hidden
              onChange={(e) => setPending(Array.from(e.target.files ?? []))} />
            <strong>Chọn tệp để tải lên</strong>
            <span className="muted">PDF, Excel, CSV hoặc Word</span>
          </label>
          {pending.map((f, i) => (
            <div key={i} className="file-row"><span>{f.name}</span><span className="muted">{(f.size / 1024).toFixed(0)} KB</span></div>
          ))}
        </Modal>
      )}

      {modal?.type === "rename" && (
        <Modal title="Đổi tên cuộc trò chuyện" onClose={() => setModal(null)}
          footer={<>
            <button className="btn btn-outline" onClick={() => setModal(null)}>Hủy</button>
            <button className="btn btn-dark" onClick={() => confirmRename(modal.id)}>Lưu</button>
          </>}>
          <input className="field" autoFocus value={text} onChange={(e) => setText(e.target.value)} />
        </Modal>
      )}

      {modal?.type === "delete" && (
        <Modal title="Xóa cuộc trò chuyện?" onClose={() => setModal(null)}
          footer={<>
            <button className="btn btn-outline" onClick={() => setModal(null)}>Hủy</button>
            <button className="btn btn-danger" onClick={() => confirmDelete(modal.id)}>Xóa</button>
          </>}>
          <p className="muted">Bạn sắp xóa &quot;{target?.title}&quot;. Hành động này không thể hoàn tác.</p>
        </Modal>
      )}

      {modal?.type === "share" && (
        <Modal title="Chia sẻ cuộc trò chuyện" onClose={() => setModal(null)}>
          <div className="share-row">
            <input className="field" readOnly value={`https://agentfinx.app/share/${activeId}`} />
            <button className="btn btn-dark" onClick={() => {
              navigator.clipboard?.writeText(`https://agentfinx.app/share/${activeId}`);
              setModal(null); setToast("Đã sao chép liên kết");
            }}>Sao chép</button>
          </div>
        </Modal>
      )}

      {modal?.type === "search" && (
        <Modal title="Tìm kiếm" onClose={() => setModal(null)}>
          <input className="field" autoFocus placeholder="Tìm cuộc trò chuyện..." value={text} onChange={(e) => setText(e.target.value)} />
          <div className="search-list">
            {results.length === 0 && <p className="muted">Không có kết quả.</p>}
            {results.map((c) => (
              <button key={c.id} className="search-item" onClick={() => { setActiveId(c.id); setModal(null); }}>{c.title}</button>
            ))}
          </div>
        </Modal>
      )}

      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
