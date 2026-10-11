(() => {
  "use strict";

  // The session is an HttpOnly cookie sent automatically on same-origin
  // requests; this script never sees, stores or forwards a token.
  const POLL_INTERVAL_MS = 1500;
  const NETWORK_RETRY_MS = 3000;
  const IMPORT_POLL_MS = 2000;
  const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
  // Processing and billing are separate: a report can be ready to question
  // while its parsing cost is still being confirmed.
  const PROCESSING_TEXT = {
    queued: "Đang chờ tới lượt xử lý",
    downloading: "Đang tải báo cáo",
    parsing: "Đang trích xuất nội dung",
    validating: "Đang kiểm tra chất lượng trích xuất",
    building: "Đang dựng chỉ mục tìm kiếm",
    ready: "Sẵn sàng để hỏi",
    failed: "Không xử lý được",
  };
  const BILLING_TEXT = {
    not_submitted: "Không phát sinh chi phí trích xuất đám mây",
    pending: "Đang chờ nhà cung cấp xác nhận",
    confirmed: "Đã xác nhận",
    estimated_legacy: "Ước tính (dữ liệu cũ)",
  };
  // Report discovery. Candidate data comes from CafeF: untrusted, shown as text.
  const SAFE_SOURCE_URL = /^https:\/\/([a-z0-9-]+\.)*(cafef\.vn|mediacdn\.vn)\//i;
  const SCOPE_TEXT = { consolidated: "Hợp nhất", separate: "Riêng/công ty mẹ", unknown: "Phạm vi chưa rõ" };
  const AUDIT_TEXT = {
    audited: "Đã kiểm toán",
    reviewed: "Đã soát xét",
    unaudited: "Chưa kiểm toán",
    unknown: "Kiểm toán chưa rõ",
  };
  const VERIFICATION_TEXT = {
    missing_structured_period: "CafeF không ghi kỳ báo cáo có cấu trúc cho tài liệu này.",
    inconsistent_structured_period: "Thông tin kỳ báo cáo CafeF ghi không nhất quán.",
    title_conflicts_year: "Năm trong tiêu đề khác với năm CafeF ghi nhận.",
    title_conflicts_period: "Kỳ trong tiêu đề khác với kỳ CafeF ghi nhận.",
    half_year_without_structured_code: "Tiêu đề ghi bán niên nhưng CafeF không có mã kỳ bán niên.",
  };
  const WARNING_TEXT = {
    catalog_stale: "Danh mục mã CK đã cũ hơn 24 giờ.",
    catalog_unavailable: "Danh mục mã CK chưa sẵn sàng; tên công ty và sàn có thể thiếu.",
    ticker_not_in_catalog: "Mã này không có trong danh mục CafeF hiện tại.",
    results_truncated: "Chỉ hiển thị 30 kết quả mới nhất; hãy lọc thêm theo năm hoặc kỳ.",
  };
  const CLARIFICATION_TEXT = {
    ambiguous: (subject) => `Có nhiều doanh nghiệp khớp “${subject}”. Hãy chọn một:`,
    not_found: (subject) => `Không tìm thấy “${subject}” trong danh mục CafeF. Hãy nhập mã CK.`,
    catalog_unavailable: () => "Danh mục mã CK chưa sẵn sàng. Hãy nhập mã CK (VD: VNM).",
    needs_input: () => "Hãy cho biết mã CK hoặc tên doanh nghiệp cần tìm.",
    confirm_ticker: (subject) => `“${subject}” không khớp trực tiếp mã nào. Có phải bạn muốn tìm:`,
    ambiguous_period: () => "Kỳ báo cáo chưa rõ. Hãy chọn lại kỳ trong form tìm kiếm.",
  };
  const READ_ONLY_CODES = new Set([
    "session_read_only",
    "dataset_changed",
    "dataset_unavailable",
    "artifact_missing",
    "kb_invalid",
    "index_missing",
  ]);
  const AI_MARK_SVG =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M4 18L9 8L13 15L16 10L20 6" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const DELETE_SVG =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 6h16M9 6V4h6v2M6 6l1 14h10l1-14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  // ---------- DOM refs ----------
  const appShell = document.getElementById("appShell");
  const sidebarScrim = document.getElementById("sidebarScrim");
  const collapseBtn = document.getElementById("collapseBtn");
  const mobileSidebarBtn = document.getElementById("mobileSidebarBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const historyList = document.getElementById("historyList");
  const historyEmpty = document.getElementById("historyEmpty");
  const historySearch = document.getElementById("historySearch");
  const userMenuBtn = document.getElementById("userMenuBtn");
  const userMenu = document.getElementById("userMenu");
  const userName = document.getElementById("userName");
  const userEmail = document.getElementById("userEmail");
  const userAvatar = document.getElementById("userAvatar");
  const greetingName = document.getElementById("greetingName");
  const logoutMenuItem = document.getElementById("logoutMenuItem");

  const welcomeState = document.getElementById("welcomeState");
  const conversation = document.getElementById("conversation");
  const chatScroll = document.getElementById("chatScroll");
  const sessionBanner = document.getElementById("sessionBanner");
  const datasetSelect = document.getElementById("datasetSelect");
  const datasetHint = document.getElementById("datasetHint");

  const composerForm = document.getElementById("composerForm");
  const composerInput = document.getElementById("composerInput");
  const sendBtn = document.getElementById("sendBtn");

  const connectionPill = document.getElementById("connectionPill");
  const connectionLabel = document.getElementById("connectionLabel");

  const uploadOpenBtn = document.getElementById("uploadOpenBtn");
  const uploadForm = document.getElementById("uploadForm");
  const uploadFile = document.getElementById("uploadFile");
  const uploadCompany = document.getElementById("uploadCompany");
  const uploadTicker = document.getElementById("uploadTicker");
  const uploadYear = document.getElementById("uploadYear");
  const uploadQuarter = document.getElementById("uploadQuarter");
  const uploadScope = document.getElementById("uploadScope");
  const uploadAudit = document.getElementById("uploadAudit");
  const uploadConsent = document.getElementById("uploadConsent");
  const uploadError = document.getElementById("uploadError");
  const uploadCancelBtn = document.getElementById("uploadCancelBtn");
  const uploadSubmitBtn = document.getElementById("uploadSubmitBtn");

  const discoveryOpenBtn = document.getElementById("discoveryOpenBtn");
  const discoveryHeaderBtn = document.getElementById("discoveryHeaderBtn");
  const discoveryForm = document.getElementById("discoveryForm");
  const discoverySubject = document.getElementById("discoverySubject");
  const discoveryYear = document.getElementById("discoveryYear");
  const discoveryPeriod = document.getElementById("discoveryPeriod");
  const discoveryQuarter = document.getElementById("discoveryQuarter");
  const discoveryHalf = document.getElementById("discoveryHalf");
  const discoveryScope = document.getElementById("discoveryScope");
  const discoveryAudit = document.getElementById("discoveryAudit");
  const discoveryError = document.getElementById("discoveryError");
  const discoveryCancelBtn = document.getElementById("discoveryCancelBtn");
  const discoverySubmitBtn = document.getElementById("discoverySubmitBtn");

  // ---------- state ----------
  let currentUser = null;
  let sessions = [];
  let datasets = [];
  let activeSession = null;
  let isSending = false;
  let pollingRunId = null;
  let pollingImportId = null;
  let newsRunId = null; // run whose company news is still being finished
  let features = {};

  // ---------- helpers ----------
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function initials(name) {
    if (!name) return "U";
    const parts = String(name).trim().split(/\s+/);
    const last = parts[parts.length - 1] || "";
    return (last[0] || "U").toUpperCase();
  }

  function newIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  function errorFromBody(status, data) {
    const detail = data && data.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      return new ApiError(status, detail.code || "error", detail.message || "Đã có lỗi xảy ra.");
    }
    if (Array.isArray(detail)) return new ApiError(status, "invalid_request", "Dữ liệu gửi lên không hợp lệ.");
    return new ApiError(status, "error", `Lỗi máy chủ (${status}).`);
  }

  async function api(path, { method = "GET", body, form } = {}) {
    const headers = { Accept: "application/json" };
    let payload;
    if (form !== undefined) {
      payload = form; // FormData: the browser sets the multipart boundary itself
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, { method, credentials: "same-origin", headers, body: payload });
    } catch (_) {
      throw new ApiError(0, "network", "Không kết nối được máy chủ.");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }

    if (response.status === 401) {
      window.location.replace("/login");
      throw new ApiError(401, "unauthenticated", "Bạn chưa đăng nhập.");
    }
    if (!response.ok) throw errorFromBody(response.status, data);
    return data;
  }

  // ---------- markdown ----------
  // AI answers are Markdown. marked does NOT sanitize its output, so every
  // render goes through DOMPurify; if either library is missing, show text.
  const SAFE_LINK = /^(https?:|mailto:)/i;
  let purifyConfigured = false;

  function configurePurify() {
    if (purifyConfigured || !window.DOMPurify) return;
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") {
        const href = (node.getAttribute("href") || "").trim();
        if (!SAFE_LINK.test(href)) {
          node.removeAttribute("href");
        } else {
          node.setAttribute("target", "_blank");
          node.setAttribute("rel", "noopener noreferrer");
        }
      }
    });
    purifyConfigured = true;
  }

  function renderMarkdown(target, markdown) {
    const source = String(markdown || "");
    if (!window.marked || !window.DOMPurify) {
      target.textContent = source;
      return;
    }
    configurePurify();
    const html = window.marked.parse(source, { gfm: true, breaks: true });
    target.innerHTML = window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["img", "style", "iframe", "form", "input", "button", "textarea", "select", "svg", "math"],
      FORBID_ATTR: ["style"],
    });
  }

  // ---------- connection ----------
  function setConnection(online, label) {
    connectionPill.classList.toggle("online", online);
    connectionPill.classList.toggle("offline", !online);
    connectionLabel.textContent = label;
  }

  async function checkHealth() {
    try {
      const response = await fetch("/api/health", { credentials: "same-origin" });
      const health = await response.json();
      const chat = health && health.workers && health.workers.chat;
      if (chat && chat.status === "alive") setConnection(true, "Sẵn sàng");
      else setConnection(false, "Worker chat chưa chạy");
    } catch (_) {
      setConnection(false, "Mất kết nối máy chủ");
    }
  }

  // ---------- user ----------
  async function loadUser() {
    const data = await api("/api/me");
    currentUser = data.user;
    features = data.features || {};
    discoveryOpenBtn.hidden = !features.report_discovery;
    discoveryHeaderBtn.hidden = !features.report_discovery;
    userName.textContent = currentUser.name || currentUser.email || "Người dùng";
    userEmail.textContent = currentUser.email || "";
    userAvatar.textContent = initials(currentUser.name || currentUser.email);
    const firstName = (currentUser.name || "").trim().split(/\s+/).pop();
    greetingName.textContent = firstName ? `, ${firstName}!` : "!";
  }

  // ---------- sidebar ----------
  function toggleMobileSidebar(force) {
    const open = typeof force === "boolean" ? force : !appShell.classList.contains("sidebar-open");
    appShell.classList.toggle("sidebar-open", open);
  }
  collapseBtn.addEventListener("click", () => appShell.classList.toggle("collapsed"));
  mobileSidebarBtn.addEventListener("click", () => toggleMobileSidebar(true));
  sidebarScrim.addEventListener("click", () => toggleMobileSidebar(false));

  userMenuBtn.addEventListener("click", () => {
    const open = userMenu.hidden;
    userMenu.hidden = !open;
    userMenuBtn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => {
    if (!userMenu.hidden && !userMenu.contains(e.target) && !userMenuBtn.contains(e.target)) {
      userMenu.hidden = true;
      userMenuBtn.setAttribute("aria-expanded", "false");
    }
  });
  logoutMenuItem.addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST", body: {} });
    } catch (_) {
      /* the cookie is cleared server-side whenever the server is reachable */
    }
    window.location.replace("/login");
  });

  // ---------- datasets ----------
  async function loadDatasets() {
    try {
      datasets = await api("/api/datasets");
    } catch (_) {
      datasets = [];
    }
    datasetSelect.replaceChildren(new Option("Chọn báo cáo để hỏi…", ""));
    datasets.forEach((d) => {
      const option = new Option(d.ready ? d.label : `${d.label} — chưa sẵn sàng`, d.id);
      option.disabled = !d.ready;
      datasetSelect.add(option);
    });
    datasetHint.textContent = datasets.length ? "" : "Chưa có báo cáo nào được cấp quyền cho tài khoản này.";
    updateComposer();
  }
  datasetSelect.addEventListener("change", () => {
    datasetHint.textContent = "";
    updateComposer();
  });

  // ---------- sessions / history ----------
  async function loadSessions() {
    try {
      sessions = await api("/api/sessions");
    } catch (_) {
      sessions = [];
    }
    if (activeSession) {
      activeSession = sessions.find((s) => s.id === activeSession.id) || activeSession;
      renderBanner();
      updateComposer();
    }
    renderHistory(historySearch.value);
  }

  function renderHistory(filter = "") {
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? sessions.filter(
          (s) => (s.title || "").toLowerCase().includes(q) || (s.dataset_label || "").toLowerCase().includes(q)
        )
      : sessions;

    historyList.querySelectorAll(".history-item").forEach((el) => el.remove());
    historyEmpty.hidden = filtered.length > 0;

    filtered.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "history-item" + (activeSession && s.id === activeSession.id ? " active" : "");
      const title = document.createElement("span");
      title.className = "h-title";
      title.textContent = (s.read_only ? "🔒 " : "") + (s.title || "Cuộc trò chuyện mới");
      if (s.dataset_label) title.title = s.dataset_label;
      btn.appendChild(title);
      btn.addEventListener("click", () => openSession(s.id));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "history-delete";
      del.setAttribute("aria-label", "Xoá cuộc trò chuyện");
      del.innerHTML = DELETE_SVG; // constant markup, no data
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });
      btn.appendChild(del);

      historyList.appendChild(btn);
    });
  }

  historySearch.addEventListener("input", () => renderHistory(historySearch.value));

  async function deleteSession(id) {
    if (!confirm("Xoá cuộc trò chuyện này? Báo cáo vẫn được giữ lại.")) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      sessions = sessions.filter((s) => s.id !== id);
      if (activeSession && activeSession.id === id) startNewChat();
      renderHistory(historySearch.value);
    } catch (err) {
      alert(err.message);
    }
  }

  function showConversation() {
    welcomeState.hidden = true;
    conversation.hidden = false;
  }

  function renderBanner() {
    sessionBanner.classList.remove("is-readonly");
    if (activeSession && activeSession.read_only) {
      sessionBanner.textContent = activeSession.read_only_message || "Cuộc trò chuyện này chỉ còn để xem lại.";
      sessionBanner.classList.add("is-readonly");
      sessionBanner.hidden = false;
    } else if (activeSession && !activeSession.dataset_id && activeSession.import_job_id) {
      sessionBanner.textContent = "Báo cáo đang được xử lý; bạn sẽ hỏi được khi báo cáo sẵn sàng.";
      sessionBanner.hidden = false;
    } else if (activeSession && activeSession.dataset_label) {
      sessionBanner.textContent = `Đang hỏi trên: ${activeSession.dataset_label}`;
      sessionBanner.hidden = false;
    } else {
      sessionBanner.textContent = "";
      sessionBanner.hidden = true;
    }
  }

  function updateComposer() {
    const readOnly = Boolean(activeSession && activeSession.read_only);
    const discovery = Boolean(features.report_discovery);
    // With discovery on, a conversation without a report still takes a
    // request to find one; a financial question there gets a "choose a
    // report" card, never an answer.
    const withoutReport = Boolean(activeSession && !activeSession.dataset_id);
    const awaitingImport = withoutReport && !discovery;
    const needsDataset = !activeSession && !datasetSelect.value && !discovery;
    composerInput.disabled = readOnly || awaitingImport;
    composerInput.placeholder = readOnly
      ? "Cuộc trò chuyện chỉ còn để xem lại — hãy tạo cuộc trò chuyện mới."
      : awaitingImport
        ? "Đang xử lý báo cáo — bạn có thể hỏi khi báo cáo sẵn sàng."
        : needsDataset
          ? "Chọn báo cáo phía trên rồi đặt câu hỏi"
          : (withoutReport || (!activeSession && !datasetSelect.value))
            ? "Nhập yêu cầu tìm báo cáo (VD: tìm BCTC VNM năm 2025)…"
            : "Đặt câu hỏi về báo cáo…";
    sendBtn.disabled = isSending || readOnly || awaitingImport || needsDataset || !composerInput.value.trim();
  }

  async function openSession(id) {
    const session = sessions.find((s) => s.id === id);
    if (!session) return;
    activeSession = session;
    pollingRunId = null;
    pollingImportId = null;
    newsRunId = null;
    toggleMobileSidebar(false);
    renderHistory(historySearch.value);
    showConversation();
    conversation.replaceChildren();
    renderBanner();
    updateComposer();

    try {
      const messages = await api(`/api/sessions/${encodeURIComponent(id)}/messages`);
      if (!activeSession || activeSession.id !== id) return;
      messages.forEach((m) => renderMessage(m, { skipScroll: true }));
      scrollToBottom();
      // After a reload, pick status polling back up from the stored job ID.
      if (session.import_job_id) resumeImport(session.import_job_id);
      if (session.active_run_id) resumeRun(session.active_run_id);
      if (session.news_update_run_id) followNewsUpdate(session.news_update_run_id);
    } catch (err) {
      appendMessage("notice", `Không thể tải cuộc trò chuyện: ${err.message}`);
    }
  }

  function startNewChat() {
    activeSession = null;
    pollingRunId = null;
    pollingImportId = null;
    newsRunId = null;
    conversation.replaceChildren();
    conversation.hidden = true;
    welcomeState.hidden = false;
    renderBanner();
    renderHistory(historySearch.value);
    updateComposer();
    composerInput.focus();
  }
  newChatBtn.addEventListener("click", startNewChat);

  // ---------- message rendering ----------
  function formatTime(iso) {
    try {
      const d = iso ? new Date(iso) : new Date();
      return d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "";
    }
  }

  // role: "user" | "assistant" | "notice". Only assistant Markdown is parsed,
  // and only through DOMPurify; user text and notices are always plain text.
  function appendMessage(role, content, opts = {}) {
    const isUser = role === "user";
    const row = document.createElement("div");
    row.className = `msg ${isUser ? "msg-user" : "msg-ai"}${role === "notice" ? " msg-error" : ""}`;

    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    if (isUser) avatar.textContent = initials(currentUser && (currentUser.name || currentUser.email));
    else avatar.innerHTML = AI_MARK_SVG; // constant markup, no data

    const col = document.createElement("div");
    col.className = "msg-bubble-col";

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (role === "assistant") {
      bubble.classList.add("markdown");
      renderMarkdown(bubble, content);
    } else {
      bubble.textContent = String(content || "");
    }
    col.appendChild(bubble);

    if (opts.caption) {
      const caption = document.createElement("span");
      caption.className = "msg-caption";
      caption.textContent = opts.caption;
      col.appendChild(caption);
    }

    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = formatTime(opts.time);
    col.appendChild(time);

    if (isUser) {
      row.appendChild(col);
      row.appendChild(avatar);
    } else {
      row.appendChild(avatar);
      row.appendChild(col);
    }

    conversation.appendChild(row);
    if (!opts.skipScroll) scrollToBottom();
    return row;
  }

  function appendLoadingBubble() {
    const row = document.createElement("div");
    row.className = "msg msg-ai msg-loading";
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.innerHTML = AI_MARK_SVG;
    const col = document.createElement("div");
    col.className = "msg-bubble-col";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    for (let i = 0; i < 3; i += 1) {
      const dot = document.createElement("span");
      dot.className = "dot-flash";
      bubble.appendChild(dot);
    }
    const status = document.createElement("span");
    status.className = "msg-status";
    status.textContent = "Đang gửi…";
    bubble.appendChild(status);
    col.appendChild(bubble);
    row.appendChild(avatar);
    row.appendChild(col);
    conversation.appendChild(row);
    scrollToBottom();
    row.setStatus = (text) => {
      status.textContent = text;
    };
    return row;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      chatScroll.scrollTop = chatScroll.scrollHeight;
    });
  }

  // ---------- composer ----------
  function autoExpand() {
    composerInput.style.height = "auto";
    composerInput.style.height = Math.min(composerInput.scrollHeight, 200) + "px";
  }
  composerInput.addEventListener("input", () => {
    autoExpand();
    updateComposer();
  });
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composerForm.requestSubmit();
    }
  });

  // ---------- runs ----------
  // A network retry re-sends the same idempotency key, so the server returns
  // the original run or import instead of doing the work twice.
  async function postIdempotent(path, body) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await api(path, { method: "POST", body });
      } catch (err) {
        if (err.status === 0 && attempt < 2) {
          await sleep(NETWORK_RETRY_MS);
          continue;
        }
        throw err;
      }
    }
  }

  async function submitQuestion(sessionId, text) {
    // One key per question, so the model is never asked twice.
    return postIdempotent("/api/chat", { session_id: sessionId, message: text, idempotency_key: newIdempotencyKey() });
  }

  async function followRun(runId, loadingRow) {
    pollingRunId = runId;
    const sessionId = activeSession && activeSession.id;
    while (pollingRunId === runId) {
      let run;
      try {
        run = await api(`/api/chat-runs/${encodeURIComponent(runId)}`);
      } catch (err) {
        if (err.status === 0) {
          loadingRow.setStatus("Mất kết nối, đang thử lại…");
          await sleep(NETWORK_RETRY_MS);
          continue;
        }
        throw err;
      }
      if (!activeSession || activeSession.id !== sessionId || pollingRunId !== runId) return;

      if (run.status === "succeeded") {
        loadingRow.remove();
        const answer = run.answer;
        if (answer && answer.kind && answer.kind !== "text") {
          renderMessage(answer);
        } else {
          appendMessage("assistant", answer ? answer.content : "", {
            time: answer && answer.created_at,
            caption: run.context_status === "resolved" && run.interpreted_question
              ? `Đã hiểu câu hỏi là: ${run.interpreted_question}` : "",
          });
        }
        pollingRunId = null;
        // Company news finished after the answer: it may be done already.
        if (run.news_update && run.news_update.message) renderMessage(run.news_update.message);
        else if (run.news_update && run.news_update.status === "pending") followNewsUpdate(runId);
        return;
      }
      if (run.status === "failed" || run.status === "interrupted") {
        loadingRow.remove();
        if (run.answer && run.answer.kind === "context_error") renderMessage(run.answer);
        else appendMessage("notice", run.error_message || "Không tạo được câu trả lời.");
        pollingRunId = null;
        return;
      }
      loadingRow.setStatus(run.status === "running" ? "Đang phân tích báo cáo…" : "Đang chờ tới lượt xử lý…");
      await sleep(POLL_INTERVAL_MS);
    }
  }

  // The answer went out before its company news was ready: wait for the
  // update quietly and add it when it arrives (the server stops after 10 min).
  async function followNewsUpdate(runId) {
    if (newsRunId === runId) return;
    newsRunId = runId;
    const sessionId = activeSession && activeSession.id;
    const note = el("p", "news-pending", "Đang cập nhật tin tức; kết quả sẽ tự hiện ở đây.");
    note.setAttribute("role", "status");
    conversation.appendChild(note);
    const stopAt = Date.now() + 11 * 60 * 1000;
    try {
      while (newsRunId === runId && Date.now() < stopAt) {
        await sleep(5000);
        if (!activeSession || activeSession.id !== sessionId || newsRunId !== runId) return;
        let run;
        try {
          run = await api(`/api/chat-runs/${encodeURIComponent(runId)}`);
        } catch (_) {
          continue;
        }
        if (!activeSession || activeSession.id !== sessionId || newsRunId !== runId) return;
        const update = run.news_update;
        if (!update || update.status === "pending") continue;
        if (update.message) renderMessage(update.message);
        return;
      }
    } finally {
      note.remove();
      if (newsRunId === runId) newsRunId = null;
    }
  }

  function resumeRun(runId) {
    const loadingRow = appendLoadingBubble();
    isSending = true;
    updateComposer();
    followRun(runId, loadingRow)
      .catch((err) => {
        loadingRow.remove();
        appendMessage("notice", err.message);
      })
      .finally(() => {
        isSending = false;
        updateComposer();
      });
  }

  // ---------- report discovery ----------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function assistantRow(node, opts = {}) {
    const row = el("div", "msg msg-ai");
    const avatar = el("div", "msg-avatar");
    avatar.innerHTML = AI_MARK_SVG; // constant markup, no data
    const col = el("div", "msg-bubble-col");
    col.appendChild(node);
    col.appendChild(el("span", "msg-time", formatTime(opts.time)));
    row.append(avatar, col);
    conversation.appendChild(row);
    if (!opts.skipScroll) scrollToBottom();
    return row;
  }

  // Structured messages carry a server-built payload; everything in it is
  // rendered with textContent, never parsed as HTML.
  function renderMessage(m, opts = {}) {
    const kind = m.kind || "text";
    const payload = m.payload;
    const options = { ...opts, time: m.created_at,
      caption: m.context_status === "resolved" && m.interpreted_question
        ? `Đã hiểu câu hỏi là: ${m.interpreted_question}` : (opts.caption || "") };
    if (kind === "context_clarification" || kind === "context_error") {
      const card = el("div", "report-card context-card");
      card.appendChild(el("p", "", m.content || "Không làm rõ được câu hỏi."));
      return assistantRow(card, options);
    }
    if (kind === "text" || !payload) return appendMessage(m.role, m.content, options);
    if (kind === "report_search_request") return appendMessage("user", m.content, options);
    if (kind === "report_candidates") return assistantRow(buildCandidatesCard(payload), options);
    if (kind === "report_search_clarification") return assistantRow(buildClarificationCard(payload), options);
    if (kind === "report_search_error") return assistantRow(buildSearchErrorCard(payload), options);
    if (kind === "dataset_required") return assistantRow(buildDatasetRequiredCard(payload), options);
    return appendMessage(m.role, m.content, options);
  }

  // A chat question the planner took as "find a report" can be answered on
  // the report this conversation already has — an explicit user choice.
  function appendOverrideButton(container, payload) {
    if (payload.origin !== "chat" || !payload.run_id) return;
    if (!activeSession || !activeSession.dataset_id || activeSession.read_only) return;
    const button = el("button", "choice-btn secondary", "Trả lời trên báo cáo đang mở");
    button.type = "button";
    button.addEventListener("click", () => answerWithReport(payload.run_id, button));
    container.appendChild(button);
  }

  async function answerWithReport(runId, button) {
    button.disabled = true;
    let loadingRow = null;
    try {
      const run = await postIdempotent(`/api/chat-runs/${encodeURIComponent(runId)}/answer-with-report`, {
        idempotency_key: newIdempotencyKey(),
      });
      loadingRow = appendLoadingBubble();
      isSending = true;
      updateComposer();
      await followRun(run.run_id, loadingRow);
    } catch (err) {
      if (loadingRow) loadingRow.remove();
      appendMessage("notice", err.message);
      button.disabled = false;
    } finally {
      isSending = false;
      updateComposer();
    }
  }

  function buildDatasetRequiredCard(payload) {
    const card = el("div", "report-card");
    card.appendChild(el("p", "report-card-title", "Cuộc trò chuyện này chưa có báo cáo nên chưa trả lời được câu hỏi tài chính."));
    card.appendChild(el("p", "report-card-note", "Hãy chọn một báo cáo có sẵn, tìm báo cáo trên CafeF hoặc tải PDF lên."));
    const actions = el("div", "choice-list");
    const choose = el("button", "choice-btn", "Chọn báo cáo có sẵn");
    choose.type = "button";
    choose.addEventListener("click", () => {
      startNewChat();
      datasetSelect.focus();
    });
    actions.appendChild(choose);
    if (features.report_discovery) {
      const find = el("button", "choice-btn", "Tìm trên CafeF");
      find.type = "button";
      find.addEventListener("click", () => openDiscoveryForm());
      actions.appendChild(find);
    }
    const upload = el("button", "choice-btn secondary", "Tải PDF lên");
    upload.type = "button";
    upload.addEventListener("click", () => {
      startNewChat();
      setUploadOpen(true);
    });
    actions.appendChild(upload);
    card.appendChild(actions);
    return card;
  }

  function candidatePeriod(c) {
    if (!c.period_verified) return `Chưa xác minh kỳ${c.period_label ? ` (CafeF ghi: ${c.period_label})` : ""}`;
    if (c.period_type === "annual") return `Báo cáo năm ${c.fiscal_year}`;
    if (c.period_type === "quarter") return `Quý ${c.fiscal_quarter}/${c.fiscal_year}`;
    if (c.period_type === "half_year") return `Bán niên ${c.fiscal_year}`;
    return "Chưa rõ kỳ";
  }

  function isExpired(c) {
    const at = Date.parse(c.expires_at || "");
    return Number.isFinite(at) && at <= Date.now();
  }

  function buildCandidatesCard(payload) {
    const card = el("div", "report-card");
    const search = payload.search || {};
    const who = search.ticker || search.subject || "";
    const candidates = payload.candidates || [];
    card.appendChild(el("p", "report-card-title",
      candidates.length ? `Báo cáo tìm thấy cho ${who}` : `Không có báo cáo nào khớp cho ${who}`));
    const meta = [search.company, search.exchange].filter(Boolean).join(" · ");
    if (meta) card.appendChild(el("p", "report-card-meta", meta));
    (payload.warnings || []).forEach((code) => {
      if (WARNING_TEXT[code]) card.appendChild(el("p", "report-card-warning", WARNING_TEXT[code]));
    });
    if (!candidates.length) {
      card.appendChild(el("p", "report-card-note",
        "CafeF trả lời bình thường nhưng không có báo cáo khớp bộ lọc. Hãy nới bộ lọc hoặc tải PDF lên."));
    }
    const list = el("ul", "candidate-list");
    candidates.forEach((c) => list.appendChild(buildCandidateRow(c, payload)));
    card.appendChild(list);
    const overrides = el("div", "choice-list");
    appendOverrideButton(overrides, payload);
    if (overrides.childElementCount) card.appendChild(overrides);
    return card;
  }

  function buildCandidateRow(c, payload) {
    const item = el("li", `candidate-row${c.period_verified ? "" : " unverified"}`);
    item.appendChild(el("p", "candidate-title", c.title));
    item.appendChild(el("p", "candidate-facts",
      [candidatePeriod(c), SCOPE_TEXT[c.scope] || c.scope, AUDIT_TEXT[c.audit_status] || c.audit_status].join(" · ")));
    if (!c.period_verified && VERIFICATION_TEXT[c.verification_note]) {
      item.appendChild(el("p", "candidate-warning", VERIFICATION_TEXT[c.verification_note]));
    }
    const actions = el("div", "candidate-actions");
    if (SAFE_SOURCE_URL.test(c.source_url || "")) {
      const link = el("a", "candidate-source", `Nguồn: ${c.source_host || "CafeF"}`);
      link.href = c.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      actions.appendChild(link);
    }
    const expired = isExpired(c);
    const choose = el("button", "candidate-choose", expired ? "Đã hết hạn — tìm lại" : "Chọn báo cáo này");
    choose.type = "button";
    choose.addEventListener("click", () => {
      if (isExpired(c)) submitDiscovery(payload.search_request, choose);
      else openConfirmation(item, c, payload);
    });
    actions.appendChild(choose);
    item.appendChild(actions);
    return item;
  }

  function openConfirmation(item, c, payload) {
    if (item.querySelector(".confirm-panel")) return;
    const panel = el("div", "confirm-panel");
    panel.appendChild(el("p", "confirm-title", `Xác nhận: ${c.title}`));
    const newConversation = Boolean(activeSession && (activeSession.dataset_id || activeSession.import_job_id));
    panel.appendChild(el("p", "report-card-note", newConversation
      ? "Báo cáo sẽ mở trong một cuộc trò chuyện mới; cuộc trò chuyện này giữ nguyên báo cáo đang có."
      : "Báo cáo sẽ được xử lý trong cuộc trò chuyện này."));
    panel.appendChild(el("p", "report-card-note",
      "Nguồn công khai trên CafeF. Kết quả trích xuất có thể dùng chung giữa các tài khoản; báo cáo, quyền truy cập và lịch sử trò chuyện của bạn vẫn riêng. Chi phí hiển thị sau khi nhà cung cấp xác nhận."));
    if (!c.period_verified) {
      panel.appendChild(el("p", "candidate-warning",
        "Kỳ của báo cáo này chưa được xác minh; hãy kiểm tra tiêu đề trước khi xác nhận."));
    }
    const consentLabel = el("label", "upload-consent");
    const consent = document.createElement("input");
    consent.type = "checkbox";
    consentLabel.append(consent, el("span", "",
      "Đồng ý gửi PDF tới LlamaParse nếu không đọc được lớp văn bản (phát sinh chi phí). Không đồng ý thì chỉ dùng trích xuất cục bộ."));
    panel.appendChild(consentLabel);
    const error = el("p", "upload-error");
    error.setAttribute("role", "alert");
    panel.appendChild(error);
    const buttons = el("div", "upload-actions");
    const cancel = el("button", "upload-cancel-btn", "Huỷ");
    cancel.type = "button";
    cancel.addEventListener("click", () => panel.remove());
    const confirm = el("button", "upload-submit-btn", "Xác nhận tải và xử lý");
    confirm.type = "button";
    // One key per confirmation, renewed if the consent choice changes: a
    // network retry replays the same import and never starts a second one.
    let key = newIdempotencyKey();
    consent.addEventListener("change", () => { key = newIdempotencyKey(); });
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      error.textContent = "";
      try {
        const job = await postIdempotent("/api/report-imports", {
          session_id: payload.source_session_id,
          confirmed_candidate_id: c.candidate_id,
          idempotency_key: key,
          allow_cloud_parse: consent.checked,
        });
        await openImportTarget(job);
      } catch (err) {
        error.textContent = err.message;
        confirm.disabled = false;
      }
    });
    buttons.append(cancel, confirm);
    panel.appendChild(buttons);
    item.appendChild(panel);
  }

  function buildClarificationCard(payload) {
    const card = el("div", "report-card");
    const clarification = payload.clarification || {};
    const text = CLARIFICATION_TEXT[clarification.reason];
    card.appendChild(el("p", "report-card-title",
      text ? text(clarification.subject || "") : "Cần thêm thông tin để tìm báo cáo."));
    const choices = el("div", "choice-list");
    (clarification.choices || []).forEach((choice) => {
      const button = el("button", "choice-btn", [choice.ticker, choice.company, choice.exchange].filter(Boolean).join(" · "));
      button.type = "button";
      button.addEventListener("click", () => submitDiscovery({ ...(payload.search_request || {}), ticker: choice.ticker }, button));
      choices.appendChild(button);
    });
    const edit = el("button", "choice-btn secondary", "Sửa thông tin tìm kiếm");
    edit.type = "button";
    edit.addEventListener("click", () => openDiscoveryForm(payload.search_request || {}));
    choices.appendChild(edit);
    appendOverrideButton(choices, payload);
    if (clarification.more) card.appendChild(el("p", "report-card-note", "Còn nhiều kết quả khác; hãy nhập tên đầy đủ hơn hoặc mã CK."));
    card.appendChild(choices);
    return card;
  }

  function buildSearchErrorCard(payload) {
    const card = el("div", "report-card is-error");
    card.appendChild(el("p", "report-card-title", (payload.error && payload.error.message) || "Chưa tìm được báo cáo trên CafeF."));
    const actions = el("div", "choice-list");
    const retry = el("button", "choice-btn", "Thử lại");
    retry.type = "button";
    retry.addEventListener("click", () => submitDiscovery(payload.search_request || {}, retry));
    const upload = el("button", "choice-btn secondary", "Tải PDF lên");
    upload.type = "button";
    upload.addEventListener("click", () => {
      startNewChat();
      setUploadOpen(true);
    });
    actions.append(retry, upload);
    appendOverrideButton(actions, payload);
    card.appendChild(actions);
    return card;
  }

  function cleanSearch(search) {
    const allowed = ["subject", "ticker", "fiscal_year", "period", "fiscal_quarter", "fiscal_half", "scope", "audit_status"];
    const out = {};
    allowed.forEach((field) => {
      const value = search ? search[field] : undefined;
      if (value !== undefined && value !== null && value !== "") out[field] = value;
    });
    return out;
  }

  async function openImportTarget(job) {
    await loadSessions();
    await openSession(job.target_session_id || job.session_id);
  }

  async function submitDiscovery(search, trigger) {
    if (trigger) trigger.disabled = true;
    try {
      const result = await postIdempotent("/api/report-discoveries", {
        session_id: activeSession && !activeSession.read_only ? activeSession.id : null,
        idempotency_key: newIdempotencyKey(),
        search: cleanSearch(search),
      });
      if (!activeSession || activeSession.id !== result.session_id) {
        await loadSessions();
        await openSession(result.session_id);
      } else {
        renderMessage(result.request_message);
        renderMessage(result.result_message);
      }
      return result;
    } catch (err) {
      if (!discoveryForm.hidden) discoveryError.textContent = err.message;
      else appendMessage("notice", err.message);
      return null;
    } finally {
      if (trigger) trigger.disabled = false;
    }
  }

  function syncPeriodFields() {
    discoveryQuarter.disabled = discoveryPeriod.value !== "quarter";
    if (discoveryQuarter.disabled) discoveryQuarter.value = "";
    discoveryHalf.disabled = discoveryPeriod.value !== "half_year";
    if (discoveryHalf.disabled) discoveryHalf.value = "";
  }

  function openDiscoveryForm(prefill = {}) {
    discoveryForm.hidden = false;
    discoveryError.textContent = "";
    if (prefill.subject) discoverySubject.value = prefill.subject;
    if (prefill.fiscal_year) discoveryYear.value = prefill.fiscal_year;
    if (prefill.period) discoveryPeriod.value = prefill.period;
    syncPeriodFields();
    if (prefill.fiscal_quarter) discoveryQuarter.value = String(prefill.fiscal_quarter);
    if (prefill.fiscal_half) discoveryHalf.value = String(prefill.fiscal_half);
    if (prefill.scope) discoveryScope.value = prefill.scope;
    if (prefill.audit_status) discoveryAudit.value = prefill.audit_status;
    discoveryForm.scrollIntoView({ block: "nearest" });
    discoverySubject.focus();
  }

  discoveryOpenBtn.addEventListener("click", () => openDiscoveryForm());
  discoveryHeaderBtn.addEventListener("click", () => openDiscoveryForm());
  discoveryCancelBtn.addEventListener("click", () => {
    discoveryForm.hidden = true;
  });
  discoveryPeriod.addEventListener("change", syncPeriodFields);

  discoveryForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const subject = discoverySubject.value.trim();
    const year = discoveryYear.value.trim();
    if (!subject) {
      discoveryError.textContent = "Hãy nhập mã CK hoặc tên công ty.";
      return;
    }
    if (year && !/^\d{4}$/.test(year)) {
      discoveryError.textContent = "Năm tài chính không hợp lệ.";
      return;
    }
    const search = {
      subject,
      period: discoveryPeriod.value,
      scope: discoveryScope.value,
      audit_status: discoveryAudit.value,
    };
    if (year) search.fiscal_year = Number(year);
    if (discoveryQuarter.value) search.fiscal_quarter = Number(discoveryQuarter.value);
    if (discoveryHalf.value) search.fiscal_half = Number(discoveryHalf.value);
    discoverySubmitBtn.disabled = true;
    discoverySubmitBtn.textContent = "Đang tìm…";
    try {
      const result = await submitDiscovery(search, null);
      if (result) {
        discoveryForm.reset();
        syncPeriodFields();
        discoveryForm.hidden = true;
      }
    } finally {
      discoverySubmitBtn.disabled = false;
      discoverySubmitBtn.textContent = "Tìm báo cáo";
    }
  });

  // ---------- report upload ----------
  function billingLine(job) {
    const text = BILLING_TEXT[job.billing_status] || "";
    if (job.billing_status === "confirmed" && job.confirmed_credits !== "") {
      return `${text}: ${job.confirmed_credits} credit`;
    }
    return text;
  }

  // Import status is shown with textContent only; nothing from the server is parsed as HTML.
  function renderImportCard(job) {
    let card = conversation.querySelector(".import-card");
    if (!card) {
      card = document.createElement("div");
      card.className = "import-card";
      card.setAttribute("role", "status");
      const title = document.createElement("p");
      title.className = "import-title";
      title.textContent = "Báo cáo tải lên";
      const processing = document.createElement("p");
      processing.className = "import-processing";
      const billing = document.createElement("p");
      billing.className = "import-billing";
      const note = document.createElement("p");
      note.className = "import-note";
      card.append(title, processing, billing, note);
      conversation.prepend(card);
    }
    card.classList.toggle("is-failed", job.status === "failed");
    const pages = job.page_count ? ` · ${job.page_count} trang` : "";
    card.querySelector(".import-processing").textContent = `Xử lý: ${PROCESSING_TEXT[job.status] || job.status}${pages}`;
    card.querySelector(".import-billing").textContent = `Chi phí trích xuất: ${billingLine(job)}`;
    let note = "";
    if (job.status === "failed") {
      note = job.error_message || "Không xử lý được báo cáo này.";
    } else if (job.status === "ready" && !job.session_attached) {
      note = "Báo cáo đã sẵn sàng nhưng chưa gắn được vào cuộc trò chuyện này; hãy chọn nó trong danh sách báo cáo.";
    } else if (job.status === "ready" && job.billing_blocking) {
      // Only a blocking pending charge holds back the next import.
      note = "Bạn đã có thể hỏi trên báo cáo này. Lần tải báo cáo tiếp theo sẽ mở khi chi phí được xác nhận.";
    } else if (job.status === "ready" && job.billing_pending) {
      note = "Bạn đã có thể hỏi trên báo cáo này.";
    }
    card.querySelector(".import-note").textContent = note;
  }

  async function followImport(jobId) {
    pollingImportId = jobId;
    const sessionId = activeSession && activeSession.id;
    while (pollingImportId === jobId) {
      let job;
      try {
        job = await api(`/api/report-imports/${encodeURIComponent(jobId)}`);
      } catch (err) {
        if (err.status === 0) {
          await sleep(NETWORK_RETRY_MS);
          continue;
        }
        throw err;
      }
      if (!activeSession || activeSession.id !== sessionId || pollingImportId !== jobId) return;
      renderImportCard(job);
      if (job.status === "ready" || job.status === "failed") {
        pollingImportId = null;
        if (job.status === "ready") await Promise.all([loadSessions(), loadDatasets()]);
        return;
      }
      await sleep(IMPORT_POLL_MS);
    }
  }

  function resumeImport(jobId) {
    followImport(jobId)
      .catch((err) => appendMessage("notice", err.message))
      .finally(updateComposer);
  }

  function setUploadOpen(open) {
    uploadForm.hidden = !open;
    uploadOpenBtn.hidden = open;
    uploadError.textContent = "";
    if (open) uploadFile.focus();
  }
  uploadOpenBtn.addEventListener("click", () => setUploadOpen(true));
  uploadCancelBtn.addEventListener("click", () => setUploadOpen(false));

  function uploadProblem(file) {
    if (!file) return "Hãy chọn file PDF.";
    const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name || "");
    if (!isPdf) return "Chỉ nhận file PDF.";
    if (file.size > MAX_UPLOAD_BYTES) return "PDF vượt quá giới hạn 25 MB.";
    if (!uploadCompany.value.trim()) return "Hãy nhập tên công ty.";
    const year = uploadYear.value.trim();
    if (year && !/^\d{4}$/.test(year)) return "Năm tài chính không hợp lệ.";
    return "";
  }

  function uploadMetadata() {
    const metadata = {
      company: uploadCompany.value.trim(),
      ticker: uploadTicker.value.trim().toUpperCase(),
      scope: uploadScope.value,
      audit_status: uploadAudit.value,
    };
    const year = uploadYear.value.trim();
    if (year) metadata.fiscal_year = Number(year);
    if (uploadQuarter.value) metadata.fiscal_quarter = Number(uploadQuarter.value);
    return metadata;
  }

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = uploadFile.files && uploadFile.files[0];
    const problem = uploadProblem(file);
    if (problem) {
      uploadError.textContent = problem;
      return;
    }
    uploadError.textContent = "";
    uploadSubmitBtn.disabled = true;
    uploadSubmitBtn.textContent = "Đang tải lên…";
    try {
      const form = new FormData();
      form.append("file", file, file.name || "report.pdf");
      const upload = await api("/api/report-uploads", { method: "POST", form });
      // The server picks the conversation: this one if it has no report yet,
      // otherwise one created only now. Consent is off unless ticked.
      const job = await postIdempotent("/api/report-imports", {
        session_id: activeSession && !activeSession.read_only ? activeSession.id : null,
        upload_id: upload.upload_id,
        idempotency_key: newIdempotencyKey(),
        allow_cloud_parse: uploadConsent.checked,
        metadata: uploadMetadata(),
      });
      uploadForm.reset();
      setUploadOpen(false);
      await openImportTarget(job);
    } catch (err) {
      uploadError.textContent = err.message;
    } finally {
      uploadSubmitBtn.disabled = false;
      uploadSubmitBtn.textContent = "Tải lên và xử lý";
    }
  });

  composerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = composerInput.value.trim();
    if (!text || isSending) return;
    if (activeSession && activeSession.read_only) return;
    if (activeSession && !activeSession.dataset_id && !features.report_discovery) return;

    isSending = true;
    updateComposer();
    let loadingRow = null;
    try {
      if (!activeSession) {
        const datasetId = datasetSelect.value;
        if (!datasetId && !features.report_discovery) {
          datasetHint.textContent = "Hãy chọn báo cáo trước khi hỏi.";
          return;
        }
        // Without a report the message may still be a request to find one.
        const created = await api("/api/sessions", {
          method: "POST",
          body: datasetId ? { dataset_id: datasetId } : {},
        });
        sessions.unshift(created);
        activeSession = created;
        showConversation();
        conversation.replaceChildren();
        renderBanner();
        renderHistory(historySearch.value);
      }

      composerInput.value = "";
      autoExpand();
      appendMessage("user", text);
      loadingRow = appendLoadingBubble();
      const run = await submitQuestion(activeSession.id, text);
      await followRun(run.run_id, loadingRow);
    } catch (err) {
      if (loadingRow) loadingRow.remove();
      appendMessage("notice", err.message);
      if (READ_ONLY_CODES.has(err.code)) await loadSessions();
    } finally {
      isSending = false;
      await loadSessions();
      updateComposer();
      composerInput.focus();
    }
  });

  // ---------- init ----------
  (async function init() {
    try {
      await loadUser();
    } catch (_) {
      return; // 401 already redirected to the login page
    }
    // Render the start state first. The page is usable while the lists load,
    // and a slow request (the first health check can take seconds) must never
    // reset a conversation or upload the user has already started.
    startNewChat();
    checkHealth();
    setInterval(checkHealth, 20000);
    await Promise.all([loadDatasets(), loadSessions()]);
  })();
})();
