import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { api, type ApiKey } from "../api";
import { ago, Badge, Button, Card, CodeBlock, ErrorNote, inputClass, PageHeader } from "../components/ui";

export function KeysPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ApiKey | null>(null);

  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const keys = useQuery({ queryKey: ["keys"], queryFn: () => api.keys() });
  const create = useMutation({
    mutationFn: () => api.createKey(name),
    onSuccess: (key) => {
      setCreated(key);
      setName("");
      queryClient.invalidateQueries({ queryKey: ["keys"] });
    },
  });
  const revoke = useMutation({
    mutationFn: api.revokeKey,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["keys"] }),
  });
  // Clients that signed in with the browser instead of a key.
  const grants = useQuery({ queryKey: ["grants"], queryFn: api.grants });
  const signOut = useMutation({
    mutationFn: api.revokeGrant,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["grants"] }),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };
  const mcpUrl = config.data?.mcp_url ?? "";

  return (
    <>
      <PageHeader
        title="API keys"
        description="A key lets an MCP client such as Claude Code or Cursor use every enabled server through the gateway, as you."
      />

      <Card className="mb-6 p-5">
        <form onSubmit={submit} className="flex items-end gap-3">
          <label className="flex-1">
            <span className="mb-1 block text-sm font-medium text-slate-700">New key</span>
            <input className={inputClass} required maxLength={60} placeholder="What is it for? e.g. Claude Code on my laptop" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <Button variant="primary" type="submit" disabled={create.isPending}>
            Create key
          </Button>
        </form>
        <div className="mt-3">
          <ErrorNote error={create.error} />
        </div>
      </Card>

      {created?.key && <NewKey apiKey={created.key} mcpUrl={mcpUrl} onDismiss={() => setCreated(null)} />}

      {config.data?.oauth_enabled && (
        <Card className="mb-6 p-5">
          <h2 className="text-sm font-semibold text-slate-900">Or sign in from the client, without a key</h2>
          <p className="mt-1 text-sm text-slate-600">
            Give an MCP client only the address <code className="text-xs">{mcpUrl}</code>. It opens this dashboard in your browser, you click
            Allow, and it is connected as you. In Claude Code: <code className="text-xs">claude mcp add --transport http gateway {mcpUrl}</code>
          </p>
          {grants.data && grants.data.length > 0 && (
            <ul className="mt-3 divide-y divide-slate-100 border-t border-slate-100 text-sm">
              {grants.data.map((grant) => (
                <li key={grant.id} className="flex items-center justify-between gap-3 py-2">
                  <span>
                    <span className="font-medium">{grant.client_name}</span>{" "}
                    <span className="text-slate-500">
                      signed in {ago(grant.created_at)}, last used {ago(grant.last_used_at)}
                    </span>
                  </span>
                  <Button size="sm" variant="danger" onClick={() => signOut.mutate(grant.id)} disabled={signOut.isPending}>
                    Sign out
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <Card>
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="px-5 py-3 font-medium">Key</th>
              <th className="px-5 py-3 font-medium">Created</th>
              <th className="px-5 py-3 font-medium">Last used</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {keys.data?.map((key) => (
              <tr key={key.id} className={key.revoked_at ? "text-slate-400" : ""}>
                <td className="px-5 py-3">{key.name}</td>
                <td className="px-5 py-3">
                  <code className="text-xs">{key.prefix}…</code>
                </td>
                <td className="px-5 py-3">{ago(key.created_at)}</td>
                <td className="px-5 py-3">{ago(key.last_used_at)}</td>
                <td className="px-5 py-3 text-right">
                  {key.revoked_at ? (
                    <Badge>revoked</Badge>
                  ) : (
                    <Button size="sm" variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(key.id)}>
                      Revoke
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {keys.data?.length === 0 && (
              <tr>
                <td colSpan={5} className="px-5 py-8 text-center text-slate-500">
                  You have no keys yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
      <ErrorNote error={keys.error ?? revoke.error} />
    </>
  );
}

/** Shown once, straight after a key is created. The gateway keeps only a hash of it. */
export function NewKey({ apiKey, mcpUrl, onDismiss, owner }: { apiKey: string; mcpUrl: string; onDismiss: () => void; owner?: string }) {
  const cursor = JSON.stringify({ mcpServers: { gateway: { url: mcpUrl, headers: { Authorization: `Bearer ${apiKey}` } } } }, null, 2);
  return (
    <Card className="mb-6 border-l-4 border-emerald-500 p-5">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold">{owner ? `Key for ${owner}` : "Your new key"}</h2>
          <p className="text-sm text-slate-600">Copy it now. The gateway stores only a hash of it, so it cannot be shown again.</p>
        </div>
        <Button size="sm" onClick={onDismiss}>
          Done
        </Button>
      </div>
      <CodeBlock code={apiKey} />
      <h3 className="mb-1.5 mt-5 text-sm font-medium text-slate-700">Claude Code</h3>
      <CodeBlock code={`claude mcp add --transport http gateway ${mcpUrl} \\\n  --header "Authorization: Bearer ${apiKey}"`} />
      <h3 className="mb-1.5 mt-4 text-sm font-medium text-slate-700">Cursor, and other clients configured with JSON</h3>
      <CodeBlock code={cursor} />
      <h3 className="mb-1.5 mt-4 text-sm font-medium text-slate-700">MCP Inspector</h3>
      <CodeBlock code={`npx @modelcontextprotocol/inspector --cli ${mcpUrl} \\\n  --header "Authorization: Bearer ${apiKey}" --method tools/list`} />
      <p className="mt-4 text-xs text-slate-500">
        With many servers the tool list gets long. Add <code>/search</code> to the URL ({mcpUrl}/search) and the client is given two tools
        instead, one to find a tool and one to call it, with the same key and the same permissions.
      </p>
    </Card>
  );
}
