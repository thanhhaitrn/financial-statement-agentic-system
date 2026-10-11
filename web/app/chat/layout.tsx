import { redirect } from "next/navigation";
import { LogoMark } from "@/components/Logo";
import { getSession } from "@/lib/server-user";
import "./chat.css";

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();
  if (session.status === "anonymous") redirect("/login");
  if (session.status === "unavailable") {
    // Not a logout: the cookie may be fine, the backend just did not answer.
    return (
      <main className="service-down" role="alert">
        <LogoMark size={40} />
        <h1>Không kết nối được máy chủ</h1>
        <p className="muted">Máy chủ AgentFinX đang không phản hồi. Phiên đăng nhập của bạn vẫn được giữ.</p>
        <a className="btn btn-dark" href="/chat">Thử lại</a>
      </main>
    );
  }
  return children;
}
