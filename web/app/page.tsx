import PageLink from "@/components/PageLink";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import Reveal from "@/components/Reveal";
import HeroDemo from "@/components/HeroDemo";
import { getUser } from "@/lib/auth";

const WORDS = ["Hệ", "thống", "phân", "tích", "báo", "cáo", "tài", "chính"];
const STEPS = [
  ["01", "Tải báo cáo lên"],
  ["02", "Đặt câu hỏi"],
  ["03", "Nhận phân tích"],
];

export default async function HomePage() {
  const user = await getUser();

  return (
    <>
      <Navbar loggedIn={!!user} />
      <main>
        <section className="hero container">
          <h1 className="hero-title">
            {WORDS.map((w, i) => (
              <span key={w + i}><span className="w"><span style={{ animationDelay: `${120 + i * 55}ms` }}>{w}</span></span>{" "}</span>
            ))}
            <span className="w"><em style={{ animationDelay: `${120 + WORDS.length * 55}ms` }}>AgentFinX</em></span>
          </h1>
          <p className="hero-sub fade-up" style={{ ["--d" as string]: "700ms" }}>
            Tải lên báo cáo, đặt câu hỏi bằng ngôn ngữ tự nhiên và nhận câu trả lời kèm nguồn trích dẫn trong vài giây.
          </p>
          <div className="hero-cta fade-up" style={{ ["--d" as string]: "820ms" }}>
            <PageLink href={user ? "/chat" : "/signup"} className="btn btn-dark btn-lg">
              {user ? "Vào ứng dụng" : "Bắt đầu miễn phí"}
            </PageLink>
            <a href="#features" className="btn btn-soft btn-lg">Xem thêm</a>
          </div>
        </section>

        <div className="container demo-wrap fade-up" style={{ ["--d" as string]: "950ms" }}>
          <HeroDemo />
        </div>


        <section className="section container" id="features">
          <Reveal><h2 className="h2">Được thiết kế cho <em>công việc phân tích</em> thực tế</h2></Reveal>
          <div className="bento">
            <Reveal className="cell c2" delay={0}>
              <div className="cell-in">
                <div className="viz doc-viz">
                  <div className="chips"><span className="mono">Bảng cân đối</span><span className="mono">KQKD</span><span className="mono">Lưu chuyển tiền tệ</span><span className="mono">Thuyết minh</span></div>
                  <div className="lines"><i /><i /><i /><i /><i /><b /></div>
                </div>
                <h3>Đọc hiểu cả bộ báo cáo</h3>
                <p className="muted">Các bảng được nhận diện và liên kết với nhau, nên câu hỏi chéo giữa nhiều báo cáo vẫn cho kết quả chính xác.</p>
              </div>
            </Reveal>
            <Reveal className="cell" delay={90}>
              <div className="cell-in">
                <div className="viz quote-viz">
                  <p>&quot;Nợ vay ngắn hạn giảm 18% so với đầu năm.&quot;</p>
                  <span className="mono tag">Trang 14, Thuyết minh 5</span>
                </div>
                <h3>Trả lời có dẫn nguồn</h3>
                <p className="muted">Mỗi kết luận chỉ rõ vị trí trong tài liệu gốc để bạn kiểm chứng ngay.</p>
              </div>
            </Reveal>
            <Reveal className="cell" delay={0}>
              <div className="cell-in">
                <div className="viz metric-viz">
                  {[["ROE", "14,2%", "71%"], ["ROA", "6,8%", "34%"], ["Nợ/VCSH", "0,9", "45%"]].map(([k, v, w]) => (
                    <div key={k} className="metric"><span className="mono">{k}</span><span className="track"><u style={{ ["--w" as string]: w }} /></span><b className="mono">{v}</b></div>
                  ))}
                </div>
                <h3>Chỉ số tính sẵn</h3>
                <p className="muted">ROE, ROA, hệ số nợ và biên lợi nhuận được tính tự động, đối chiếu qua nhiều kỳ.</p>
              </div>
            </Reveal>
            <Reveal className="cell c2" delay={90}>
              <div className="cell-in">
                <div className="viz line-viz">
                  <svg viewBox="0 0 400 120" preserveAspectRatio="none">
                    <path className="ln a" pathLength={1} d="M0 95 C50 90 80 70 130 72 S210 40 260 44 S350 20 400 12" />
                    <path className="ln b" pathLength={1} d="M0 100 C60 98 90 90 140 86 S220 76 270 70 S350 62 400 56" />
                  </svg>
                </div>
                <h3>So sánh nhiều kỳ, nhiều doanh nghiệp</h3>
                <p className="muted">Đặt hai doanh nghiệp cạnh nhau và xem xu hướng thay đổi theo từng quý chỉ với một câu hỏi.</p>
              </div>
            </Reveal>
          </div>
        </section>

        <section className="section container" id="how">
          <Reveal><h2 className="h2">Ba bước để có <em>câu trả lời</em></h2></Reveal>
          <div className="steps">
            {STEPS.map(([n, t], i) => (
              <Reveal key={n} className="step" delay={i * 110}>
                <div className="step-line" />
                <span className="mono muted">{n}</span>
                <h3>{t}</h3>
              </Reveal>
            ))}
          </div>
        </section>

        <section className="container cta-wrap">
          <Reveal>
            <div className="cta">
              <h2>Phân tích báo cáo đầu tiên của bạn <em>ngay hôm nay</em></h2>
              <PageLink href={user ? "/chat" : "/signup"} className="btn btn-light btn-lg">
                {user ? "Vào ứng dụng" : "Tạo tài khoản miễn phí"}
              </PageLink>
            </div>
          </Reveal>
        </section>
      </main>
      <Footer />
    </>
  );
}
