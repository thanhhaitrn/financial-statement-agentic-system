"use client";

import PageLink from "@/components/PageLink";
import { useEffect, useState } from "react";
import { Logo } from "./Logo";

export default function Navbar({ loggedIn }: { loggedIn: boolean }) {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 12);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);

  return (
    <header className={`nav ${scrolled ? "scrolled" : ""}`}>
      <div className="container nav-inner">
        <Logo />
        <nav className="nav-links" aria-label="Chính">
          <a href="#features">Tính năng</a>
          <a href="#how">Cách hoạt động</a>
        </nav>
        <div className="nav-actions">
          {loggedIn ? (
            <PageLink href="/chat" className="btn btn-dark">Vào ứng dụng</PageLink>
          ) : (
            <>
              <PageLink href="/login" className="btn btn-ghost">Đăng nhập</PageLink>
              <a href="#footer" className="btn btn-outline">Liên hệ</a>
              <PageLink href="/signup" className="btn btn-dark">Đăng ký</PageLink>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
