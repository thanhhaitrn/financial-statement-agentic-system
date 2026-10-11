import { BRAND } from "@/lib/site";
import { Logo } from "./Logo";
import Reveal from "./Reveal";

export default function Footer() {
  return (
    <footer className="footer" id="footer">
      <div className="container footer-top">
        <Reveal className="footer-brand">
          <Logo />
          <p className="muted">Hỏi đáp báo cáo tài chính bằng tiếng Việt, có dẫn nguồn. Công cụ nội bộ của nhóm.</p>
        </Reveal>
      </div>
      <Reveal delay={120}>
        <div className="container footer-bottom muted">
          <span>© {new Date().getFullYear()} {BRAND}</span>
        </div>
      </Reveal>
    </footer>
  );
}
