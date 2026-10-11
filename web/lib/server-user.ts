import { cookies } from "next/headers";

// Name of the HttpOnly cookie set by the FastAPI backend (webapp/auth.py).
const SESSION_COOKIE = "finx_session";
// Inlined at build time from next.config.mjs, the same value as the /api proxy.
const API = (process.env.FINX_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

export type ServerUser = { id: string; name: string; email: string };
export type RegistrationMode = "invite" | "open" | "closed";

/**
 * "anonymous" only when FastAPI says so (401) or there is no cookie;
 * "unavailable" when it could not answer. A down backend must not look like
 * a logout: that would bounce a signed-in user to the login form.
 */
export type ServerSession =
  | { status: "authenticated"; user: ServerUser }
  | { status: "anonymous" }
  | { status: "unavailable" };

/**
 * The signed-in user, asked from FastAPI with the browser's session cookie.
 * Used only to choose redirects and CTAs on the server; FastAPI re-checks the
 * cookie on every API call, so a stale answer here can never grant access.
 */
export async function getSession(): Promise<ServerSession> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return { status: "anonymous" };
  try {
    const res = await fetch(`${API}/api/me`, {
      headers: { cookie: `${SESSION_COOKIE}=${token}`, accept: "application/json" },
      cache: "no-store",
    });
    if (res.status === 401) return { status: "anonymous" };
    if (!res.ok) return { status: "unavailable" };
    const data = (await res.json()) as { user?: ServerUser };
    return data.user ? { status: "authenticated", user: data.user } : { status: "unavailable" };
  } catch {
    return { status: "unavailable" };
  }
}

export async function getUser(): Promise<ServerUser | null> {
  const session = await getSession();
  return session.status === "authenticated" ? session.user : null;
}

/** How new accounts are created; null when the backend cannot be reached. */
export async function getRegistrationMode(): Promise<RegistrationMode | null> {
  try {
    const res = await fetch(`${API}/api/auth-config`, { cache: "no-store" });
    if (!res.ok) return null;
    const mode = ((await res.json()) as { registration?: string }).registration;
    return mode === "invite" || mode === "open" || mode === "closed" ? mode : null;
  } catch {
    return null;
  }
}
