import PageLink from "@/components/PageLink";
import { BRAND, DOCS_URL, SOURCE_URL, docsPage } from "@/lib/site";

type FooterLink = { label: string; href: string; kind?: "page" | "external" };

// The columns follow the navbar: Sản phẩm, Mã nguồn, Tài liệu.
const COLUMNS: { title: string; links: FooterLink[] }[] = [
  {
    title: "Sản phẩm",
    links: [
      { label: "Tổng quan", href: "#san-pham" },
      { label: "Tính năng", href: "#tinh-nang" },
      { label: "Dùng thử demo", href: "/signup", kind: "page" },
      { label: "Đăng nhập", href: "/login", kind: "page" },
    ],
  },
  {
    title: "Mã nguồn",
    links: [
      { label: "GitHub", href: SOURCE_URL, kind: "external" },
      { label: "Cài đặt", href: `${SOURCE_URL}#readme`, kind: "external" },
    ],
  },
  {
    title: "Tài liệu",
    links: [
      { label: "Tất cả tài liệu", href: DOCS_URL, kind: "external" },
      { label: "Kiến trúc", href: docsPage("architecture"), kind: "external" },
      { label: "Vận hành", href: docsPage("operations"), kind: "external" },
      { label: "Đánh giá", href: docsPage("evaluation"), kind: "external" },
      { label: "Bảo mật", href: docsPage("security"), kind: "external" },
    ],
  },
];

function FooterAnchor({ label, href, kind }: FooterLink) {
  if (kind === "page") return <PageLink href={href}>{label}</PageLink>;
  if (kind === "external") return <a href={href} target="_blank" rel="noopener noreferrer">{label}</a>;
  return <a href={href}>{label}</a>;
}

export default function Footer() {
  return (
    <footer className="footer">
      <div className="container footer-cols">
        {COLUMNS.map((column) => (
          <nav key={column.title} className="footer-col" aria-label={column.title}>
            <h4>{column.title}</h4>
            {column.links.map((link) => <FooterAnchor key={link.label} {...link} />)}
          </nav>
        ))}
      </div>
      <div className="container footer-bottom">© {new Date().getFullYear()} {BRAND}</div>
    </footer>
  );
}
