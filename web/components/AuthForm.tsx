"use client";

import PageLink from "@/components/PageLink";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { go } from "@/lib/transition";
import { Logo } from "./Logo";
import AuthAside from "./AuthAside";
import { BRAND } from "@/lib/site";

type Mode = "login" | "signup";
type Errors = { name?: string | null; email?: string | null; password?: string | null; form?: string };

function strength(p: string) {
  let s = 0;
  if (p.length >= 8) s++;
  if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++;
  if (/\d/.test(p)) s++;
  if (/[^A-Za-z0-9]/.test(p)) s++;
  return s;
}

export default function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const isLogin = mode === "login";
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [shake, setShake] = useState(false);
  const level = strength(password);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (loading || done) return;
    const f = new FormData(e.currentTarget);
    setLoading(true);
    setErrors({});
    try {
      const res = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: f.get("name"), email: f.get("email"), password }),
      });
      const data = await res.json();
      if (!res.ok) {
        setErrors(data.errors ?? { form: "Đã có lỗi xảy ra." });
        setShake(true);
        setTimeout(() => setShake(false), 450);
        setLoading(false);
        return;
      }
      setDone(true);
      router.refresh();
      go(router, "/chat", true);
    } catch {
      setErrors({ form: "Không thể kết nối máy chủ." });
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

          <form onSubmit={onSubmit} className="form" noValidate>
            {!isLogin && (
              <div className={`field-wrap ${errors.name ? "bad" : ""}`}>
                <label htmlFor="name">Họ tên</label>
                <input id="name" name="name" type="text" autoComplete="name" placeholder="Nguyễn Văn A" />
                {errors.name && <small>{errors.name}</small>}
              </div>
            )}
            <div className={`field-wrap ${errors.email ? "bad" : ""}`}>
              <label htmlFor="email">Email</label>
              <input id="email" name="email" type="email" autoComplete="email" placeholder="ban@congty.vn" />
              {errors.email && <small>{errors.email}</small>}
            </div>
            <div className={`field-wrap ${errors.password ? "bad" : ""}`}>
              <label htmlFor="password">Mật khẩu</label>
              <div className="pw">
                <input
                  id="password" type={show ? "text" : "password"} value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={isLogin ? "current-password" : "new-password"}
                  placeholder={isLogin ? "Nhập mật khẩu" : "Ít nhất 8 ký tự"}
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

            {errors.form && <p className="form-error" role="alert">{errors.form}</p>}

            <button type="submit" className="btn btn-dark btn-block btn-lg" disabled={loading || done}>
              {loading || done ? <span className="spin" /> : null}
              {done ? "Đang chuyển hướng" : loading ? "Vui lòng đợi" : isLogin ? "Đăng nhập" : "Tạo tài khoản"}
            </button>
          </form>

          <p className="auth-switch muted">
            {isLogin ? "Chưa có tài khoản? " : "Đã có tài khoản? "}
            <PageLink href={isLogin ? "/signup" : "/login"}>{isLogin ? "Đăng ký" : "Đăng nhập"}</PageLink>
          </p>
        </div>

        <p className="auth-legal muted">Bằng việc tiếp tục, bạn đồng ý với Điều khoản sử dụng và Chính sách bảo mật.</p>
      </section>
      <AuthAside />
    </main>
  );
}
