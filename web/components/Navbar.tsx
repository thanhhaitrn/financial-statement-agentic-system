"use client";

import PageLink from "@/components/PageLink";
import { DOCS_URL, SOURCE_URL } from "@/lib/site";
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
        <div className="nav-right">
          <nav className="nav-links" aria-label="Chính">
            <a href="#san-pham">Sản phẩm</a>
            <a href={SOURCE_URL} target="_blank" rel="noopener noreferrer">Mã nguồn</a>
            <a href={DOCS_URL} target="_blank" rel="noopener noreferrer">Tài liệu</a>
          </nav>
          <PageLink href={loggedIn ? "/chat" : "/signup"} className="btn btn-dark">
            {loggedIn ? "Vào ứng dụng" : "Dùng thử demo"}
          </PageLink>
        </div>
      </div>
    </header>
  );
}
