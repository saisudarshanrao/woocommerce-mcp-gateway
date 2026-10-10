import { useMutation, useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, type User } from "../api";
import { Button, Card, ErrorNote } from "../components/ui";

/**
 * Where an MCP client sends the browser when it wants to sign in.
 *
 * Reaching this page already required a dashboard session, so the gateway
 * knows who is answering. Allow hands the client a one-time code by sending
 * the browser back to it; the client never sees the password or the session.
 */
export function ConsentPage({ user }: { user: User }) {
  const [search] = useSearchParams();
  const id = search.get("request") ?? "";
  const pending = useQuery({ queryKey: ["consent", id], queryFn: () => api.consentRequest(id), retry: false });
  const answer = useMutation({
    mutationFn: (choice: "approve" | "deny") => api.answerConsent(id, choice),
    // Back to the client, which is waiting on a local address for exactly this.
    onSuccess: ({ redirect }) => window.location.assign(redirect),
  });

  return (
    <div className="mx-auto mt-10 max-w-md">
      <Card className="p-6">
        <div className="mb-3 flex items-center gap-2 text-indigo-700">
          <ShieldCheck size={20} />
          <h1 className="text-lg font-semibold text-slate-900">Allow access?</h1>
        </div>
        {pending.isPending && <p className="text-sm text-slate-500">Loading…</p>}
        <ErrorNote error={pending.error ?? answer.error} />
        {pending.data && (
          <>
            <p className="text-sm text-slate-700">
              <strong>{pending.data.client_name}</strong> wants to use the gateway&apos;s tools as{" "}
              <strong>{user.email}</strong>.
            </p>
            <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-slate-600">
              <li>It can call every tool your role allows, and its calls appear in the audit log under your name.</li>
              <li>It never sees your password, and you can sign it out again on the API keys page.</li>
              <li>
                You will be sent back to <code className="text-xs">{pending.data.returns_to}</code>. If you did not just start this from an
                MCP client on this computer, choose Deny.
              </li>
            </ul>
            <div className="mt-5 flex justify-end gap-2">
              <Button onClick={() => answer.mutate("deny")} disabled={answer.isPending}>
                Deny
              </Button>
              <Button variant="primary" onClick={() => answer.mutate("approve")} disabled={answer.isPending}>
                Allow
              </Button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
