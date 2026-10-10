import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { Button, Card, ErrorNote, Field, inputClass } from "../components/ui";

export function AcceptInvitePage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");

  const invite = useQuery({ queryKey: ["invite", token], queryFn: () => api.invite(token) });
  const accept = useMutation({
    mutationFn: () => api.acceptInvite(token, name, password),
    onSuccess: (user) => {
      queryClient.setQueryData(["me"], user);
      navigate("/keys", { replace: true });
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    accept.mutate();
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold">Join MCP Gateway</h1>
        {invite.isPending && <p className="mt-3 text-sm text-slate-500">Checking the invitation…</p>}
        {invite.error && (
          <div className="mt-4">
            <ErrorNote error={invite.error} />
          </div>
        )}
        {invite.data && (
          <>
            <p className="mb-5 mt-1 text-sm text-slate-600">
              You are joining as <strong>{invite.data.email}</strong>, with the role <strong>{invite.data.role}</strong>.
            </p>
            <form onSubmit={submit} className="space-y-4">
              <Field label="Your name">
                <input className={inputClass} required autoFocus value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label="Choose a password" hint="At least 10 characters.">
                <input className={inputClass} type="password" autoComplete="new-password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} />
              </Field>
              <ErrorNote error={accept.error} />
              <Button variant="primary" type="submit" className="w-full" disabled={accept.isPending}>
                {accept.isPending ? "Creating your account…" : "Create account"}
              </Button>
            </form>
          </>
        )}
      </Card>
    </div>
  );
}
