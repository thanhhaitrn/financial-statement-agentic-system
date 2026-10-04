import { SignJWT, jwtVerify } from "jose";
import { cookies } from "next/headers";
import { prisma } from "./db";

const COOKIE = "afx_session";
const MAX_AGE = 60 * 60 * 24 * 7;
const secret = () =>
  new TextEncoder().encode(process.env.AUTH_SECRET ?? "dev-only-secret-change-me-0123456789");

export async function createSession(userId: string) {
  const token = await new SignJWT({})
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(userId)
    .setIssuedAt()
    .setExpirationTime("7d")
    .sign(secret());
  cookies().set(COOKIE, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE,
  });
}

export function destroySession() {
  cookies().delete(COOKIE);
}

export async function getUser() {
  const token = cookies().get(COOKIE)?.value;
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secret());
    if (!payload.sub) return null;
    return await prisma.user.findUnique({
      where: { id: payload.sub },
      select: { id: true, name: true, email: true },
    });
  } catch {
    return null;
  }
}

export const normalizeEmail = (v: unknown) => String(v ?? "").trim().toLowerCase();

export function checkEmail(v: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v) && v.length <= 120 ? null : "Email không hợp lệ.";
}
export function checkPassword(v: string) {
  if (v.length < 8) return "Mật khẩu cần ít nhất 8 ký tự.";
  if (v.length > 72) return "Mật khẩu tối đa 72 ký tự.";
  return null;
}
export function checkName(v: string) {
  return v.length >= 2 && v.length <= 60 ? null : "Họ tên cần từ 2 đến 60 ký tự.";
}
