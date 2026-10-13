import Footer from "@/components/Footer";
import PageLink from "@/components/PageLink";
import Navbar from "@/components/Navbar";
import Reveal from "@/components/Reveal";
import { CitedAnswer, PeriodComparison, VerifiedNews } from "@/components/landing/FeatureVisuals";
import ProductDemo from "@/components/landing/ProductDemo";
import { SAMPLE } from "@/components/landing/sample";
import { getUser } from "@/lib/server-user";

// Figures are generated isometric line drawings in public/landing.
const HIGHLIGHTS = [
  {
    figure: "/landing/fig-report.svg",
    title: "Chuyên cho báo cáo tài chính",
    body: "Đọc bảng cân đối, kết quả kinh doanh, lưu chuyển tiền tệ, thuyết minh thành dữ liệu có cấu trúc.",
  },
  {
    figure: "/landing/fig-agents.svg",
    title: "Vận hành bằng agent",
    body: "Các agent chuyên về từng khía cạnh tài chính, có thể điều chỉnh theo phong cách phân tích của bạn.",
  },
  {
    figure: "/landing/fig-source.svg",
    title: "Kiểm chứng từng con số",
    body: "Mỗi kết luận dẫn về bảng hoặc thuyết minh gốc, kèm số trang khi báo cáo có.",
  },
];

const FEATURES = [
  {
    title: "Trả lời kèm nguồn",
    body: "Mỗi kết luận chỉ rõ bảng hoặc thuyết minh lấy số liệu, kèm số trang khi báo cáo có, để đối chiếu với bản gốc.",
    visual: <CitedAnswer />,
  },
  {
    title: "So sánh cùng kỳ",
    body: "Chỉ tiêu được đặt cạnh kỳ trước với cùng đơn vị, nên biến động hiện ra ngay mà không phải tự cộng trừ.",
    visual: <PeriodComparison />,
  },
  {
    title: "Tin tức có mức xác nhận",
    body: "Tin doanh nghiệp từ Vietstock, mỗi sự kiện kèm câu trích nguyên văn và mức xác nhận do hệ thống kiểm tra.",
    visual: <VerifiedNews />,
  },
];

export default async function HomePage() {
  const user = await getUser();

  return (
    <>
      <Navbar loggedIn={!!user} />
      <main>
        <section className="hero container">
          <h1 className="hero-title fade-up">
            Phân tích báo cáo tài chính
            <br />
            cùng AI agent
          </h1>
          <div className="hero-row fade-up" style={{ ["--d" as string]: "160ms" }}>
            <p className="hero-sub">
              Tự động hóa quy trình phân tích tài chính doanh nghiệp bằng hệ thống trí tuệ nhân tạo.
            </p>
            <PageLink className="btn btn-dark hero-new" href={user ? "/chat" : "/signup"}>
              {user ? "Vào ứng dụng" : "Tạo tài khoản"} <span aria-hidden="true">→</span>
            </PageLink>
          </div>
        </section>

        <section className="demo-section fade-up" style={{ ["--d" as string]: "280ms" }}>
          <ProductDemo />
        </section>

        <section className="section container" id="san-pham">
          <Reveal>
            <h2 className="statement">
              <span>Một cách mới để đọc báo cáo tài chính.</span> Xây dựng riêng cho doanh nghiệp Việt Nam,
              AgentFinX để các AI agent trích xuất số liệu, tính chỉ số và đối chiếu cùng kỳ — bạn chỉ việc đặt
              câu hỏi.
            </h2>
          </Reveal>
          <div className="highlights">
            {HIGHLIGHTS.map((item, i) => (
              <Reveal key={item.title} className="highlight" delay={i * 90}>
                <span className="highlight-n mono">HÌNH 0.{i + 1}</span>
                <div className="highlight-fig">
                  <img src={item.figure} alt="" />
                </div>
                <h3>{item.title}</h3>
                <p className="muted">{item.body}</p>
              </Reveal>
            ))}
          </div>
        </section>

        <section className="section container" id="tinh-nang">
          <Reveal>
            <h2 className="h2">Mỗi câu trả lời đều kiểm chứng được</h2>
            <p className="section-note muted">Ví dụ dùng dữ liệu minh họa của {SAMPLE.company} ({SAMPLE.ticker}).</p>
          </Reveal>
          <div className="features">
            {FEATURES.map((feature, i) => (
              <Reveal key={feature.title} className="feature" delay={i * 90}>
                {feature.visual}
                <h3>{feature.title}</h3>
                <p className="muted">{feature.body}</p>
              </Reveal>
            ))}
          </div>
        </section>

        <section className="cta container">
          <Reveal>
            <h2 className="cta-title">Thử AgentFinX ngay.</h2>
            <PageLink className="btn btn-dark btn-lg" href={user ? "/chat" : "/signup"}>
              {user ? "Vào ứng dụng" : "Dùng thử demo"} <span aria-hidden="true">→</span>
            </PageLink>
          </Reveal>
        </section>
      </main>
      <Footer />
    </>
  );
}
