/**
 * AgentFinX — chatbox.js
 * Gọi FastAPI backend thay vì fake reply.
 */

// ── CONFIG ──────────────────────────────────────────────────────────────────
const API = 'http://localhost:8000';   // ← đổi nếu deploy server khác

// ── STATE ────────────────────────────────────────────────────────────────────
let currentUser  = null;
let authToken    = null;
let attachedFile = null;
let uploadedFileId = null;   // ID file trả về từ server sau khi upload
let currentSessionId = null;
let isTyping     = false;

// ── INIT ─────────────────────────────────────────────────────────────────────
(async function init() {
  authToken   = localStorage.getItem('finx_token');
  const raw   = localStorage.getItem('finx_user');

  if (!authToken || !raw) {
    window.location.href = 'index.html';
    return;
  }

  currentUser = JSON.parse(raw);
  document.getElementById('sidebar-name').textContent  = currentUser.name  || 'Người dùng';
  document.getElementById('sidebar-email').textContent = currentUser.email || '';
  document.getElementById('sidebar-avatar').textContent =
    currentUser.name ? currentUser.name[0].toUpperCase() : 'U';

  await loadSessionList();
})();

// ── API HELPER ────────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    ...opts,
    headers: {
      'Authorization': `Bearer ${authToken}`,
      ...(opts.headers || {})
    }
  });
  if (res.status === 401) {
    doLogout();
    throw new Error('Phiên đăng nhập hết hạn');
  }
  return res;
}

// ── ĐĂNG XUẤT ────────────────────────────────────────────────────────────────
function doLogout() {
  localStorage.removeItem('finx_token');
  localStorage.removeItem('finx_user');
  window.location.href = 'index.html';
}

// ── LOAD DANH SÁCH SESSION ───────────────────────────────────────────────────
async function loadSessionList() {
  try {
    const res  = await apiFetch('/api/sessions');
    const list = await res.json();
    renderSessionList(list);
  } catch(e) {
    console.warn('Không tải được lịch sử:', e);
  }
}

function renderSessionList(sessions) {
  const el = document.getElementById('history-list');
  if (!sessions.length) {
    el.innerHTML = '<div style="padding:8px 12px;font-size:12px;color:var(--text-muted)">Chưa có cuộc trò chuyện nào</div>';
    return;
  }
  el.innerHTML = sessions.map(s => `
    <div class="history-item${s.id === currentSessionId ? ' active' : ''}"
         onclick="loadSession('${s.id}', this)">
      <span class="dot"></span>${escHtml(s.title)}
    </div>
  `).join('');
}

// ── LOAD MESSAGES CỦA SESSION ─────────────────────────────────────────────────
async function loadSession(sessionId, el) {
  document.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
  el?.classList.add('active');

  currentSessionId = sessionId;
  hideWelcome();
  document.getElementById('messages').innerHTML = '';

  try {
    const res  = await apiFetch(`/api/sessions/${sessionId}/messages`);
    const msgs = await res.json();
    msgs.forEach(m => appendMessage(m.role === 'user' ? 'user' : 'ai', m.content, false));
    scrollBottom();
  } catch(e) {
    showToast('Không tải được tin nhắn');
  }
}

// ── NEW CHAT ─────────────────────────────────────────────────────────────────
function newChat() {
  currentSessionId = null;
  attachedFile     = null;
  uploadedFileId   = null;
  document.getElementById('attached-preview').classList.remove('show');
  document.getElementById('chat-input').value = '';
  document.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
  resetMessages();
}

// ── TEXTAREA AUTO-RESIZE ──────────────────────────────────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  document.getElementById('send-btn').disabled = el.value.trim() === '' && !attachedFile;
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

// ── ĐÍNH KÈM FILE ─────────────────────────────────────────────────────────────
function attachFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  attachedFile = file;
  uploadedFileId = null;  // reset, sẽ upload khi gửi

  document.getElementById('attached-name').textContent = file.name;
  document.getElementById('attached-size').textContent = formatSize(file.size);
  document.getElementById('attached-preview').classList.add('show');
  document.getElementById('send-btn').disabled = false;
  e.target.value = '';
}

function removeFile() {
  attachedFile   = null;
  uploadedFileId = null;
  document.getElementById('attached-preview').classList.remove('show');
  const input = document.getElementById('chat-input');
  document.getElementById('send-btn').disabled = input.value.trim() === '';
}

function formatSize(bytes) {
  return bytes < 1048576
    ? (bytes / 1024).toFixed(1) + ' KB'
    : (bytes / 1048576).toFixed(1) + ' MB';
}

// ── GỬI TIN NHẮN ─────────────────────────────────────────────────────────────
async function sendMessage() {
  const input = document.getElementById('chat-input');
  const text  = input.value.trim();
  if (!text && !attachedFile) return;

  hideWelcome();
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('send-btn').disabled = true;

  // 1. Upload file nếu có
  if (attachedFile) {
    const file = attachedFile;
    appendFileBubble(file.name, formatSize(file.size));
    attachedFile = null;
    document.getElementById('attached-preview').classList.remove('show');

    try {
      const fd = new FormData();
      fd.append('file', file);
      if (currentSessionId) fd.append('session_id', currentSessionId);

      const res  = await apiFetch('/api/upload', { method: 'POST', body: fd });
      const data = await res.json();

      if (!res.ok) {
        appendMessage('ai', `⚠️ Upload thất bại: ${data.detail || 'lỗi không xác định'}`);
        return;
      }
      uploadedFileId = data.file_id;
    } catch(e) {
      appendMessage('ai', '⚠️ Không kết nối được server để upload file.');
    }

    if (!text) return;   // nếu chỉ upload file, không có câu hỏi thì dừng
  }

  // 2. Hiển thị tin nhắn user
  if (text) {
    appendMessage('user', text);
  }

  // 3. Gọi API chat
  showTyping();
  try {
    const res = await apiFetch('/api/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: currentSessionId || null,
        message:    text,
        file_id:    uploadedFileId || null
      })
    });

    const data = await res.json();
    removeTyping();

    if (!res.ok) {
      appendMessage('ai', `⚠️ Lỗi: ${data.detail || 'Không rõ nguyên nhân'}`);
      return;
    }

    // Lưu session_id nếu là session mới
    if (!currentSessionId) {
      currentSessionId = data.session_id;
      await loadSessionList();
    }

    appendMessage('ai', data.reply);
    uploadedFileId = null;  // dùng 1 lần rồi reset

  } catch(e) {
    removeTyping();
    appendMessage('ai', '⚠️ Không kết nối được server. Hãy chắc chắn backend đang chạy.');
  }
}

// ── HIỂN THỊ TIN NHẮN ────────────────────────────────────────────────────────
function appendMessage(role, text, doScroll = true) {
  const isAI = role === 'ai';
  const row  = document.createElement('div');
  row.className = `msg-row${isAI ? '' : ' user'}`;
  row.innerHTML = `
    <div class="msg-avatar ${isAI ? 'ai' : 'user-av'}">
      <img src="${isAI ? 'ảnh.jpg' : 'ảnh2.jpg'}" onerror="this.style.display='none'" />
    </div>
    <div class="msg-content">
      <div class="msg-sender">${isAI ? 'AgentFinX' : (currentUser?.name || 'Bạn')}</div>
      <div class="msg-bubble">${text.replace(/\n/g,'<br>')}</div>
    </div>`;
  document.getElementById('messages').appendChild(row);
  if (doScroll) row.scrollIntoView({ behavior:'smooth', block:'end' });
}

function scrollBottom() {
  const m = document.getElementById('messages');
  m.scrollTop = m.scrollHeight;
}

// ── FILE BUBBLE ───────────────────────────────────────────────────────────────
function appendFileBubble(name, size) {
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML = `
    <div class="msg-avatar user-av">
      <img src="ảnh2.jpg" onerror="this.style.display='none'" />
    </div>
    <div class="msg-content" style="align-items:flex-end;">
      <div class="msg-sender">${currentUser?.name || 'Bạn'}</div>
      <div class="file-bubble">
        <div class="file-icon">📄</div>
        <div class="file-meta">
          <div class="file-name">${escHtml(name)}</div>
          <div class="file-size">${size} · PDF</div>
        </div>
      </div>
    </div>`;
  document.getElementById('messages').appendChild(row);
  row.scrollIntoView({ behavior:'smooth', block:'end' });
}

// ── TYPING INDICATOR ──────────────────────────────────────────────────────────
function showTyping() {
  isTyping = true;
  const row = document.createElement('div');
  row.className = 'msg-row'; row.id = 'typing-row';
  row.innerHTML = `
    <div class="msg-avatar ai">
      <img src="ảnh.jpg" onerror="this.style.display='none'" />
    </div>
    <div class="msg-content">
      <div class="msg-sender">AgentFinX</div>
      <div class="msg-bubble" style="padding:14px 18px;">
        <div class="typing-indicator"><span></span><span></span><span></span></div>
      </div>
    </div>`;
  document.getElementById('messages').appendChild(row);
  row.scrollIntoView({ behavior:'smooth', block:'end' });
}

function removeTyping() {
  isTyping = false;
  const t = document.getElementById('typing-row');
  if (t) t.remove();
}

// ── WELCOME / CHIPS ───────────────────────────────────────────────────────────
function hideWelcome() {
  const w = document.getElementById('welcome-state');
  if (w) w.remove();
}

function useChip(el) {
  const input = document.getElementById('chat-input');
  input.value = el.textContent.trim();
  autoResize(input);
  sendMessage();
}

function resetMessages() {
  document.getElementById('messages').innerHTML = `
    <div class="welcome" id="welcome-state">
      <div class="welcome-icon"></div>
      <h2>Hế lô tôi là AgentFinX</h2>
      <p>Tải lên báo cáo tài chính PDF hoặc đặt câu hỏi để tôi phân tích, dự đoán xu hướng và cung cấp insight chuyên sâu.</p>
      <div class="welcome-chips">
        <div class="chip" onclick="useChip(this)">Phân tích báo cáo tài chính</div>
        <div class="chip" onclick="useChip(this)">Dự báo doanh thu năm tới</div>
        <div class="chip" onclick="useChip(this)">So sánh với ngành</div>
        <div class="chip" onclick="useChip(this)">Phát hiện rủi ro tài chính</div>
        <div class="chip" onclick="useChip(this)">Phân tích dòng tiền</div>
        <div class="chip" onclick="useChip(this)">Đánh giá chỉ số ROE/ROA</div>
      </div>
    </div>`;
}

// ── EXPORT ────────────────────────────────────────────────────────────────────
function exportChat() { showToast('Đang xuất báo cáo PDF...'); }

// ── TOAST ─────────────────────────────────────────────────────────────────────
function showToast(msg) {
  const t = document.getElementById('toast');
  document.getElementById('toast-msg').textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── UTILS ─────────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
