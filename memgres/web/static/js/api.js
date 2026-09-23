// The panel talks only to /ui/api, with its session cookie. Every call that
// changes something carries the session's CSRF token; the server refuses it
// otherwise.

let csrf = null;
export const setCsrf = (value) => { csrf = value; };

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrf) headers["X-Memgres-CSRF"] = csrf;
  const r = await fetch(`/ui/api${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  const text = await r.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (!r.ok) throw new ApiError(r.status, data && typeof data === "object" ? data.detail : data);
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body) => api("POST", path, body ?? {});
export const patch = (path, body) => api("PATCH", path, body ?? {});
export const put = (path, body) => api("PUT", path, body ?? {});
export const del = (path) => api("DELETE", path);
