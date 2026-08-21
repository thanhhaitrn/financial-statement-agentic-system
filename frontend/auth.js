(() => {
  "use strict";

  // Backend base URL. When FastAPI itself serves this file (the recommended
  // setup — see backend/README further down), we're already same-origin on
  // :8000, so relative paths work. If the frontend is instead served by a
  // separate static server (e.g. `python -m http.server 3000`), fall back to
  // http://localhost:8000. Override anytime with window.FINX_API_BASE.
  const API_BASE = window.FINX_API_BASE || (location.port === "8000" ? "" : "http://localhost:8000");

  const TOKEN_KEY = "finx_token";
  const USER_KEY = "finx_user";

  const loginPanel = document.getElementById("loginPanel");
  const registerPanel = document.getElementById("registerPanel");
  const loginForm = document.getElementById("loginForm");
  const registerForm = document.getElementById("registerForm");
  const loginBanner = document.getElementById("loginBanner");
  const registerBanner = document.getElementById("registerBanner");

  // Already authenticated? Skip straight to the app.
  if (localStorage.getItem(TOKEN_KEY)) {
    window.location.replace("chat.html");
    return;
  }

  function showPanel(mode) {
    const isRegister = mode === "register";
    loginPanel.hidden = isRegister;
    registerPanel.hidden = !isRegister;
    (isRegister ? document.getElementById("registerName") : document.getElementById("loginEmail"))?.focus();
  }

  const params = new URLSearchParams(window.location.search);
  showPanel(params.get("mode") === "register" ? "register" : "login");

  document.getElementById("showRegister").addEventListener("click", () => showPanel("register"));
  document.getElementById("showLogin").addEventListener("click", () => showPanel("login"));

  document.querySelectorAll("[data-toggle-password]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById(btn.getAttribute("data-toggle-password"));
      if (!input) return;
      input.type = input.type === "password" ? "text" : "password";
    });
  });

  function setBanner(el, message, kind) {
    if (!message) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = message;
    el.classList.toggle("success", kind === "success");
  }

  function clearFieldErrors(form) {
    form.querySelectorAll(".field-error").forEach((el) => (el.textContent = ""));
    form.querySelectorAll("input").forEach((el) => el.removeAttribute("aria-invalid"));
  }

  function setFieldError(inputId, message) {
    const errorEl = document.getElementById(`${inputId}Error`);
    const input = document.getElementById(inputId);
    if (errorEl) errorEl.textContent = message;
    if (input) input.setAttribute("aria-invalid", "true");
  }

  async function apiRequest(path, body) {
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      throw new Error("Không thể kết nối máy chủ. Kiểm tra backend đã chạy tại " + API_BASE + " chưa.");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      /* no body */
    }

    if (!response.ok) {
      const detail = (data && (data.detail || data.message)) || "Đã có lỗi xảy ra, vui lòng thử lại.";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function persistSession(data, remember) {
    const store = remember ? localStorage : sessionStorage;
    store.setItem(TOKEN_KEY, data.token);
    store.setItem(USER_KEY, JSON.stringify(data.user));
    // Mirror into localStorage too so chat.html's single lookup path works
    // regardless of "remember me"; sessionStorage copy still expires on tab close
    // is not enforceable for localStorage, so we accept persistent login either way
    // for this student/demo build.
    localStorage.setItem(TOKEN_KEY, data.token);
    localStorage.setItem(USER_KEY, JSON.stringify(data.user));
  }

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFieldErrors(loginForm);
    setBanner(loginBanner, "");

    const email = document.getElementById("loginEmail").value.trim();
    const password = document.getElementById("loginPassword").value;
    const remember = document.getElementById("rememberMe").checked;

    if (!email) return setFieldError("loginEmail", "Vui lòng nhập email.");
    if (!password) return setFieldError("loginPassword", "Vui lòng nhập mật khẩu.");

    const submitBtn = document.getElementById("loginSubmit");
    submitBtn.classList.add("is-loading");
    submitBtn.disabled = true;

    try {
      const data = await apiRequest("/api/login", { email, password });
      persistSession(data, remember);
      window.location.href = "chat.html";
    } catch (err) {
      setBanner(loginBanner, err.message);
    } finally {
      submitBtn.classList.remove("is-loading");
      submitBtn.disabled = false;
    }
  });

  registerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFieldErrors(registerForm);
    setBanner(registerBanner, "");

    const name = document.getElementById("registerName").value.trim();
    const email = document.getElementById("registerEmail").value.trim();
    const password = document.getElementById("registerPassword").value;
    const confirm = document.getElementById("registerConfirm").value;

    let hasError = false;
    if (!name) { setFieldError("registerName", "Vui lòng nhập họ tên."); hasError = true; }
    if (!email) { setFieldError("registerEmail", "Vui lòng nhập email."); hasError = true; }
    if (password.length < 6) { setFieldError("registerPassword", "Mật khẩu tối thiểu 6 ký tự."); hasError = true; }
    if (confirm !== password) { setFieldError("registerConfirm", "Mật khẩu xác nhận không khớp."); hasError = true; }
    if (hasError) return;

    const submitBtn = document.getElementById("registerSubmit");
    submitBtn.classList.add("is-loading");
    submitBtn.disabled = true;

    try {
      const data = await apiRequest("/api/register", { name, email, password });
      persistSession(data, true);
      window.location.href = "chat.html";
    } catch (err) {
      setBanner(registerBanner, err.message);
    } finally {
      submitBtn.classList.remove("is-loading");
      submitBtn.disabled = false;
    }
  });

  document.getElementById("forgotPasswordLink").addEventListener("click", (e) => {
    e.preventDefault();
    setBanner(
      loginBanner,
      "Tính năng khôi phục mật khẩu chưa khả dụng trong bản demo. Vui lòng liên hệ quản trị viên.",
      "success"
    );
  });

  document.getElementById("googleBtn").addEventListener("click", () => {
    setBanner(loginBanner, "Đăng nhập với Google chưa được cấu hình trong bản demo này.");
  });
})();
