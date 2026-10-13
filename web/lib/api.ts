// Browser-side client for the FastAPI backend, reached through the same-origin
// /api proxy. The session is an HttpOnly cookie: this code never sees, stores
// or forwards a token.

export class ApiError extends Error {
  status: number;
  code: string;
  fields: Record<string, string>;

  constructor(status: number, code: string, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

const FIELD_MESSAGES: Record<string, string> = {
  name: "Vui lòng nhập họ tên (tối đa 120 ký tự).",
  email: "Email không hợp lệ.",
  password: "Mật khẩu cần từ 8 đến 256 ký tự.",
  title: "Tên cuộc trò chuyện cần từ 1 đến 120 ký tự.",
};

function errorFromBody(status: number, data: unknown): ApiError {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { code?: string; message?: string };
    return new ApiError(status, d.code || "error", d.message || "Đã có lỗi xảy ra.");
  }
  if (Array.isArray(detail)) {
    // FastAPI request validation: [{loc: ["body", "<field>"], ...}]
    const fields: Record<string, string> = {};
    for (const item of detail as { loc?: unknown[] }[]) {
      const field = String(item?.loc?.[item.loc.length - 1] ?? "");
      if (FIELD_MESSAGES[field] && !fields[field]) fields[field] = FIELD_MESSAGES[field];
    }
    return new ApiError(status, "invalid_request", "Dữ liệu gửi lên không hợp lệ.", fields);
  }
  return new ApiError(status, "error", `Lỗi máy chủ (${status}).`);
}

type ApiOptions = {
  method?: string;
  body?: unknown;
  form?: FormData;
  /** Do not redirect to /login on 401 (login and register forms). */
  allowUnauthenticated?: boolean;
};

export async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
  const { method = "GET", body, form, allowUnauthenticated = false } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  let payload: BodyInit | undefined;
  if (form !== undefined) {
    payload = form; // the browser sets the multipart boundary itself
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, { method, credentials: "same-origin", headers, body: payload, cache: "no-store" });
  } catch {
    throw new ApiError(0, "network", "Không kết nối được máy chủ.");
  }
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401 && !allowUnauthenticated) {
    window.location.replace("/login");
    throw new ApiError(401, "unauthenticated", "Bạn chưa đăng nhập.");
  }
  if (!response.ok) throw errorFromBody(response.status, data);
  return data as T;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * POST that survives a dropped connection: a retry re-sends the same body and
 * idempotency key, so the server returns the original run or import instead
 * of doing the work (or charging) twice.
 */
export async function postIdempotent<T = unknown>(path: string, body: unknown): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await api<T>(path, { method: "POST", body });
    } catch (err) {
      if (err instanceof ApiError && err.status === 0 && attempt < 2) {
        await sleep(3000);
        continue;
      }
      throw err;
    }
  }
}

export { sleep };
