"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import PageLink from "@/components/PageLink";
import Modal from "@/components/Modal";
import { LogoMark } from "@/components/Logo";
import DiscoveryModal from "@/components/chat/DiscoveryModal";
import ImportCard from "@/components/chat/ImportCard";
import MessageItem, { MessageActions } from "@/components/chat/MessageItem";
import UploadModal, { UploadRequest } from "@/components/chat/UploadModal";
import { ApiError, api, newIdempotencyKey, postIdempotent, sleep } from "@/lib/api";
import { BRAND } from "@/lib/site";
import type {
  Candidate,
  Dataset,
  DiscoveryResult,
  DiscoverySearch,
  Features,
  Health,
  ImportJob,
  Message,
  Run,
  Session,
  User,
} from "@/lib/types";

const RUN_POLL_MS = 1500;
// Company news that is finished after the answer (server gives up after 10 min).
const NEWS_POLL_MS = 5000;
const NEWS_POLL_LIMIT_MS = 11 * 60 * 1000;
const IMPORT_POLL_MS = 2000;
const NETWORK_RETRY_MS = 3000;
const HEALTH_POLL_MS = 20000;
const SEARCH_FIELDS = [
  "subject", "ticker", "fiscal_year", "period", "fiscal_quarter", "fiscal_half", "scope", "audit_status",
] as const;

// A conversation entry: a stored message, or a local notice (an error that
// is shown once and never stored).
type Item = { key: string; message?: Message; notice?: string; caption?: string };

type ModalState =
  | { type: "upload" }
  | { type: "discovery"; prefill: DiscoverySearch }
  | { type: "search" }
  | { type: "rename"; session: Session }
  | { type: "delete"; session: Session }
  | null;

type Connection = { online: boolean; label: string };
// How a view change is recorded in the address bar (?session=<id>).
type HistoryMode = "push" | "replace" | "none";

const enc = encodeURIComponent;

function errorText(err: unknown) {
  return err instanceof Error ? err.message : "Đã có lỗi xảy ra.";
}

function cleanSearch(search: DiscoverySearch): DiscoverySearch {
  const out: Record<string, unknown> = {};
  for (const field of SEARCH_FIELDS) {
    const value = search?.[field];
    if (value !== undefined && value !== null && value !== "") out[field] = value;
  }
  return out as DiscoverySearch;
}

function initials(user: User | null) {
  const source = (user?.name || user?.email || "U").trim();
  return source.charAt(0).toUpperCase();
}

function isNarrow() {
  return typeof window !== "undefined" && window.matchMedia("(max-width: 800px)").matches;
}

function sessionFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("session");
}

function syncUrl(sessionId: string | null, how: HistoryMode) {
  if (how === "none") return;
  const url = sessionId ? `/chat?session=${encodeURIComponent(sessionId)}` : "/chat";
  if (`${window.location.pathname}${window.location.search}` === url) return;
  // Native history calls stay in sync with the Next.js router.
  if (how === "push") window.history.pushState(null, "", url);
  else window.history.replaceState(null, "", url);
}

export default function ChatPage() {
  const [user, setUser] = useState<User | null>(null);
  const [features, setFeatures] = useState<Features>({ report_discovery: false, chat_context: false });
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetsLoaded, setDatasetsLoaded] = useState(false);
  const [sessions, setSessionsState] = useState<Session[]>([]);
  const [active, setActiveState] = useState<Session | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  // Company news still being finished for this conversation (not blocking).
  const [newsPending, setNewsPending] = useState(false);
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [sending, setSending] = useState(false);
  const [input, setInput] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [datasetHint, setDatasetHint] = useState("");
  const [connection, setConnection] = useState<Connection>({ online: false, label: "Đang kiểm tra…" });
  const [collapsed, setCollapsed] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [userMenu, setUserMenu] = useState(false);
  const [modal, setModal] = useState<ModalState>(null);
  const [historyQuery, setHistoryQuery] = useState("");
  const [renameText, setRenameText] = useState("");
  const [modalError, setModalError] = useState("");
  const [modalBusy, setModalBusy] = useState(false);
  const [toast, setToast] = useState("");

  // Async loops compare against these refs, not against render-time state:
  // a poll that outlives its conversation must not write into the next one.
  const mounted = useRef(false);
  const viewGen = useRef(0);
  const runPoll = useRef<string | null>(null);
  const importPoll = useRef<string | null>(null);
  const newsPoll = useRef<string | null>(null);
  const sessionsRef = useRef<Session[]>([]);
  const activeRef = useRef<Session | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const datasetRef = useRef<HTMLSelectElement>(null);

  const live = (gen: number) => mounted.current && gen === viewGen.current;

  function setSessions(list: Session[]) {
    sessionsRef.current = list;
    setSessionsState(list);
  }

  function setActive(session: Session | null) {
    activeRef.current = session;
    setActiveState(session);
  }

  function appendItem(item: Omit<Item, "key">) {
    setItems((prev) => {
      // A run that finished while the conversation was loading arrives both
      // in the stored messages and from its poll: show it once.
      const id = item.message?.id;
      if (id && prev.some((existing) => existing.message?.id === id)) return prev;
      return [...prev, { key: id || `local-${Date.now()}-${prev.length}`, ...item }];
    });
  }

  const appendNotice = (text: string) => appendItem({ notice: text });

  // ---------------------------------------------------------------- loading

  async function loadDatasets() {
    try {
      setDatasets(await api<Dataset[]>("/api/datasets"));
    } catch {
      setDatasets([]);
    } finally {
      setDatasetsLoaded(true);
    }
  }

  async function loadSessions(): Promise<Session[]> {
    let list: Session[];
    try {
      list = await api<Session[]>("/api/sessions");
    } catch {
      list = sessionsRef.current;
    }
    setSessions(list);
    const current = activeRef.current;
    if (current) setActive(list.find((s) => s.id === current.id) || current);
    return list;
  }

  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch("/api/health", { credentials: "same-origin", cache: "no-store" });
      const health = (await response.json()) as Health;
      const chat = health?.workers?.chat;
      setConnection(chat?.status === "alive"
        ? { online: true, label: "Sẵn sàng" }
        : { online: false, label: "Worker chat chưa chạy" });
    } catch {
      setConnection({ online: false, label: "Mất kết nối máy chủ" });
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    if (isNarrow()) setCollapsed(true);
    // Read before any await, and act on it only if the user has not already
    // opened or started a conversation while the lists were loading.
    const wanted = sessionFromUrl();
    const startGen = viewGen.current;
    (async () => {
      try {
        const me = await api<{ user: User; features?: Features }>("/api/me");
        if (!mounted.current) return;
        setUser(me.user);
        setFeatures({ report_discovery: false, chat_context: false, ...(me.features || {}) });
      } catch {
        return; // a 401 already redirected to the login page
      }
      const [, list] = await Promise.all([loadDatasets(), loadSessions()]);
      if (!live(startGen)) return;
      // After a reload, reopen the conversation in the address bar; its open
      // run and import resume polling from the stored IDs.
      if (wanted && list.some((s) => s.id === wanted)) await openSession(wanted, list, "replace");
      else if (wanted) syncUrl(null, "replace"); // deleted, or not this user's
    })();
    checkHealth();
    const timer = setInterval(checkHealth, HEALTH_POLL_MS);
    const onPopState = () => {
      const wanted = sessionFromUrl();
      if (wanted && sessionsRef.current.some((s) => s.id === wanted)) openSession(wanted, sessionsRef.current, "none");
      else enterSession(null, "none");
    };
    window.addEventListener("popstate", onPopState);
    return () => {
      mounted.current = false;
      viewGen.current += 1; // stop every poll of this page
      clearInterval(timer);
      window.removeEventListener("popstate", onPopState);
    };
    // Mount-only: the loaders read refs, not render-time state.
  }, [checkHealth]);

  // Keep the newest message in view. (A block body: scrollIntoView-style
  // calls may return a Promise, which React would take for a cleanup.)
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length, pending, importJob?.status]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 2200);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    const close = () => {
      setMenuFor(null);
      setUserMenu(false);
    };
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, []);

  // ------------------------------------------------------------ navigation

  /** Switch the view to a conversation; returns the generation for its polls. */
  function enterSession(session: Session | null, how: HistoryMode = "push"): number {
    viewGen.current += 1;
    syncUrl(session?.id ?? null, how);
    runPoll.current = null;
    importPoll.current = null;
    newsPoll.current = null;
    setNewsPending(false);
    setActive(session);
    setItems([]);
    setPending(null);
    setImportJob(null);
    setSending(false);
    setMenuFor(null);
    if (isNarrow()) setCollapsed(true);
    return viewGen.current;
  }

  function startNewChat(how: HistoryMode = "push") {
    enterSession(null, how);
    setDatasetHint("");
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  async function openSession(id: string, list: Session[] = sessionsRef.current, how: HistoryMode = "push") {
    const session = list.find((s) => s.id === id);
    if (!session) return;
    const gen = enterSession(session, how);
    try {
      const messages = await api<Message[]>(`/api/sessions/${enc(id)}/messages`);
      if (!live(gen)) return;
      setItems(messages.map((m) => ({ key: m.id, message: m })));
      // After a reload, pick status polling back up from the stored IDs.
      if (session.import_job_id) {
        followImport(session.import_job_id).catch((err) => live(gen) && appendNotice(errorText(err)));
      }
      if (session.active_run_id) resumeRun(session.active_run_id, gen, session.id);
      if (session.news_update_run_id) void followNewsUpdate(session.news_update_run_id, gen, session.id);
    } catch (err) {
      if (live(gen)) appendNotice(`Không thể tải cuộc trò chuyện: ${errorText(err)}`);
    }
  }

  /** Show the conversation an import landed in, unless the user moved on. */
  async function openImportTarget(job: ImportJob, gen: number) {
    const list = await loadSessions();
    if (live(gen)) await openSession(job.target_session_id || job.session_id, list);
  }

  // ------------------------------------------------------------------ runs

  /**
   * Poll one run for the view (generation) and conversation that started it.
   * Both are captured by the caller *before* its own awaits: a run must never
   * render into a conversation the user switched to while it was submitted.
   */
  async function followRun(runId: string, gen: number, sessionId: string) {
    if (!live(gen) || activeRef.current?.id !== sessionId) return;
    runPoll.current = runId;
    setPending("Đang gửi…");
    while (live(gen) && runPoll.current === runId) {
      let run: Run;
      try {
        run = await api<Run>(`/api/chat-runs/${enc(runId)}`);
      } catch (err) {
        if (err instanceof ApiError && err.status === 0) {
          if (live(gen)) setPending("Mất kết nối, đang thử lại…");
          await sleep(NETWORK_RETRY_MS);
          continue;
        }
        if (live(gen)) setPending(null);
        throw err;
      }
      if (!live(gen) || runPoll.current !== runId) return;
      if (run.session_id !== sessionId || activeRef.current?.id !== sessionId) return;
      if (run.status === "succeeded") {
        setPending(null);
        runPoll.current = null;
        const caption = run.context_status === "resolved" && run.interpreted_question
          ? `Đã hiểu câu hỏi là: ${run.interpreted_question}` : undefined;
        if (run.answer) appendItem({ message: run.answer, caption });
        // The news may already be done by the time the answer is seen.
        if (run.news_update?.message) appendItem({ message: run.news_update.message });
        else if (run.news_update?.status === "pending") void followNewsUpdate(runId, gen, sessionId);
        return;
      }
      if (run.status === "failed" || run.status === "interrupted") {
        setPending(null);
        runPoll.current = null;
        if (run.answer?.kind === "context_error") appendItem({ message: run.answer });
        else appendNotice(run.error_message || "Không tạo được câu trả lời.");
        return;
      }
      setPending(run.status === "running" ? "Đang phân tích báo cáo…" : "Đang chờ tới lượt xử lý…");
      await sleep(RUN_POLL_MS);
    }
  }

  /**
   * The answer went out before its company news was ready: wait for the
   * update quietly (the composer stays usable) and add it when it arrives.
   */
  async function followNewsUpdate(runId: string, gen: number, sessionId: string) {
    if (!live(gen) || newsPoll.current === runId) return;
    newsPoll.current = runId;
    setNewsPending(true);
    const stopAt = Date.now() + NEWS_POLL_LIMIT_MS;
    try {
      while (live(gen) && newsPoll.current === runId && Date.now() < stopAt) {
        await sleep(NEWS_POLL_MS);
        if (!live(gen) || newsPoll.current !== runId) return;
        let run: Run;
        try {
          run = await api<Run>(`/api/chat-runs/${enc(runId)}`);
        } catch {
          continue; // a dropped connection: try again on the next tick
        }
        if (!live(gen) || newsPoll.current !== runId || activeRef.current?.id !== sessionId) return;
        const update = run.news_update;
        if (!update || update.status === "pending") continue;
        if (update.message) appendItem({ message: update.message });
        return;
      }
    } finally {
      if (newsPoll.current === runId) {
        newsPoll.current = null;
        if (live(gen)) setNewsPending(false);
      }
    }
  }

  function resumeRun(runId: string, gen: number, sessionId: string) {
    setSending(true);
    followRun(runId, gen, sessionId)
      .catch((err) => live(gen) && appendNotice(errorText(err)))
      .finally(() => live(gen) && setSending(false));
  }

  async function submitQuestion(event?: FormEvent) {
    event?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    let session = activeRef.current;
    if (session?.read_only) return;
    if (session && !session.dataset_id && !features.report_discovery) return;
    if (!session && !datasetId && !features.report_discovery) {
      setDatasetHint("Hãy chọn báo cáo trước khi hỏi.");
      return;
    }

    setSending(true);
    let gen = viewGen.current;
    try {
      if (!session) {
        // Without a report the message may still be a request to find one.
        const created = await api<Session>("/api/sessions", {
          method: "POST",
          body: datasetId ? { dataset_id: datasetId } : {},
        });
        if (!live(gen)) return;
        setSessions([created, ...sessionsRef.current]);
        gen = enterSession(created);
        setSending(true);
        session = created;
      }
      setInput("");
      appendItem({
        message: {
          id: `local-${Date.now()}`, role: "user", content: text, run_id: null,
          created_at: new Date().toISOString(), kind: "text", payload: null,
        },
      });
      setPending("Đang gửi…");
      // One key per question, so the model is never asked twice.
      const run = await postIdempotent<Run>("/api/chat", {
        session_id: session.id, message: text, idempotency_key: newIdempotencyKey(),
      });
      if (!live(gen)) return; // the user opened another conversation meanwhile
      await followRun(run.run_id, gen, session.id);
    } catch (err) {
      if (!live(gen)) return;
      setPending(null);
      // Before a conversation exists the start screen is showing, so the
      // error goes under the report picker; the typed question is kept.
      if (activeRef.current) appendNotice(errorText(err));
      else setDatasetHint(`Không tạo được cuộc trò chuyện: ${errorText(err)}`);
    } finally {
      if (live(gen)) {
        setSending(false);
        await loadSessions();
        inputRef.current?.focus();
      }
    }
  }

  async function answerWithReport(runId: string) {
    const gen = viewGen.current;
    const sessionId = activeRef.current?.id;
    const run = await postIdempotent<Run>(`/api/chat-runs/${enc(runId)}/answer-with-report`, {
      idempotency_key: newIdempotencyKey(),
    });
    if (!live(gen) || !sessionId) return;
    setSending(true);
    try {
      await followRun(run.run_id, gen, sessionId);
    } catch (err) {
      if (live(gen)) appendNotice(errorText(err));
    } finally {
      if (live(gen)) setSending(false);
    }
  }

  // --------------------------------------------------------------- imports

  async function followImport(jobId: string) {
    const gen = viewGen.current;
    importPoll.current = jobId;
    while (live(gen) && importPoll.current === jobId) {
      let job: ImportJob;
      try {
        job = await api<ImportJob>(`/api/report-imports/${enc(jobId)}`);
      } catch (err) {
        if (err instanceof ApiError && err.status === 0) {
          await sleep(NETWORK_RETRY_MS);
          continue;
        }
        throw err;
      }
      if (!live(gen) || importPoll.current !== jobId) return;
      setImportJob(job);
      if (job.status === "ready" || job.status === "failed") {
        importPoll.current = null;
        if (job.status === "ready") await Promise.all([loadSessions(), loadDatasets()]);
        return;
      }
      await sleep(IMPORT_POLL_MS);
    }
  }

  async function uploadReport({ file, metadata, consent }: UploadRequest) {
    const form = new FormData();
    form.append("file", file, file.name || "report.pdf");
    const gen = viewGen.current;
    const upload = await api<{ upload_id: string }>("/api/report-uploads", { method: "POST", form });
    // The server picks the conversation: this one if it has no report yet,
    // otherwise one created only now. Cloud parsing is off unless ticked.
    const current = activeRef.current;
    const job = await postIdempotent<ImportJob>("/api/report-imports", {
      session_id: current && !current.read_only ? current.id : null,
      upload_id: upload.upload_id,
      idempotency_key: newIdempotencyKey(),
      allow_cloud_parse: consent,
      metadata,
    });
    setModal(null);
    await openImportTarget(job, gen);
  }

  async function confirmCandidate(candidate: Candidate, sourceSessionId: string, consent: boolean, key: string) {
    const gen = viewGen.current;
    const job = await postIdempotent<ImportJob>("/api/report-imports", {
      session_id: sourceSessionId || null,
      confirmed_candidate_id: candidate.candidate_id,
      idempotency_key: key,
      allow_cloud_parse: consent,
    });
    await openImportTarget(job, gen);
  }

  // ------------------------------------------------------------- discovery

  async function discover(search: DiscoverySearch) {
    const current = activeRef.current;
    const gen = viewGen.current;
    const result = await postIdempotent<DiscoveryResult>("/api/report-discoveries", {
      session_id: current && !current.read_only ? current.id : null,
      idempotency_key: newIdempotencyKey(),
      search: cleanSearch(search),
    });
    setModal(null);
    if (!live(gen)) return;
    if (!current || current.id !== result.session_id) {
      const list = await loadSessions();
      await openSession(result.session_id, list);
    } else {
      appendItem({ message: result.request_message });
      appendItem({ message: result.result_message });
    }
  }

  // -------------------------------------------------------------- sessions

  async function renameSession(session: Session) {
    const title = renameText.trim();
    if (!title) {
      setModalError("Tên cuộc trò chuyện không được để trống.");
      return;
    }
    setModalBusy(true);
    setModalError("");
    try {
      const updated = await api<Session>(`/api/sessions/${enc(session.id)}`, { method: "PATCH", body: { title } });
      setSessions(sessionsRef.current.map((s) => (s.id === updated.id ? updated : s)));
      if (activeRef.current?.id === updated.id) setActive(updated);
      setModal(null);
      setToast("Đã đổi tên cuộc trò chuyện");
    } catch (err) {
      setModalError(err instanceof ApiError && err.fields.title ? err.fields.title : errorText(err));
    } finally {
      setModalBusy(false);
    }
  }

  async function deleteSession(session: Session) {
    setModalBusy(true);
    setModalError("");
    try {
      await api(`/api/sessions/${enc(session.id)}`, { method: "DELETE" });
      setSessions(sessionsRef.current.filter((s) => s.id !== session.id));
      if (activeRef.current?.id === session.id) startNewChat("replace");
      setModal(null);
      setToast("Đã xóa cuộc trò chuyện");
    } catch (err) {
      setModalError(errorText(err));
    } finally {
      setModalBusy(false);
    }
  }

  async function logout() {
    try {
      await api("/api/logout", { method: "POST", body: {} });
    } catch {
      /* the cookie is cleared server-side whenever the server is reachable */
    }
    window.location.replace("/login");
  }

  function openModal(next: ModalState) {
    setModalError("");
    setModalBusy(false);
    setMenuFor(null);
    if (next?.type === "rename") setRenameText(next.session.title);
    if (next?.type === "search") setHistoryQuery("");
    setModal(next);
  }

  // ---------------------------------------------------------------- derived

  const discovery = features.report_discovery;
  const readOnly = Boolean(active?.read_only);
  // With discovery on, a conversation without a report still takes a request
  // to find one; a financial question there gets a "choose a report" card.
  const withoutReport = Boolean(active && !active.dataset_id);
  const awaitingImport = withoutReport && !discovery;
  const needsDataset = !active && !datasetId && !discovery;
  const composerDisabled = readOnly || awaitingImport;
  const placeholder = readOnly
    ? "Cuộc trò chuyện chỉ còn để xem lại — hãy tạo cuộc trò chuyện mới."
    : awaitingImport
      ? "Đang xử lý báo cáo — bạn có thể hỏi khi báo cáo sẵn sàng."
      : needsDataset
        ? "Chọn báo cáo phía trên rồi đặt câu hỏi"
        : withoutReport || (!active && !datasetId)
          ? "Nhập yêu cầu tìm báo cáo (VD: tìm BCTC VNM năm 2025)…"
          : "Đặt câu hỏi về báo cáo…";
  const sendDisabled = sending || composerDisabled || needsDataset || !input.trim();

  let banner: { text: string; tone: "info" | "readonly" } | null = null;
  if (active?.read_only) {
    banner = { text: active.read_only_message || "Cuộc trò chuyện này chỉ còn để xem lại.", tone: "readonly" };
  } else if (active && !active.dataset_id && active.import_job_id) {
    banner = { text: "Báo cáo đang được xử lý; bạn sẽ hỏi được khi báo cáo sẵn sàng.", tone: "info" };
  } else if (active?.dataset_label) {
    banner = { text: `Đang hỏi trên: ${active.dataset_label}`, tone: "info" };
  }

  const query = historyQuery.trim().toLowerCase();
  const searchResults = query
    ? sessions.filter((s) => (s.title || "").toLowerCase().includes(query)
      || (s.dataset_label || "").toLowerCase().includes(query))
    : sessions;

  const actions: MessageActions = {
    reportDiscovery: discovery,
    canOverride: Boolean(active?.dataset_id && !active.read_only),
    importOpensNewConversation: Boolean(active && (active.dataset_id || active.import_job_id)),
    onDiscover: discover,
    onEditSearch: (prefill) => openModal({ type: "discovery", prefill }),
    onConfirmCandidate: confirmCandidate,
    onAnswerWithReport: answerWithReport,
    onChooseDataset: () => {
      startNewChat();
      setTimeout(() => datasetRef.current?.focus(), 0);
    },
    onOpenUpload: () => {
      startNewChat();
      openModal({ type: "upload" });
    },
    onOpenDiscovery: () => openModal({ type: "discovery", prefill: {} }),
  };

  const firstName = (user?.name || "").trim().split(/\s+/).pop();
  const showStart = !active;

  return (
    <div className={`chat ${collapsed ? "is-collapsed" : ""}`}>
      <aside className="chat-side" aria-label="Lịch sử trò chuyện">
        <div className="side-head">
          <PageLink href="/" className="logo"><LogoMark /><span>{BRAND}</span></PageLink>
          <button className="icon-btn" onClick={() => setCollapsed(true)} aria-label="Thu gọn thanh bên" title="Thu gọn">‹</button>
        </div>
        <div className="side-actions">
          <button className="btn btn-outline btn-block" onClick={() => startNewChat()}>+ Cuộc trò chuyện mới</button>
          <button className="btn btn-ghost btn-block left" onClick={() => openModal({ type: "search" })}>Tìm kiếm</button>
        </div>
        <p className="side-label">Gần đây</p>
        <div className="side-list">
          {sessions.length === 0 && <p className="side-empty">Chưa có cuộc trò chuyện nào.</p>}
          {sessions.map((s) => (
            <div key={s.id} className={`history ${s.id === active?.id ? "active" : ""}`}>
              <button
                className="history-title"
                title={s.dataset_label || undefined}
                aria-current={s.id === active?.id ? "page" : undefined}
                onClick={() => openSession(s.id)}
              >
                {s.read_only && <span className="lock" aria-label="Chỉ xem">🔒 </span>}
                {s.title || "Cuộc trò chuyện mới"}
              </button>
              <button
                className="icon-btn dots-btn"
                aria-label={`Tùy chọn cho ${s.title || "cuộc trò chuyện"}`}
                aria-haspopup="menu"
                aria-expanded={menuFor === s.id}
                onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }}
              >⋯</button>
              {menuFor === s.id && (
                <div className="dropdown" role="menu" onClick={(e) => e.stopPropagation()}>
                  <button role="menuitem" onClick={() => openModal({ type: "rename", session: s })}>Đổi tên</button>
                  <button role="menuitem" className="danger" onClick={() => openModal({ type: "delete", session: s })}>Xóa</button>
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="side-user">
          <span className="avatar" aria-hidden="true">{initials(user)}</span>
          <span className="grow side-user-text">
            <span className="ellipsis">{user?.name || "Tài khoản"}</span>
            <span className="ellipsis muted small">{user?.email}</span>
          </span>
          <div className="user-menu-wrap">
            <button
              className="icon-btn"
              aria-label="Tài khoản"
              aria-haspopup="menu"
              aria-expanded={userMenu}
              onClick={(e) => { e.stopPropagation(); setUserMenu(!userMenu); }}
            >⋯</button>
            {userMenu && (
              <div className="dropdown up" role="menu" onClick={(e) => e.stopPropagation()}>
                <button role="menuitem" onClick={logout}>Đăng xuất</button>
              </div>
            )}
          </div>
        </div>
      </aside>
      {!collapsed && <div className="side-scrim" onClick={() => setCollapsed(true)} aria-hidden="true" />}

      <section className="chat-main">
        <header className="chat-head">
          <div className="head-left">
            {collapsed && (
              <button className="icon-btn" onClick={() => setCollapsed(false)} aria-label="Mở thanh bên">☰</button>
            )}
            <span className="head-title ellipsis">{active?.title || BRAND}</span>
          </div>
          <div className="head-right">
            {discovery && (
              <button className="btn btn-ghost" onClick={() => openModal({ type: "discovery", prefill: {} })}>
                Tìm trên CafeF
              </button>
            )}
            <span className={`pill ${connection.online ? "online" : "offline"}`} role="status">
              <i aria-hidden="true" />{connection.label}
            </span>
          </div>
        </header>
        {banner && <div className={`session-banner ${banner.tone}`} role="status">{banner.text}</div>}

        <div className="chat-scroll" ref={scrollRef}>
          {showStart ? (
            <div className="chat-empty">
              <LogoMark size={44} />
              <h2>Chào{firstName ? ` ${firstName}` : " bạn"}, bạn muốn hỏi báo cáo nào hôm nay?</h2>
              <p className="muted">Chọn một báo cáo có sẵn, tải PDF lên{discovery ? " hoặc tìm báo cáo trên CafeF" : ""}.</p>
              <div className="start-panel">
                <label className="fl" htmlFor="dataset-select">Báo cáo để hỏi</label>
                <select
                  id="dataset-select"
                  ref={datasetRef}
                  className="field"
                  value={datasetId}
                  onChange={(e) => { setDatasetId(e.target.value); setDatasetHint(""); }}
                >
                  <option value="">{discovery ? "Chưa chọn — có thể yêu cầu tìm báo cáo" : "Chọn báo cáo để hỏi…"}</option>
                  {datasets.map((d) => (
                    <option key={d.id} value={d.id} disabled={!d.ready}>
                      {d.ready ? d.label : `${d.label} — chưa sẵn sàng`}
                    </option>
                  ))}
                </select>
                {datasetsLoaded && datasets.length === 0 && (
                  <p className="muted small">Chưa có báo cáo nào được cấp quyền cho tài khoản này.</p>
                )}
                {datasetHint && <p className="form-error" role="alert">{datasetHint}</p>}
                <div className="start-actions">
                  <button className="btn btn-outline" onClick={() => openModal({ type: "upload" })}>Tải PDF lên</button>
                  {discovery && (
                    <button className="btn btn-outline" onClick={() => openModal({ type: "discovery", prefill: {} })}>
                      Tìm trên CafeF
                    </button>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="chat-list" aria-live="polite">
              {importJob && <ImportCard job={importJob} />}
              {items.map((item) =>
                item.message ? (
                  <MessageItem key={item.key} message={item.message} caption={item.caption} actions={actions} />
                ) : (
                  <div key={item.key} className="row bot">
                    <span className="row-mark" aria-hidden="true"><LogoMark size={22} /></span>
                    <div className="row-body"><div className="bubble notice" role="alert">{item.notice}</div></div>
                  </div>
                ),
              )}
              {newsPending && !pending && (
                <div className="news-pending" role="status">
                  Đang cập nhật tin tức; kết quả sẽ tự hiện ở đây.
                </div>
              )}
              {pending && (
                <div className="row bot">
                  <span className="row-mark" aria-hidden="true"><LogoMark size={22} /></span>
                  <div className="row-body">
                    <div className="bubble bot dots" role="status"><i /><i /><i /><span className="dots-text">{pending}</span></div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <form className="composer" onSubmit={submitQuestion}>
          <div className="composer-row">
            <button
              type="button"
              className="icon-btn"
              title="Tải báo cáo PDF"
              aria-label="Tải báo cáo PDF"
              disabled={readOnly}
              onClick={() => openModal({ type: "upload" })}
            >＋</button>
            <textarea
              ref={inputRef}
              value={input}
              rows={1}
              maxLength={4000}
              disabled={composerDisabled}
              placeholder={placeholder}
              aria-label="Câu hỏi"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  submitQuestion();
                }
              }}
            />
            <button type="submit" className="send" disabled={sendDisabled} aria-label="Gửi">↑</button>
          </div>
        </form>
        <p className="composer-note muted small">Câu trả lời do AI tạo từ báo cáo; hãy đối chiếu số liệu quan trọng với nguồn.</p>
      </section>

      {modal?.type === "upload" && <UploadModal onClose={() => setModal(null)} onSubmit={uploadReport} />}

      {modal?.type === "discovery" && (
        <DiscoveryModal prefill={modal.prefill} onClose={() => setModal(null)} onSubmit={discover} />
      )}

      {modal?.type === "rename" && (
        <Modal
          title="Đổi tên cuộc trò chuyện"
          onClose={() => setModal(null)}
          footer={<>
            <button className="btn btn-outline" onClick={() => setModal(null)} disabled={modalBusy}>Hủy</button>
            <button type="submit" form="rename-form" className="btn btn-dark" disabled={modalBusy}>Lưu</button>
          </>}
        >
          <form id="rename-form" onSubmit={(e) => { e.preventDefault(); renameSession(modal.session); }}>
            <input
              className="field"
              autoFocus
              maxLength={120}
              aria-label="Tên cuộc trò chuyện"
              value={renameText}
              onChange={(e) => setRenameText(e.target.value)}
            />
          </form>
          {modalError && <p className="form-error" role="alert">{modalError}</p>}
        </Modal>
      )}

      {modal?.type === "delete" && (
        <Modal
          title="Xóa cuộc trò chuyện?"
          onClose={() => setModal(null)}
          footer={<>
            <button className="btn btn-outline" onClick={() => setModal(null)} disabled={modalBusy}>Hủy</button>
            <button className="btn btn-danger" onClick={() => deleteSession(modal.session)} disabled={modalBusy}>Xóa</button>
          </>}
        >
          <p className="muted">
            Bạn sắp xóa &quot;{modal.session.title}&quot;. Lịch sử trò chuyện không khôi phục được; báo cáo vẫn được giữ lại.
          </p>
          {modalError && <p className="form-error" role="alert">{modalError}</p>}
        </Modal>
      )}

      {modal?.type === "search" && (
        <Modal title="Tìm cuộc trò chuyện" onClose={() => setModal(null)}>
          <input
            className="field"
            autoFocus
            aria-label="Tìm theo tên hoặc báo cáo"
            placeholder="Tìm theo tên hoặc báo cáo…"
            value={historyQuery}
            onChange={(e) => setHistoryQuery(e.target.value)}
          />
          <div className="search-list">
            {searchResults.length === 0 && <p className="muted">Không có kết quả.</p>}
            {searchResults.map((s) => (
              <button key={s.id} className="search-item" onClick={() => { setModal(null); openSession(s.id); }}>
                <span>{s.title || "Cuộc trò chuyện mới"}</span>
                {s.dataset_label && <span className="muted small">{s.dataset_label}</span>}
              </button>
            ))}
          </div>
        </Modal>
      )}

      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
