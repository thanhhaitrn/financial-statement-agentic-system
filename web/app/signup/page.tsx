import { redirect } from "next/navigation";
import AuthForm from "@/components/AuthForm";
import { getUser } from "@/lib/auth";

export const metadata = { title: "Đăng ký" };

export default async function SignupPage() {
  if (await getUser()) redirect("/chat");
  return <AuthForm mode="signup" />;
}
