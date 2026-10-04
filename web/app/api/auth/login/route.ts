import { NextResponse } from "next/server";
import bcrypt from "bcryptjs";
import { prisma } from "@/lib/db";
import { createSession, normalizeEmail } from "@/lib/auth";

// Hash giả để thời gian phản hồi không lộ việc email có tồn tại hay không
const DUMMY = bcrypt.hashSync("khong-phai-mat-khau-that", 11);

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  const email = normalizeEmail(body?.email);
  const password = String(body?.password ?? "");

  const user = email ? await prisma.user.findUnique({ where: { email } }) : null;
  const ok = await bcrypt.compare(password, user?.passwordHash ?? DUMMY);

  if (!user || !ok) {
    return NextResponse.json({ errors: { form: "Email hoặc mật khẩu không đúng." } }, { status: 401 });
  }
  await createSession(user.id);
  return NextResponse.json({ ok: true });
}
