"use client";

import PageLink from "@/components/PageLink";
import { FormEvent, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Logo } from "./Logo";
import AuthAside from "./AuthAside";
import { BRAND } from "@/lib/site";

type Mode = "login" | "signup";
type Registration = "invite" | "open" | "closed";
type Errors = { name?: string; email?: string; password?: string; invite?: string; form?: string };

// Same limits as the FastAPI backend (backend/src/agentfinx/api: app.py, auth.py).
const MIN_PASSWORD = 8;
const MAX_PASSWORD = 256;

function strength(p: string) {
  let s = 0;
  if (p.length >= MIN_PASSWORD) s++;
  if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++;
  if (/\d/.test(p)) s++;
  if (/[^A-Za-z0-9]/.test(p)) s++;
  return s;
}

function validate(isLogin: boolean, name: string, email: string, password: string, invite: string | null): Errors {
  const errors: Errors = {};
  if (invite !== null && !invite.trim()) errors.invite = "Vui lòng nhập mã mời.";
  if (!isLogin && (!name.trim() || name.length > 120)) errors.name = "Vui lòng nhập họ tên (tối đa 120 ký tự).";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email.trim())) errors.email = "Email không hợp lệ.";
  if (isLogin ? !password : password.length < MIN_PASSWORD || password.length > MAX_PASSWORD) {
    errors.password = isLogin ? "Vui lòng nhập mật khẩu." : `Mật khẩu cần từ ${MIN_PASSWORD} đến ${MAX_PASSWORD} ký tự.`;
  }
  return errors;
}

// Backend error codes -> the field they belong to.
function errorsFrom(err: unknown): Errors {
  if (!(err instanceof ApiError)) return { form: "Đã có lỗi xảy ra." };
  if (Object.keys(err.fields).length) return err.fields as Errors;
  switch (err.code) {
    case "invalid_email":
      return { email: err.message };
    case "invalid_name":
      return { name: err.message };
    case "invalid_invite":
      return { invite: err.message };
    default:
      // registration_failed, too_many_attempts, registration_closed, ...
      return { form: err.message };
  }
}

type Props = { mode: Mode; registration?: Registration; invite?: string };

export default function AuthForm({ mode, registration = "invite", invite = "" }: Props) {
  const isLogin = mode === "login";
  const needsInvite = !isLogin && registration === "invite";
  const closed = !isLogin && registration === "closed";
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [shake, setShake] = useState(false);
  const level = strength(password);

  function fail(next: Errors) {
    setErrors(next);
    setShake(true);
    setTimeout(() => setShake(false), 450);
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (loading || done) return;
    const f = new FormData(e.currentTarget);
    const name = String(f.get("name") ?? "");
    const email = String(f.get("email") ?? "");
    const remember = f.get("remember") === "on";
    const inviteCode = needsInvite ? String(f.get("invite_code") ?? "") : null;
    const local = validate(isLogin, name, email, password, inviteCode);
    if (Object.keys(local).length) {
      fail(local);
      return;
    }
    setLoading(true);
    setErrors({});
    try {
      await api(isLogin ? "/api/login" : "/api/register", {
        method: "POST",
        body: isLogin
          ? { email, password, remember }
          : { name, email, password, remember, invite_code: (inviteCode ?? "").trim() },
        allowUnauthenticated: true,
      });
      setDone(true);
      // A full load, not a client navigation: the server re-reads the new
      // session cookie, and no router update queued before login can later
      // rewrite the address bar of the chat page.
      document.documentElement.classList.add("leaving");
      window.location.replace("/chat");
    } catch (err) {
      fail(errorsFrom(err));
      setLoading(false);
    }
  }

  return (
    <main className="auth-split">
      <section className="auth-pane">
        <div className="auth-top"><Logo /></div>

        <div className={`auth-box ${shake ? "shake" : ""}`}>
          <h1 className="auth-title">{isLogin ? <>Chào mừng đến với <em>{BRAND}</em></> : <>Tạo tài khoản <em>{BRAND}</em></>}</h1>
          {isLogin && <p className="muted auth-sub">Đăng nhập để tiếp tục phân tích báo cáo.</p>}
          {needsInvite && (
            <p className="muted auth-sub">Tài khoản mới cần mã mời. Hãy dùng đúng email được mời.</p>
          )}

          {closed ? (
            <p className="form-error" role="status">Hiện chưa mở đăng ký tài khoản mới.</p>
          ) : (
          <form onSubmit={onSubmit} className="form" noValidate>
            {needsInvite && (
              <div className={`field-wrap ${errors.invite ? "bad" : ""}`}>
                <label htmlFor="invite_code">Mã mời</label>
                <input id="invite_code" name="invite_code" type="text" autoComplete="off" spellCheck={false}
                  maxLength={128} defaultValue={invite} placeholder="Mã trong thư mời" />
                {errors.invite && <small>{errors.invite}</small>}
              </div>
            )}
            {!isLogin && (
              <div className={`field-wrap ${errors.name ? "bad" : ""}`}>
                <label htmlFor="name">Họ tên</label>
                <input id="name" name="name" type="text" autoComplete="name" maxLength={120} placeholder="Nguyễn Văn A" />
                {errors.name && <small>{errors.name}</small>}
              </div>
            )}
            <div className={`field-wrap ${errors.email ? "bad" : ""}`}>
              <label htmlFor="email">Email</label>
              <input id="email" name="email" type="email" autoComplete="email" maxLength={254} placeholder="ban@congty.vn" />
              {errors.email && <small>{errors.email}</small>}
            </div>
            <div className={`field-wrap ${errors.password ? "bad" : ""}`}>
              <label htmlFor="password">Mật khẩu</label>
              <div className="pw">
                <input
                  id="password" type={show ? "text" : "password"} value={password} maxLength={MAX_PASSWORD}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={isLogin ? "current-password" : "new-password"}
                  placeholder={isLogin ? "Nhập mật khẩu" : `Ít nhất ${MIN_PASSWORD} ký tự`}
                />
                <button type="button" className="pw-toggle" onClick={() => setShow((v) => !v)}>{show ? "Ẩn" : "Hiện"}</button>
              </div>
              {errors.password && <small>{errors.password}</small>}
              {!isLogin && (
                <div className="meter" aria-hidden="true">
                  {[1, 2, 3, 4].map((n) => <i key={n} className={level >= n ? `on l${level}` : ""} />)}
                </div>
              )}
            </div>

            <label className="remember">
              <input type="checkbox" name="remember" defaultChecked={!isLogin} />
              <span>Ghi nhớ đăng nhập</span>
            </label>

            {errors.form && <p className="form-error" role="alert">{errors.form}</p>}

            <button type="submit" className="btn btn-dark btn-block btn-lg" disabled={loading || done}>
              {loading || done ? <span className="spin" /> : null}
              {done ? "Đang chuyển hướng" : loading ? "Vui lòng đợi" : isLogin ? "Đăng nhập" : "Tạo tài khoản"}
            </button>
          </form>
          )}

          <p className="auth-switch muted">
            {isLogin ? "Chưa có tài khoản? " : "Đã có tài khoản? "}
            <PageLink href={isLogin ? "/signup" : "/login"}>{isLogin ? "Đăng ký" : "Đăng nhập"}</PageLink>
          </p>
        </div>
      </section>
      <AuthAside />
    </main>
  );
}
