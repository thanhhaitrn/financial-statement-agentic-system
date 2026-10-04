import { NextResponse } from "next/server";
import bcrypt from "bcryptjs";
import { prisma } from "@/lib/db";
import { checkEmail, checkName, checkPassword, createSession, normalizeEmail } from "@/lib/auth";

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  const name = String(body?.name ?? "").trim();
  const email = normalizeEmail(body?.email);
  const password = String(body?.password ?? "");

  const errors = {
    name: checkName(name),
    email: checkEmail(email),
    password: checkPassword(password),
  };
  if (errors.name || errors.email || errors.password) {
    return NextResponse.json({ errors }, { status: 400 });
  }

  if (await prisma.user.findUnique({ where: { email } })) {
    return NextResponse.json({ errors: { email: "Email này đã được đăng ký." } }, { status: 409 });
  }

  try {
    const user = await prisma.user.create({
      data: { name, email, passwordHash: await bcrypt.hash(password, 11) },
    });
    await createSession(user.id);
    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ errors: { form: "Không thể tạo tài khoản, vui lòng thử lại." } }, { status: 500 });
  }
}
