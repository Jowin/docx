// One small client for both services, through the console's proxy (vite.config.ts).

export type Service = "runtime" | "designtime";

export class ApiError extends Error {
  status: number;
  code: string;
  detail: unknown;
  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

const base = (s: Service) => `/api/${s}`;

async function parse(res: Response): Promise<unknown> {
  const text = await res.text();
  const type = res.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

export async function call<T = any>(service: Service, path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  const res = await fetch(base(service) + path, {
    ...rest,
    headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...(rest.headers || {}) },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const body: any = await parse(res);
  if (!res.ok) {
    // runtime errors: {error, message, detail}; design-time: {detail: {code, message, detail}}
    const d = body && typeof body === "object" ? (body.detail && typeof body.detail === "object" ? body.detail : body) : {};
    throw new ApiError(res.status, d.code || d.error || `http_${res.status}`, d.message || String(body || res.statusText), d.detail);
  }
  return body as T;
}

export const rt = {
  get: <T = any>(path: string) => call<T>("runtime", path),
  post: <T = any>(path: string, json: unknown) => call<T>("runtime", path, { method: "POST", json }),
};

export const dt = {
  get: <T = any>(path: string) => call<T>("designtime", path),
  post: <T = any>(path: string, json: unknown) => call<T>("designtime", path, { method: "POST", json }),
  put: <T = any>(path: string, json: unknown) => call<T>("designtime", path, { method: "PUT", json }),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}
