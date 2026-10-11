(() => {
  "use strict";

  // Same-origin API. The session is an HttpOnly cookie set by the server, so
  // this page never reads, stores or forwards a token.

  const loginPanel = document.getElementById("loginPanel");
  const registerPanel = document.getElementById("registerPanel");
  const loginForm = document.getElementById("loginForm");
  const registerForm = document.getElementById("registerForm");
  const loginBanner = document.getElementById("loginBanner");
  const registerBanner = document.getElementById("registerBanner");
  const MIN_PASSWORD_LENGTH = 8;

  function showPanel(mode) {
    const isRegister = mode === "register";
    loginPanel.hidden = isRegister;
    registerPanel.hidden = !isRegister;
    (isRegister ? document.getElementById("registerName") : document.getElementById("loginEmail"))?.focus();
  }

  const params = new URLSearchParams(window.location.search);
  const wantsRegister = params.get("mode") === "register" || window.location.pathname === "/register";
  showPanel(wantsRegister ? "register" : "login");

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

  async function post(path, body) {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      });
    } catch (_) {
      throw new Error("Không thể kết nối máy chủ.");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      /* no body */
    }

    if (!response.ok) {
      const detail = data && data.detail;
      if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.message) {
        throw new Error(detail.message);
      }
      if (Array.isArray(detail)) throw new Error("Thông tin chưa hợp lệ, vui lòng kiểm tra lại.");
      throw new Error("Đã có lỗi xảy ra, vui lòng thử lại.");
    }
    return data;
  }

  // Already signed in? The cookie is invisible to scripts, so ask the server.
  fetch("/api/me", { credentials: "same-origin" })
    .then((response) => {
      if (response.ok) window.location.replace("/chat");
    })
    .catch(() => {});

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
      await post("/api/login", { email, password, remember });
      window.location.href = "/chat";
    } catch (err) {
      setBanner(loginBanner, err.message);
    } finally {
      submitBtn.classList.remove("is-loading");
      submitBtn.disabled = false;
    }
  });

  // Sign-up is invite-only by default; the invite link carries the code.
  const inviteFromUrl = new URLSearchParams(window.location.search).get("invite");
  if (inviteFromUrl) document.getElementById("registerInvite").value = inviteFromUrl.slice(0, 128);

  registerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFieldErrors(registerForm);
    setBanner(registerBanner, "");

    const name = document.getElementById("registerName").value.trim();
    const email = document.getElementById("registerEmail").value.trim();
    const password = document.getElementById("registerPassword").value;
    const confirm = document.getElementById("registerConfirm").value;
    const inviteCode = document.getElementById("registerInvite").value.trim();

    let hasError = false;
    if (!name) { setFieldError("registerName", "Vui lòng nhập họ tên."); hasError = true; }
    if (!email) { setFieldError("registerEmail", "Vui lòng nhập email."); hasError = true; }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setFieldError("registerPassword", `Mật khẩu tối thiểu ${MIN_PASSWORD_LENGTH} ký tự.`);
      hasError = true;
    }
    if (confirm !== password) { setFieldError("registerConfirm", "Mật khẩu xác nhận không khớp."); hasError = true; }
    if (hasError) return;

    const submitBtn = document.getElementById("registerSubmit");
    submitBtn.classList.add("is-loading");
    submitBtn.disabled = true;

    try {
      await post("/api/register", { name, email, password, remember: true, invite_code: inviteCode });
      window.location.href = "/chat";
    } catch (err) {
      setBanner(registerBanner, err.message);
    } finally {
      submitBtn.classList.remove("is-loading");
      submitBtn.disabled = false;
    }
  });

  document.getElementById("forgotPasswordLink").addEventListener("click", (e) => {
    e.preventDefault();
    setBanner(loginBanner, "Khôi phục mật khẩu chưa hỗ trợ trong bản chạy nội bộ. Vui lòng liên hệ quản trị viên.", "success");
  });

  document.getElementById("googleBtn").addEventListener("click", () => {
    setBanner(loginBanner, "Đăng nhập với Google chưa hỗ trợ trong bản chạy nội bộ.");
  });
})();
