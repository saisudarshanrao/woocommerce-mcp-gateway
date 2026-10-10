import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { api, type ApiKey, type Invite, type Role, type User } from "../api";
import { ago, Badge, Button, Card, CodeBlock, ErrorNote, inputClass, PageHeader } from "../components/ui";
import { NewKey } from "./Keys";

export function TeamPage({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["members"] });
    queryClient.invalidateQueries({ queryKey: ["all-keys"] });
  };
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [invited, setInvited] = useState<Invite | null>(null);
  const [issued, setIssued] = useState<ApiKey | null>(null);

  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const team = useQuery({ queryKey: ["members"], queryFn: api.members });
  const keys = useQuery({ queryKey: ["all-keys"], queryFn: () => api.keys(true) });

  const invite = useMutation({
    mutationFn: () => api.createInvite(email, role),
    onSuccess: (created) => {
      setInvited(created);
      setEmail("");
      refresh();
    },
  });
  const cancelInvite = useMutation({ mutationFn: api.cancelInvite, onSuccess: refresh });
  const change = useMutation({
    mutationFn: ({ id, ...body }: { id: string; role?: Role; active?: boolean }) => api.changeMember(id, body),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: api.removeMember, onSuccess: refresh });
  const issue = useMutation({
    mutationFn: (member: User) => api.createKey(`Issued by ${user.name}`, member.id),
    onSuccess: (key) => {
      setIssued(key);
      refresh();
    },
  });
  const revoke = useMutation({ mutationFn: api.revokeKey, onSuccess: refresh });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    invite.mutate();
  };

  return (
    <>
      <PageHeader title="Team" description="Who can use the gateway, what they may do, and the keys they hold." />

      <Card className="mb-6 p-5">
        <form onSubmit={submit} className="flex items-end gap-3">
          <label className="flex-1">
            <span className="mb-1 block text-sm font-medium text-slate-700">Invite someone</span>
            <input className={inputClass} type="email" required placeholder="colleague@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label>
            <span className="mb-1 block text-sm font-medium text-slate-700">Role</span>
            <select className={inputClass} value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <Button variant="primary" type="submit" disabled={invite.isPending}>
            Create invitation
          </Button>
        </form>
        <div className="mt-3">
          <ErrorNote error={invite.error} />
        </div>
        {invited?.link && (
          <div className="mt-4">
            <p className="mb-2 text-sm text-slate-600">
              Send this link to <strong>{invited.email}</strong>. It works once, expires {ago(invited.expires_at).replace(" ago", "")} from now at the latest, and cannot be shown again.
            </p>
            <CodeBlock code={invited.link} />
          </div>
        )}
      </Card>

      {issued?.key && (
        <NewKey apiKey={issued.key} mcpUrl={config.data?.mcp_url ?? ""} owner={issued.user_email} onDismiss={() => setIssued(null)} />
      )}

      <h2 className="mb-2 text-sm font-semibold text-slate-700">Members</h2>
      <Card className="mb-6">
        <table className="w-full text-left text-sm">
          <tbody className="divide-y divide-slate-100">
            {team.data?.members.map((member) => (
              <tr key={member.id}>
                <td className="px-5 py-3">
                  <div className="font-medium">{member.name}</div>
                  <div className="text-xs text-slate-500">{member.email}</div>
                </td>
                <td className="px-5 py-3">
                  <Badge tone={member.role === "admin" ? "blue" : "neutral"}>{member.role}</Badge>{" "}
                  {!member.active && <Badge tone="red">deactivated</Badge>}
                </td>
                <td className="px-5 py-3">
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button size="sm" disabled={issue.isPending || !member.active} onClick={() => issue.mutate(member)}>
                      Issue key
                    </Button>
                    <Button size="sm" disabled={change.isPending} onClick={() => change.mutate({ id: member.id, role: member.role === "admin" ? "member" : "admin" })}>
                      Make {member.role === "admin" ? "member" : "admin"}
                    </Button>
                    <Button size="sm" disabled={change.isPending} onClick={() => change.mutate({ id: member.id, active: !member.active })}>
                      {member.active ? "Deactivate" : "Reactivate"}
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={remove.isPending || member.id === user.id}
                      onClick={() => window.confirm(`Remove ${member.email}? Their keys stop working at once.`) && remove.mutate(member.id)}
                    >
                      Remove
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <ErrorNote error={team.error ?? change.error ?? remove.error ?? issue.error} />

      {team.data && team.data.invites.length > 0 && (
        <>
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Invitations not yet accepted</h2>
          <Card className="mb-6">
            <table className="w-full text-left text-sm">
              <tbody className="divide-y divide-slate-100">
                {team.data.invites.map((pending) => (
                  <tr key={pending.id}>
                    <td className="px-5 py-3">{pending.email}</td>
                    <td className="px-5 py-3">
                      <Badge>{pending.role}</Badge>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <Button size="sm" variant="danger" disabled={cancelInvite.isPending} onClick={() => cancelInvite.mutate(pending.id)}>
                        Cancel
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </>
      )}

      <h2 className="mb-2 text-sm font-semibold text-slate-700">Everyone's API keys</h2>
      <Card>
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">Owner</th>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="px-5 py-3 font-medium">Key</th>
              <th className="px-5 py-3 font-medium">Last used</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {keys.data?.map((key) => (
              <tr key={key.id} className={key.revoked_at ? "text-slate-400" : ""}>
                <td className="px-5 py-3">{key.user_email}</td>
                <td className="px-5 py-3">{key.name}</td>
                <td className="px-5 py-3">
                  <code className="text-xs">{key.prefix}…</code>
                </td>
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
                  Nobody has created a key yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
      <ErrorNote error={keys.error ?? revoke.error ?? cancelInvite.error} />
    </>
  );
}
