import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type AuditEntry, type User } from "../api";
import { Badge, Card, clock, ErrorNote, inputClass, PageHeader } from "../components/ui";

const OUTCOME_TONE: Record<AuditEntry["outcome"], "green" | "red" | "amber" | "neutral"> = {
  ok: "green",
  error: "red",
  timeout: "amber",
  unreachable: "amber",
  denied: "neutral",
};

export function AuditPage({ user }: { user: User }) {
  const isAdmin = user.role === "admin";
  const [userId, setUserId] = useState("");
  const [server, setServer] = useState("");
  const [outcome, setOutcome] = useState("");

  const servers = useQuery({ queryKey: ["servers"], queryFn: api.servers });
  const members = useQuery({ queryKey: ["members"], queryFn: api.members, enabled: isAdmin });
  // Polled, so a call made in another window shows up here a few seconds later.
  const entries = useQuery({
    queryKey: ["audit", userId, server, outcome],
    queryFn: () => api.audit({ user_id: userId, server, outcome, limit: 200 }),
    refetchInterval: 4000,
  });

  return (
    <>
      <PageHeader
        title="Audit log"
        description={
          isAdmin
            ? "Every tool call through the gateway: who made it, which server and tool, when, how long it took and how it ended."
            : "Every tool call you have made through the gateway, from an MCP client or from the chat."
        }
      />
      <div className="mb-4 flex flex-wrap gap-3">
        {isAdmin && (
          <select className={`${inputClass} w-auto`} value={userId} onChange={(e) => setUserId(e.target.value)} aria-label="Filter by user">
            <option value="">Everyone</option>
            {members.data?.members.map((member) => (
              <option key={member.id} value={member.id}>
                {member.email}
              </option>
            ))}
          </select>
        )}
        <select className={`${inputClass} w-auto`} value={server} onChange={(e) => setServer(e.target.value)} aria-label="Filter by server">
          <option value="">Every server</option>
          {servers.data?.map((item) => (
            <option key={item.id} value={item.slug}>
              {item.name}
            </option>
          ))}
        </select>
        <select className={`${inputClass} w-auto`} value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Filter by outcome">
          <option value="">Every outcome</option>
          {Object.keys(OUTCOME_TONE).map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>
      <ErrorNote error={entries.error} />
      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3 font-medium">When</th>
              <th className="px-4 py-3 font-medium">Who</th>
              <th className="px-4 py-3 font-medium">Via</th>
              <th className="px-4 py-3 font-medium">Server</th>
              <th className="px-4 py-3 font-medium">Tool</th>
              <th className="px-4 py-3 text-right font-medium">Took</th>
              <th className="px-4 py-3 font-medium">Outcome</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {entries.data?.map((entry) => (
              <tr key={entry.id} className="align-top">
                <td className="whitespace-nowrap px-4 py-2.5 text-slate-600">{clock(entry.at)}</td>
                <td className="px-4 py-2.5">{entry.user_email}</td>
                <td className="whitespace-nowrap px-4 py-2.5 text-slate-600">
                  {entry.source === "chat" ? "chat" : <code className="text-xs">{entry.api_key_prefix}…</code>}
                </td>
                <td className="px-4 py-2.5">{entry.server ?? "–"}</td>
                <td className="px-4 py-2.5">
                  <code className="text-xs">{entry.tool ?? "–"}</code>
                  {entry.error && <div className="mt-0.5 max-w-md text-xs text-slate-500">{entry.error}</div>}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5 text-right tabular-nums text-slate-600">{entry.duration_ms} ms</td>
                <td className="px-4 py-2.5">
                  <Badge tone={OUTCOME_TONE[entry.outcome]}>{entry.outcome}</Badge>
                </td>
              </tr>
            ))}
            {entries.data?.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-slate-500">
                  No tool calls match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </>
  );
}
