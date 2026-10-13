import { redirect } from "next/navigation";
import AuthForm from "@/components/AuthForm";
import { getRegistrationMode, getUser } from "@/lib/server-user";

export const metadata = { title: "Đăng ký" };

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export default async function SignupPage({ searchParams }: Props) {
  if (await getUser()) redirect("/chat");
  const params = await searchParams;
  const invite = typeof params.invite === "string" ? params.invite.slice(0, 128) : "";
  // Unknown (backend down): assume the default, invite-only.
  const registration = (await getRegistrationMode()) ?? "invite";
  return <AuthForm mode="signup" registration={registration} invite={invite} />;
}
