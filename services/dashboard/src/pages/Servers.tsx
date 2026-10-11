import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, Loader2, Plus, RefreshCw, RotateCw } from "lucide-react";
import { type FormEvent, useState } from "react";

import { api, type AuthType, type Deployment, type MemberAccess, type Server, type ServerInput, type ServerStatus, type User } from "../api";
import { ago, Badge, Button, Card, ErrorNote, Field, inputClass, Modal, PageHeader } from "../components/ui";

const STATUS: Record<ServerStatus, { label: string; tone: "green" | "red" | "neutral" | "amber" }> = {
  connected: { label: "Connected", tone: "green" },
  unreachable: { label: "Unreachable", tone: "red" },
  disabled: { label: "Disabled", tone: "neutral" },
  pending: { label: "Checking…", tone: "amber" },
};

const ACCESS_LABEL: Record<MemberAccess, string> = {
  all: "Members can use every tool",
  read_only: "Members can use read-only tools",
  none: "Admins only",
};

export function ServersPage({ user }: { user: User }) {
  const isAdmin = user.role === "admin";
  const [editing, setEditing] = useState<Server | "new" | null>(null);
  // Polled, so a server that dies or comes back shows up without a reload.
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: api.servers,
    // Faster while a hosted server is being deployed or removed.
    refetchInterval: (query) =>
      query.state.data?.some((server) => server.hosted?.status === "deploying" || server.hosted?.status === "deleting") ? 1500 : 4000,
  });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });

  return (
    <>
      <PageHeader
        title="Servers"
        description="The MCP servers behind the gateway. Their tools are what the team's MCP clients and the chat can use."
        action={
          isAdmin && (
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus size={16} /> Add server
            </Button>
          )
        }
      />
      <ErrorNote error={servers.error} />
      <div className="space-y-4">
        {servers.data?.map((server) => (
          <ServerCard key={server.id} server={server} isAdmin={isAdmin} onEdit={() => setEditing(server)} />
        ))}
        {servers.data?.length === 0 && (
          <Card className="p-8 text-center text-sm text-slate-500">No servers are registered yet.</Card>
        )}
      </div>
      {editing && (
        <ServerForm
          server={editing === "new" ? null : editing}
          canHost={config.data?.hosted_enabled ?? false}
          imagePrefixes={config.data?.hosted_image_prefixes ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}

function ServerCard({ server, isAdmin, onEdit }: { server: Server; isAdmin: boolean; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const refreshList = () => queryClient.invalidateQueries({ queryKey: ["servers"] });
  const toggle = useMutation({ mutationFn: () => api.changeServer(server.id, { enabled: !server.enabled }), onSuccess: refreshList });
  const refresh = useMutation({ mutationFn: () => api.refreshServer(server.id), onSuccess: refreshList });
  const remove = useMutation({ mutationFn: () => api.removeServer(server.id), onSuccess: refreshList });
  const redeploy = useMutation({ mutationFn: () => api.redeployServer(server.id), onSuccess: refreshList });
  // While a hosted server is being deployed or removed, or has failed, that is
  // what matters, not whether the gateway can reach it yet.
  const hosted = server.hosted;
  const lifecycle =
    hosted?.status === "deploying"
      ? { label: "Deploying", tone: "amber" as const }
      : hosted?.status === "deleting"
        ? { label: "Removing", tone: "neutral" as const }
        : hosted?.status === "failed"
          ? { label: "Deployment failed", tone: "red" as const }
          : null;
  const status = lifecycle ?? STATUS[server.status];

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{server.name}</h2>
            <Badge tone={status.tone}>
              {(hosted?.status === "deploying" || hosted?.status === "deleting") && <Loader2 size={12} className="animate-spin" />}
              {status.label}
            </Badge>
            <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{server.slug}__</code>
            {server.managed && <Badge tone="blue">store</Badge>}
            {hosted && <Badge tone="blue">hosted</Badge>}
          </div>
          {lifecycle && hosted?.detail && (
            <p className={`mt-1.5 text-sm ${hosted.status === "failed" ? "text-rose-700" : "text-slate-600"}`}>{hosted.detail}</p>
          )}
          {!lifecycle && server.status === "unreachable" && (
            <p className="mt-1.5 flex items-center gap-1.5 text-sm text-rose-700">
              <AlertTriangle size={14} /> The gateway cannot reach it: {server.error}.
            </p>
          )}
          <p className="mt-1.5 text-xs text-slate-500">
            {server.status === "connected" && (
              <>
                {server.tools.length} tools · protocol {server.protocol_version} · answered in {server.latency_ms} ms ·{" "}
              </>
            )}
            {server.status !== "disabled" && <>checked {ago(server.checked_at)} · </>}
            {ACCESS_LABEL[server.member_access]}
          </p>
          {isAdmin && hosted?.image && (
            <p className="mt-1 truncate text-xs text-slate-500">
              runs <code>{[hosted.image, ...(hosted.args ?? [])].join(" ")}</code> in namespace {hosted.namespace} ·{" "}
              {hosted.internet ? "may reach the internet" : "no outbound network"}
            </p>
          )}
          {isAdmin && server.url && (
            <p className="mt-1 truncate text-xs text-slate-500">
              {server.url} ·{" "}
              {server.auth?.type === "none"
                ? "no credential"
                : server.auth?.type === "bearer"
                  ? "bearer token set"
                  : `credential set in header ${server.auth?.header}`}{" "}
              · times out after {server.timeout_seconds}s
            </p>
          )}
        </div>
        {isAdmin && (
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button size="sm" onClick={() => refresh.mutate()} disabled={refresh.isPending || !server.enabled} title="Check it now">
              <RefreshCw size={13} className={refresh.isPending ? "animate-spin" : ""} /> Check
            </Button>
            <Button size="sm" onClick={() => toggle.mutate()} disabled={toggle.isPending}>
              {server.enabled ? "Disable" : "Enable"}
            </Button>
            <Button size="sm" onClick={onEdit}>
              Edit
            </Button>
            {hosted && hosted.status !== "deleting" && hosted.status !== "deploying" && (
              <Button size="sm" onClick={() => redeploy.mutate()} disabled={redeploy.isPending} title="Deploy it again as it is">
                <RotateCw size={13} /> {hosted.status === "failed" ? "Try again" : "Restart"}
              </Button>
            )}
            {/* A store's server is removed by deleting the store, on the Stores page. */}
            {!server.managed && hosted?.status !== "deleting" && (
              <Button
                size="sm"
                variant="danger"
                disabled={remove.isPending}
                onClick={() => window.confirm(`Remove ${server.name}? Its tools will disappear for everyone.`) && remove.mutate()}
              >
                Remove
              </Button>
            )}
          </div>
        )}
      </div>
      <ErrorNote error={toggle.error ?? refresh.error ?? remove.error ?? redeploy.error} />

      {server.skipped_tools.length > 0 && (
        <p className="border-t border-amber-200 bg-amber-50 px-5 py-2 text-xs text-amber-800">
          Not exposed: {server.skipped_tools.map((tool) => `${tool.name} (${tool.reason})`).join("; ")}
        </p>
      )}

      {server.tools.length > 0 && (
        <details className="group border-t border-slate-200">
          <summary className="flex cursor-pointer list-none items-center gap-1 px-5 py-2.5 text-sm text-slate-600 hover:bg-slate-50">
            <ChevronDown size={15} className="transition group-open:rotate-180" />
            {server.tools.length} tools
          </summary>
          <ul className="divide-y divide-slate-100 border-t border-slate-100">
            {server.tools.map((tool) => (
              <li key={tool.name} className="px-5 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="text-sm font-medium text-slate-900">{tool.name}</code>
                  {tool.read_only ? <Badge tone="blue">read-only</Badge> : <Badge tone="amber">changes data</Badge>}
                </div>
                <p className="mt-0.5 text-xs text-slate-600">{tool.description}</p>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}

/** "KEY=value" lines to a name: value map, skipping blank lines. */
function parseEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf("=");
    if (at > 0) env[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return env;
}

function ServerForm({
  server,
  canHost,
  imagePrefixes,
  onClose,
}: {
  server: Server | null;
  canHost: boolean;
  imagePrefixes: string[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  // A new server is either somewhere already (a URL) or something to run (an
  // image). An existing one stays what it is.
  const [byImage, setByImage] = useState(Boolean(server?.hosted));
  const [slug, setSlug] = useState("");
  const [name, setName] = useState(server?.name ?? "");
  const [url, setUrl] = useState(server?.url ?? "");
  const [authType, setAuthType] = useState<AuthType>(server?.auth?.type ?? "none");
  const [authHeader, setAuthHeader] = useState(server?.auth?.header ?? "X-API-Key");
  const [credential, setCredential] = useState("");
  const [timeout, setTimeoutSeconds] = useState(server?.timeout_seconds ?? 30);
  const [access, setAccess] = useState<MemberAccess>(server?.member_access ?? "all");
  const [image, setImage] = useState(server?.hosted?.image ?? "");
  const [args, setArgs] = useState((server?.hosted?.args ?? []).join(" "));
  const [env, setEnv] = useState(
    Object.entries(server?.hosted?.env ?? {})
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
  );
  const [port, setPort] = useState(server?.hosted?.port ?? 8080);
  const [path, setPath] = useState(server?.hosted?.path ?? "/mcp");
  const [internet, setInternet] = useState(server?.hosted?.internet ?? false);

  const save = useMutation({
    mutationFn: () => {
      const input: ServerInput = { name, timeout_seconds: timeout, member_access: access };
      if (byImage) {
        const deployment: Deployment = {
          image: image.trim(),
          args: args.split(/\s+/).filter(Boolean),
          env: parseEnv(env),
          port,
          path,
          health_path: server?.hosted?.health_path ?? "",
          internet,
        };
        input.hosted = deployment;
      } else {
        input.url = url;
        input.auth_type = authType;
        input.auth_header = authType === "header" ? authHeader : null;
        // An empty box when editing means "keep the credential that is stored".
        if (credential) input.credential = credential;
      }
      return server ? api.changeServer(server.id, input) : api.addServer({ ...input, slug });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["servers"] });
      onClose();
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate();
  };

  return (
    <Modal title={server ? `Edit ${server.name}` : "Add an MCP server"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {!server && (
          <Field label="Identifier" hint="Lower-case letters, digits and hyphens. It prefixes every tool name, as in woo__list_orders, and cannot be changed later.">
            <input className={inputClass} required autoFocus pattern="[a-z][a-z0-9\-]{0,23}" placeholder="tasks" value={slug} onChange={(e) => setSlug(e.target.value)} />
          </Field>
        )}
        <Field label="Name">
          <input className={inputClass} required placeholder="Team tasks" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        {!server && canHost && (
          <div className="grid grid-cols-2 gap-1 rounded-md bg-slate-100 p-1 text-sm">
            {[
              { label: "It is already running: connect by URL", value: false },
              { label: "Run it here from a container image", value: true },
            ].map((choice) => (
              <button
                key={choice.label}
                type="button"
                onClick={() => setByImage(choice.value)}
                className={`rounded px-2 py-1.5 ${byImage === choice.value ? "bg-white font-medium text-slate-900 shadow-sm" : "text-slate-600"}`}
              >
                {choice.label}
              </button>
            ))}
          </div>
        )}
        {byImage && (
          <>
            <Field
              label="Image"
              hint={
                imagePrefixes.length > 0
                  ? `Must start with ${imagePrefixes.join(" or ")}.`
                  : "It is run as a non-root user with a read-only filesystem and no Kubernetes access. The gateway makes up its credential and passes it in as AUTH_TOKEN."
              }
            >
              <input className={inputClass} required placeholder="ghcr.io/acme/notes-mcp:1.4" value={image} onChange={(e) => setImage(e.target.value)} />
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <div className="col-span-2">
                <Field label="Arguments" hint="Optional, separated by spaces.">
                  <input className={inputClass} placeholder="serve --verbose" value={args} onChange={(e) => setArgs(e.target.value)} />
                </Field>
              </div>
              <Field label="Port">
                <input className={inputClass} type="number" min={1} max={65535} required value={port} onChange={(e) => setPort(Number(e.target.value))} />
              </Field>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <Field label="MCP path">
                <input className={inputClass} required value={path} onChange={(e) => setPath(e.target.value)} />
              </Field>
              <div className="col-span-2">
                <Field label="Environment" hint="Optional. One NAME=value per line. Not for secrets: these are shown here.">
                  <textarea className={inputClass} rows={2} placeholder="LOG_LEVEL=info" value={env} onChange={(e) => setEnv(e.target.value)} />
                </Field>
              </div>
            </div>
            <label className="flex items-start gap-2 text-sm text-slate-700">
              <input type="checkbox" className="mt-0.5" checked={internet} onChange={(e) => setInternet(e.target.checked)} />
              <span>
                Let it connect to the internet
                <span className="block text-xs text-slate-500">Off, it can open no connection at all, which suits a server that only computes.</span>
              </span>
            </label>
          </>
        )}
        {!byImage && (
          <Field label="URL" hint="The server's Streamable HTTP endpoint.">
            <input className={inputClass} required type="url" placeholder="http://tasks-mcp.mcp-servers.svc.cluster.local/mcp" value={url} onChange={(e) => setUrl(e.target.value)} />
          </Field>
        )}
        <div className={byImage ? "hidden" : "grid grid-cols-2 gap-3"}>
          <Field label="Authentication">
            <select className={inputClass} value={authType} onChange={(e) => setAuthType(e.target.value as AuthType)}>
              <option value="none">None</option>
              <option value="bearer">Bearer token</option>
              <option value="header">Custom header</option>
            </select>
          </Field>
          {authType === "header" && (
            <Field label="Header name">
              <input className={inputClass} required value={authHeader} onChange={(e) => setAuthHeader(e.target.value)} />
            </Field>
          )}
        </div>
        {!byImage && authType !== "none" && (
          <Field
            label={authType === "bearer" ? "Token" : "Header value"}
            hint={
              server?.auth?.credential_set
                ? "A credential is stored. Leave this empty to keep it. It is never shown again."
                : "Stored encrypted. It is sent only to this server and never shown again."
            }
          >
            <input className={inputClass} type="password" autoComplete="off" required={!server?.auth?.credential_set} value={credential} onChange={(e) => setCredential(e.target.value)} />
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Call timeout (seconds)">
            <input className={inputClass} type="number" min={1} max={300} required value={timeout} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} />
          </Field>
          <Field label="Members may use">
            <select className={inputClass} value={access} onChange={(e) => setAccess(e.target.value as MemberAccess)}>
              <option value="all">Every tool</option>
              <option value="read_only">Read-only tools</option>
              <option value="none">Nothing (admins only)</option>
            </select>
          </Field>
        </div>
        <ErrorNote error={save.error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : server ? "Save" : "Add server"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
