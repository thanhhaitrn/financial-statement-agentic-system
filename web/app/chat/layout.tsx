import { redirect } from "next/navigation";
import { getUser } from "@/lib/auth";
import "./chat.css";

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  if (!(await getUser())) redirect("/login");
  return children;
}
