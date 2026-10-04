import { BRAND, FOOTER_COLUMNS } from "@/lib/site";
import { Logo } from "./Logo";
import Reveal from "./Reveal";

export default function Footer() {
  return (
    <footer className="footer" id="footer">
      <div className="container footer-top">
        <Reveal className="footer-brand">
          <Logo />
          <p className="muted">Phân tích báo cáo tài chính bằng tiếng Việt, có dẫn nguồn.</p>
        </Reveal>
        <div className="footer-cols">
          {FOOTER_COLUMNS.map((c, i) => (
            <Reveal key={c.title} delay={80 + i * 80}>
              <h4>{c.title}</h4>
              {c.links.map((l) => (
                <a key={l} href="#">{l}</a>
              ))}
            </Reveal>
          ))}
        </div>
      </div>
      <Reveal delay={200}>
        <div className="container footer-bottom muted">
          <span>© {new Date().getFullYear()} {BRAND}</span>
          <span>Bảo lưu mọi quyền</span>
        </div>
      </Reveal>
    </footer>
  );
}
