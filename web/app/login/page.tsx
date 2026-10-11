import { redirect } from "next/navigation";
import AuthForm from "@/components/AuthForm";
import { getUser } from "@/lib/server-user";

export const metadata = { title: "Đăng nhập" };

export default async function LoginPage() {
  if (await getUser()) redirect("/chat");
  return <AuthForm mode="login" />;
}
