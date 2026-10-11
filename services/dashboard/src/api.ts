// Everything the dashboard asks of the gateway goes through this file.
//
// Requests are same-origin, so the browser attaches the session cookie by
// itself. The cookie is HttpOnly: this code never sees it, and neither would a
// script injected into the page.

export type Role = "admin" | "member";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  active: boolean;
  created_at: string;
}

export interface Tool {
  name: string;
  title: string | null;
  description: string | null;
  read_only: boolean;
}

export type ServerStatus = "connected" | "unreachable" | "disabled" | "pending";
export type AuthType = "none" | "bearer" | "header";
export type MemberAccess = "all" | "read_only" | "none";

/** What to run for a server the platform hosts itself. */
export interface Deployment {
  image: string;
  args: string[];
  env: Record<string, string>;
  port: number;
  path: string;
  health_path: string;
  internet: boolean;
}

export type HostedStatus = "deploying" | "running" | "failed" | "deleting";

export interface Server {
  id: string;
  slug: string;
  name: string;
  enabled: boolean;
  status: ServerStatus;
  error: string | null;
  checked_at: string | null;
  latency_ms: number | null;
  protocol_version: string | null;
  member_access: MemberAccess;
  // True for the server of a store created from the dashboard.
  managed: boolean;
  // Set for a server the platform runs from a container image. Members see
  // only how the deployment is going; admins also see what is deployed.
  hosted: ({ status: HostedStatus; detail: string | null } & Partial<Deployment> & { namespace?: string }) | null;
  tools: Tool[];
  skipped_tools: { name: string; reason: string }[];
  // The three below are sent to admins only. The credential itself is never sent.
  url?: string;
  timeout_seconds?: number;
  auth?: { type: AuthType; header: string | null; credential_set: boolean };
}

export interface ServerInput {
  slug?: string;
  name?: string;
  url?: string;
  hosted?: Deployment;
  auth_type?: AuthType;
  auth_header?: string | null;
  credential?: string;
  timeout_seconds?: number;
  member_access?: MemberAccess;
  enabled?: boolean;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  user_id: string;
  user_email: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  // Present only in the response to creating the key.
  key?: string;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  expires_at: string;
  link?: string;
}

export interface AuditEntry {
  id: number;
  at: string;
  user_id: string | null;
  user_email: string;
  api_key_prefix: string | null;
  source: "mcp" | "chat";
  server: string | null;
  tool: string | null;
  duration_ms: number;
  outcome: "ok" | "error" | "timeout" | "unreachable" | "denied" | "limited";
  error: string | null;
}

/** One server's traffic over a period, worked out from the audit log. */
export interface ServerStats {
  server: string;
  calls: number;
  failures: number;
  median_ms: number;
  p95_ms: number;
}

export interface Config {
  public_url: string;
  mcp_url: string;
  mcp_search_url: string;
  oauth_enabled: boolean;
  chat_tool_search_above: number;
  chat_enabled: boolean;
  chat_model: string;
  rate_limit_per_minute: number;
  stores_enabled: boolean;
  hosted_enabled: boolean;
  hosted_image_prefixes: string[];
  max_stores_per_user: number;
  // For example "http://{slug}.localhost".
  store_url_template: string;
}

export type StoreStatus = "provisioning" | "ready" | "failed" | "deleting";

export interface Store {
  id: string;
  slug: string;
  name: string;
  status: StoreStatus;
  // What is happening right now or, for a failed store, why it failed.
  detail: string | null;
  url: string | null;
  namespace: string;
  tool_prefix: string;
  owner_email: string;
  created_at: string;
  can_manage: boolean;
}

/** An MCP client that signed in with the browser instead of an API key. */
export interface Grant {
  id: string;
  client_name: string;
  user_id: string;
  user_email: string;
  created_at: string;
  last_used_at: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `The gateway answered with HTTP ${response.status}.`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") message = payload.detail;
      else if (Array.isArray(payload.detail)) message = payload.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      // not JSON; keep the generic message
    }
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  config: () => request<Config>("GET", "/api/config"),
  me: () => request<User>("GET", "/api/auth/me"),
  login: (email: string, password: string) => request<User>("POST", "/api/auth/login", { email, password }),
  logout: () => request<void>("POST", "/api/auth/logout"),

  invite: (token: string) => request<{ email: string; role: Role }>("GET", `/api/invites/${token}`),
  acceptInvite: (token: string, name: string, password: string) =>
    request<User>("POST", `/api/invites/${token}/accept`, { name, password }),

  servers: () => request<Server[]>("GET", "/api/servers"),
  addServer: (input: ServerInput) => request<Server>("POST", "/api/servers", input),
  changeServer: (id: string, input: ServerInput) => request<Server>("PATCH", `/api/servers/${id}`, input),
  removeServer: (id: string) => request<void>("DELETE", `/api/servers/${id}`),
  refreshServer: (id: string) => request<Server>("POST", `/api/servers/${id}/refresh`),
  redeployServer: (id: string) => request<Server>("POST", `/api/servers/${id}/redeploy`),

  keys: (everyone = false) => request<ApiKey[]>("GET", `/api/keys${everyone ? "?everyone=true" : ""}`),
  createKey: (name: string, userId?: string) => request<ApiKey>("POST", "/api/keys", { name, user_id: userId }),
  revokeKey: (id: string) => request<void>("DELETE", `/api/keys/${id}`),

  members: () => request<{ members: User[]; invites: Invite[] }>("GET", "/api/members"),
  createInvite: (email: string, role: Role) => request<Invite>("POST", "/api/invites", { email, role }),
  cancelInvite: (id: string) => request<void>("DELETE", `/api/invites/${id}`),
  changeMember: (id: string, change: { role?: Role; active?: boolean }) =>
    request<User>("PATCH", `/api/members/${id}`, change),
  removeMember: (id: string) => request<void>("DELETE", `/api/members/${id}`),

  audit: (filters: { user_id?: string; server?: string; outcome?: string; limit?: number }) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, String(value));
    return request<AuditEntry[]>("GET", `/api/audit?${query}`);
  },
  consentRequest: (id: string) => request<{ client_name: string; returns_to: string }>("GET", `/api/oauth/requests/${id}`),
  answerConsent: (id: string, answer: "approve" | "deny") =>
    request<{ redirect: string }>("POST", `/api/oauth/requests/${id}/${answer}`),
  grants: () => request<Grant[]>("GET", "/api/oauth/grants"),
  revokeGrant: (id: string) => request<void>("DELETE", `/api/oauth/grants/${id}`),

  stores: () => request<Store[]>("GET", "/api/stores"),
  createStore: (name: string, slug: string) => request<Store>("POST", "/api/stores", { name, slug }),
  retryStore: (id: string) => request<Store>("POST", `/api/stores/${id}/retry`),
  deleteStore: (id: string) => request<Store>("DELETE", `/api/stores/${id}`),

  stats: (hours = 24) => request<ServerStats[]>("GET", `/api/stats?hours=${hours}`),
};

// --- chat --------------------------------------------------------------------

export type ChatEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; id: string; name: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ok: boolean; duration_ms: number; preview: string }
  | { type: "error"; message: string }
  | { type: "done" };

/**
 * Sends the conversation and calls `onEvent` for each event as it arrives.
 *
 * The reply is a server-sent event stream: lines of "data: <json>" separated by
 * blank lines. It is read with fetch rather than EventSource because
 * EventSource can only make GET requests.
 */
export async function streamChat(
  messages: { role: "user" | "assistant"; content: string }[],
  onEvent: (event: ChatEvent) => void,
): Promise<void> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!response.ok || !response.body) {
    let message = `The gateway answered with HTTP ${response.status}.`;
    try {
      message = (await response.json()).detail ?? message;
    } catch {
      // keep the generic message
    }
    throw new ApiError(response.status, message);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);
      if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as ChatEvent);
    }
  }
}
