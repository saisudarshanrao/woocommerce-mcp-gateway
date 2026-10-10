import { useQuery } from "@tanstack/react-query";
import { Check, Loader2, Send, Wrench, X } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { api, type ChatEvent, streamChat } from "../api";
import { Button, Card, inputClass, PageHeader } from "../components/ui";

interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  // Filled in when the result arrives.
  ok?: boolean;
  duration_ms?: number;
  preview?: string;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolCall[];
  error?: string;
}

const SUGGESTIONS = ["What orders came in today?", "List the products in the store", "Create a product called Demo Mug priced at 12.50"];

export function ChatPage() {
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [turns]);

  const send = async (question: string) => {
    if (!question.trim() || busy) return;
    const history: Turn[] = [...turns, { role: "user", text: question.trim(), tools: [] }];
    setTurns([...history, { role: "assistant", text: "", tools: [] }]);
    setDraft("");
    setBusy(true);

    // Apply one event to the reply being built, which is always the last turn.
    const apply = (event: ChatEvent) =>
      setTurns((current) => {
        const reply = { ...current[current.length - 1] };
        if (event.type === "text") reply.text = reply.text ? `${reply.text}\n\n${event.text}` : event.text;
        if (event.type === "tool_call") reply.tools = [...reply.tools, { id: event.id, name: event.name, arguments: event.arguments }];
        if (event.type === "tool_result") reply.tools = reply.tools.map((tool) => (tool.id === event.id ? { ...tool, ...event } : tool));
        if (event.type === "error") reply.error = event.message;
        return [...current.slice(0, -1), reply];
      });

    try {
      // The whole conversation is sent each time: the gateway keeps no chat state.
      await streamChat(
        history.filter((turn) => turn.text).map((turn) => ({ role: turn.role, content: turn.text })),
        apply,
      );
    } catch (error) {
      apply({ type: "error", message: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send(draft);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send(draft);
    }
  };

  if (config.data && !config.data.chat_enabled) {
    return (
      <>
        <PageHeader title="Chat" description="Ask questions that are answered with the gateway's tools." />
        <Card className="p-6 text-sm text-slate-600">
          The chat has no model configured yet. An admin needs to store the model's API key in the cluster, with{" "}
          <code className="rounded bg-slate-100 px-1 py-0.5">make llm-key</code>. Everything else works without it.
        </Card>
      </>
    );
  }

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      <PageHeader
        title="Chat"
        description={`Answers come from the gateway's tools, called as you. Every call is in the audit log.${config.data ? ` Model: ${config.data.chat_model}.` : ""}`}
      />
      <div className="flex-1 space-y-4 overflow-y-auto pb-4">
        {turns.length === 0 && (
          <Card className="p-6">
            <p className="mb-3 text-sm text-slate-600">Try one of these:</p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <Button key={suggestion} size="sm" onClick={() => void send(suggestion)}>
                  {suggestion}
                </Button>
              ))}
            </div>
          </Card>
        )}
        {turns.map((turn, index) =>
          turn.role === "user" ? (
            <div key={index} className="flex justify-end">
              <div className="max-w-2xl whitespace-pre-wrap rounded-lg bg-indigo-600 px-4 py-2.5 text-sm text-white">{turn.text}</div>
            </div>
          ) : (
            <div key={index} className="max-w-3xl space-y-2">
              {turn.tools.map((tool) => (
                <ToolChip key={tool.id} tool={tool} />
              ))}
              {turn.text && <Card className="whitespace-pre-wrap px-4 py-3 text-sm leading-relaxed">{turn.text}</Card>}
              {turn.error && <p className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 ring-1 ring-inset ring-rose-200">{turn.error}</p>}
              {busy && index === turns.length - 1 && !turn.error && (
                <p className="flex items-center gap-2 text-xs text-slate-500">
                  <Loader2 size={13} className="animate-spin" /> Working…
                </p>
              )}
            </div>
          ),
        )}
        <div ref={bottom} />
      </div>
      <form onSubmit={submit} className="flex items-end gap-2 border-t border-slate-200 pt-4">
        <textarea
          className={`${inputClass} max-h-40 min-h-11 resize-none`}
          rows={1}
          placeholder="Ask about orders, products, tasks…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          aria-label="Message"
        />
        <Button variant="primary" type="submit" disabled={busy || !draft.trim()} aria-label="Send">
          <Send size={16} />
        </Button>
      </form>
    </div>
  );
}

/** One tool call, as it happens: the name and arguments at once, the outcome when it returns. */
function ToolChip({ tool }: { tool: ToolCall }) {
  const pending = tool.ok === undefined;
  const args = Object.keys(tool.arguments).length ? JSON.stringify(tool.arguments) : "";
  return (
    <details className="rounded-md bg-white text-xs ring-1 ring-slate-200">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2">
        {pending ? (
          <Loader2 size={13} className="animate-spin text-slate-400" />
        ) : tool.ok ? (
          <Check size={13} className="text-emerald-600" />
        ) : (
          <X size={13} className="text-rose-600" />
        )}
        <Wrench size={13} className="text-slate-400" />
        <code className="font-medium text-slate-800">{tool.name}</code>
        <code className="truncate text-slate-500">{args}</code>
        {!pending && <span className="ml-auto shrink-0 tabular-nums text-slate-400">{tool.duration_ms} ms</span>}
      </summary>
      {tool.preview && <pre className="overflow-x-auto border-t border-slate-100 px-3 py-2 text-slate-600">{tool.preview}</pre>}
    </details>
  );
}
