import { redirect } from "next/navigation";
import AuthForm from "@/components/AuthForm";
import { getUser } from "@/lib/auth";

export const metadata = { title: "Đăng nhập" };

export default async function LoginPage() {
  if (await getUser()) redirect("/chat");
  return <AuthForm mode="login" />;
}
