import { BRAND, DOCS_URL, SOURCE_URL, docsPage } from "@/lib/site";

type FooterLink = { label: string; href: string; external?: boolean };

// The columns follow the navbar: Sản phẩm, Mã nguồn, Tài liệu.
const COLUMNS: { title: string; links: FooterLink[] }[] = [
  {
    title: "Sản phẩm",
    links: [
      { label: "Tổng quan", href: "#san-pham" },
      { label: "Tính năng", href: "#tinh-nang" },
    ],
  },
  {
    title: "Mã nguồn",
    links: [
      { label: "GitHub", href: SOURCE_URL, external: true },
      { label: "Cài đặt", href: `${SOURCE_URL}#readme`, external: true },
    ],
  },
  {
    title: "Tài liệu",
    links: [
      { label: "Tất cả tài liệu", href: DOCS_URL, external: true },
      { label: "Kiến trúc", href: docsPage("architecture"), external: true },
      { label: "Vận hành", href: docsPage("operations"), external: true },
      { label: "Đánh giá", href: docsPage("evaluation"), external: true },
      { label: "Bảo mật", href: docsPage("security"), external: true },
    ],
  },
];

function FooterAnchor({ label, href, external }: FooterLink) {
  if (external) return <a href={href} target="_blank" rel="noopener noreferrer">{label}</a>;
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
