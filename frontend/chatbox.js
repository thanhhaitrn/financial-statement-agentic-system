(() => {
  "use strict";

  const API_BASE = window.FINX_API_BASE || (location.port === "8000" ? "" : "http://localhost:8000");
  const TOKEN_KEY = "finx_token";
  const USER_KEY = "finx_user";

  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    window.location.replace("auth.html");
    return;
  }

  let currentUser = null;
  try {
    currentUser = JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch (_) {
    currentUser = null;
  }

  // ---------- DOM refs ----------
  const appShell = document.getElementById("appShell");
  const sidebar = document.getElementById("sidebar");
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

  const composerForm = document.getElementById("composerForm");
  const composerInput = document.getElementById("composerInput");
  const sendBtn = document.getElementById("sendBtn");
  const attachBtn = document.getElementById("attachBtn");
  const fileInput = document.getElementById("fileInput");
  const attachmentRow = document.getElementById("attachmentRow");
  const voiceBtn = document.getElementById("voiceBtn");

  const connectionPill = document.getElementById("connectionPill");
  const connectionDot = document.getElementById("connectionDot");
  const connectionLabel = document.getElementById("connectionLabel");

  // ---------- state ----------
  let sessions = [];
  let activeSessionId = null;
  let pendingFile = null; // { file_id, filename }
  let isSending = false;

  // ---------- helpers ----------
  function initials(name) {
    if (!name) return "U";
    const parts = name.trim().split(/\s+/);
    const last = parts[parts.length - 1] || "";
    return (last[0] || "U").toUpperCase();
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  async function api(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });

    if (response.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      window.location.replace("auth.html");
      throw new Error("Unauthorized");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      /* empty body (e.g. DELETE) */
    }

    if (!response.ok) {
      const detail = (data && (data.detail || data.message)) || `Lỗi máy chủ (${response.status})`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  // ---------- connection status ----------
  async function checkConnection() {
    try {
      await api("/api/me");
      connectionPill.classList.remove("offline");
      connectionPill.classList.add("online");
      connectionLabel.textContent = "Đã kết nối";
    } catch (err) {
      if (err.message === "Unauthorized") return;
      connectionPill.classList.remove("online");
      connectionPill.classList.add("offline");
      connectionLabel.textContent = "Mất kết nối backend";
    }
  }

  // ---------- user ----------
  async function loadUser() {
    try {
      const user = await api("/api/me");
      currentUser = user;
      localStorage.setItem(USER_KEY, JSON.stringify(user));
    } catch (_) {
      /* fall back to cached user, connection banner already reflects the issue */
    }
    if (currentUser) {
      userName.textContent = currentUser.name || currentUser.email || "Người dùng";
      userEmail.textContent = currentUser.email || "";
      userAvatar.textContent = initials(currentUser.name || currentUser.email);
      const firstName = (currentUser.name || "").trim().split(/\s+/).pop();
      greetingName.textContent = firstName ? `, ${firstName}!` : "!";
    }
  }

  // ---------- sidebar ----------
  function toggleCollapse() {
    appShell.classList.toggle("collapsed");
  }
  function toggleMobileSidebar(force) {
    const open = typeof force === "boolean" ? force : !appShell.classList.contains("sidebar-open");
    appShell.classList.toggle("sidebar-open", open);
  }
  collapseBtn.addEventListener("click", toggleCollapse);
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
  logoutMenuItem.addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
    window.location.replace("auth.html");
  });
  

  // ---------- sessions / history ----------
  async function loadSessions() {
    try {
      sessions = await api("/api/sessions");
    } catch (_) {
      sessions = [];
    }
    renderHistory();
  }

  function renderHistory(filter = "") {
    const q = filter.trim().toLowerCase();
    const filtered = q ? sessions.filter((s) => (s.title || "").toLowerCase().includes(q)) : sessions;

    historyList.querySelectorAll(".history-item").forEach((el) => el.remove());
    historyEmpty.hidden = filtered.length > 0;

    filtered.forEach((s) => {
      const btn = document.createElement("button");
      btn.className = "history-item" + (s.id === activeSessionId ? " active" : "");
      btn.innerHTML = `<span class="h-title">${escapeHtml(s.title || "Cuộc trò chuyện mới")}</span>`;
      btn.addEventListener("click", () => openSession(s.id));

      const del = document.createElement("button");
      del.className = "history-delete";
      del.setAttribute("aria-label", "Xoá cuộc trò chuyện");
      del.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 6h16M9 6V4h6v2M6 6l1 14h10l1-14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
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
    if (!confirm("Xoá cuộc trò chuyện này?")) return;
    try {
      await api(`/api/sessions/${id}`, { method: "DELETE" });
      sessions = sessions.filter((s) => s.id !== id);
      if (activeSessionId === id) startNewChat();
      renderHistory(historySearch.value);
    } catch (err) {
      alert(err.message);
    }
  }

  async function openSession(id) {
    activeSessionId = id;
    toggleMobileSidebar(false);
    renderHistory(historySearch.value);

    conversation.innerHTML = "";
    conversation.hidden = false;
    welcomeState.hidden = true;

    try {
      const messages = await api(`/api/sessions/${id}/messages`);
      messages.forEach((m) => appendMessage(m.role === "user" ? "user" : "ai", m.content, { skipScroll: true, time: m.created_at }));
      scrollToBottom();
    } catch (err) {
      appendMessage("ai", `Không thể tải cuộc trò chuyện: ${escapeHtml(err.message)}`, { isError: true });
    }
  }

  function startNewChat() {
    activeSessionId = null;
    conversation.innerHTML = "";
    conversation.hidden = true;
    welcomeState.hidden = false;
    clearAttachment();
    renderHistory(historySearch.value);
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

  function appendMessage(role, contentHtml, opts = {}) {
    const row = document.createElement("div");
    row.className = `msg msg-${role}${opts.isError ? " msg-error" : ""}`;

    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.innerHTML =
      role === "user"
        ? escapeHtml(initials(currentUser?.name || currentUser?.email))
        : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M4 18L9 8L13 15L16 10L20 6" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

    const col = document.createElement("div");
    col.className = "msg-bubble-col";

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    // Server-generated content only (canned/agent replies or user's own text, escaped below).
    bubble.innerHTML = contentHtml;
    col.appendChild(bubble);

    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = formatTime(opts.time);
    col.appendChild(time);

    if (role === "user") {
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
    row.id = "loadingBubble";
    row.innerHTML = `
      <div class="msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M4 18L9 8L13 15L16 10L20 6" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
      <div class="msg-bubble-col">
        <div class="msg-bubble"><span class="dot-flash"></span><span class="dot-flash"></span><span class="dot-flash"></span></div>
      </div>`;
    conversation.appendChild(row);
    scrollToBottom();
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
    sendBtn.disabled = isSending || (!composerInput.value.trim() && !pendingFile);
  });
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composerForm.requestSubmit();
    }
  });

  document.querySelectorAll(".suggestion-card").forEach((card) => {
    card.addEventListener("click", () => {
      composerInput.value = card.getAttribute("data-prompt") || "";
      autoExpand();
      sendBtn.disabled = false;
      composerInput.focus();
    });
  });

  voiceBtn.addEventListener("click", () => {
    voiceBtn.classList.toggle("active");
    alert("Nhập bằng giọng nói sẽ sớm ra mắt.");
  });

  // ---------- attachments ----------
  attachBtn.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    fileInput.value = "";
    if (!file) return;

    const allowed = [".pdf", ".xlsx", ".csv"];
    const ext = "." + file.name.split(".").pop().toLowerCase();
    if (!allowed.includes(ext)) {
      alert("Chỉ hỗ trợ file PDF, Excel (.xlsx) hoặc CSV.");
      return;
    }

    renderAttachmentChip(file.name, true);

    try {
      const formData = new FormData();
      formData.append("file", file);
      if (activeSessionId) formData.append("session_id", activeSessionId);
      const res = await api("/api/upload", { method: "POST", body: formData });
      pendingFile = { file_id: res.file_id, filename: res.filename };
      renderAttachmentChip(res.filename, false);
      sendBtn.disabled = isSending || (!composerInput.value.trim() && !pendingFile);
    } catch (err) {
      clearAttachment();
      alert(`Tải file thất bại: ${err.message}`);
    }
  });

  function renderAttachmentChip(filename, uploading) {
    attachmentRow.hidden = false;
    attachmentRow.innerHTML = `
      <span class="attachment-chip">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="m21.44 11.05-9.19 9.19a5.5 5.5 0 0 1-7.78-7.78l9.2-9.19a3.5 3.5 0 1 1 4.95 4.95l-9.2 9.19a1.5 1.5 0 0 1-2.12-2.12l8.49-8.48" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
        ${escapeHtml(filename)}${uploading ? " · đang tải…" : ""}
        <button type="button" id="removeAttachmentBtn" aria-label="Gỡ file">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </span>`;
    document.getElementById("removeAttachmentBtn").addEventListener("click", clearAttachment);
  }

  function clearAttachment() {
    pendingFile = null;
    attachmentRow.hidden = true;
    attachmentRow.innerHTML = "";
    sendBtn.disabled = isSending || !composerInput.value.trim();
  }

  // ---------- send ----------
  composerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = composerInput.value.trim();
    if ((!text && !pendingFile) || isSending) return;

    welcomeState.hidden = true;
    conversation.hidden = false;

    const userLine = pendingFile
      ? `${text ? `<p>${escapeHtml(text)}</p>` : ""}<span class="msg-attachment">📎 ${escapeHtml(pendingFile.filename)}</span>`
      : escapeHtml(text);
    appendMessage("user", userLine);

    const fileForRequest = pendingFile;
    composerInput.value = "";
    autoExpand();
    clearAttachment();
    isSending = true;
    sendBtn.disabled = true;
    const loadingRow = appendLoadingBubble();

    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          session_id: activeSessionId,
          message: text || `Phân tích file: ${fileForRequest?.filename || ""}`,
          file_id: fileForRequest?.file_id || null,
        }),
      });

      loadingRow.remove();
      appendMessage("ai", res.reply);

      if (!activeSessionId) {
        activeSessionId = res.session_id;
        await loadSessions();
        renderHistory(historySearch.value);
      } else {
        await loadSessions();
      }
    } catch (err) {
      loadingRow.remove();
      appendMessage("ai", `Đã có lỗi khi gửi tin nhắn: ${escapeHtml(err.message)}`, { isError: true });
    } finally {
      isSending = false;
      sendBtn.disabled = !composerInput.value.trim();
      composerInput.focus();
    }
  });

  // ---------- init ----------
  (async function init() {
    await loadUser();
    await checkConnection();
    await loadSessions();
    setInterval(checkConnection, 20000);
  })();
})();
