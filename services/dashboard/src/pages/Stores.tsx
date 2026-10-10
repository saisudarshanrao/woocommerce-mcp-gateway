import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2, Plus, RotateCw, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";

import { api, type Store, type StoreStatus, type User } from "../api";
import { ago, Badge, Button, Card, ErrorNote, Field, inputClass, Modal, PageHeader } from "../components/ui";

const STATUS: Record<StoreStatus, { label: string; tone: "green" | "red" | "amber" | "neutral" }> = {
  provisioning: { label: "Provisioning", tone: "amber" },
  ready: { label: "Ready", tone: "green" },
  failed: { label: "Failed", tone: "red" },
  deleting: { label: "Deleting", tone: "neutral" },
};

/** "Corner Books!" -> "corner-books": a starting point the user can change. */
function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^[^a-z]+|-+$/g, "")
    .slice(0, 18)
    .replace(/-+$/, "");
}

export function StoresPage({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const stores = useQuery({
    queryKey: ["stores"],
    queryFn: api.stores,
    // Fast while something is being created or removed, slow otherwise.
    refetchInterval: (query) =>
      query.state.data?.some((store) => store.status === "provisioning" || store.status === "deleting") ? 1500 : 10000,
  });
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["stores"] });
    // A store's tools come and go with it.
    queryClient.invalidateQueries({ queryKey: ["servers"] });
  };
  const retry = useMutation({ mutationFn: api.retryStore, onSuccess: refresh });
  const remove = useMutation({ mutationFn: api.deleteStore, onSuccess: refresh });

  const limit = config.data?.max_stores_per_user ?? 0;
  const mine = stores.data?.filter((store) => store.owner_email === user.email).length ?? 0;

  return (
    <>
      <PageHeader
        title="Stores"
        description="Each store is its own WooCommerce site in its own Kubernetes namespace, with its own database. Its products and orders become tools in the gateway as soon as it is ready."
        action={
          <Button variant="primary" onClick={() => setCreating(true)} disabled={mine >= limit} title={mine >= limit ? `You have ${limit} stores, the most one person may have.` : undefined}>
            <Plus size={16} /> New store
          </Button>
        }
      />
      <ErrorNote error={stores.error ?? retry.error ?? remove.error} />
      <div className="space-y-3">
        {stores.data?.map((store) => (
          <StoreCard
            key={store.id}
            store={store}
            onRetry={() => retry.mutate(store.id)}
            onDelete={() => {
              if (window.confirm(`Delete ${store.name}? Its products, orders and database are removed for good.`)) remove.mutate(store.id);
            }}
          />
        ))}
        {stores.data?.length === 0 && (
          <Card className="px-5 py-10 text-center text-sm text-slate-500">
            No stores have been created here yet. The first store, installed with the platform, is the server called “woo”.
          </Card>
        )}
      </div>
      <p className="mt-4 text-xs text-slate-500">
        You have {mine} of the {limit} stores one person may have.
      </p>
      {creating && (
        <NewStore
          urlTemplate={config.data?.store_url_template ?? ""}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            refresh();
          }}
        />
      )}
    </>
  );
}

function StoreCard({ store, onRetry, onDelete }: { store: Store; onRetry: () => void; onDelete: () => void }) {
  const status = STATUS[store.status];
  const busy = store.status === "provisioning" || store.status === "deleting";
  return (
    <Card className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold text-slate-900">{store.name}</h2>
            <Badge tone={status.tone}>
              {busy && <Loader2 size={12} className="animate-spin" />}
              {status.label}
            </Badge>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-slate-600">
            {store.url && store.status === "ready" ? (
              <a href={store.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-indigo-700 hover:underline">
                {store.url} <ExternalLink size={12} />
              </a>
            ) : (
              <span>{store.namespace}</span>
            )}
            <span>
              tools: <code className="text-xs">{store.tool_prefix}…</code>
            </span>
            <span>
              created {ago(store.created_at)} by {store.owner_email}
            </span>
          </div>
          {store.detail && (
            <p className={`mt-2 text-sm ${store.status === "failed" ? "text-rose-700" : "text-slate-500"}`}>{store.detail}</p>
          )}
        </div>
        {store.can_manage && (
          <div className="flex shrink-0 gap-2">
            {store.status === "failed" && (
              <Button size="sm" onClick={onRetry}>
                <RotateCw size={14} /> Try again
              </Button>
            )}
            {store.status !== "deleting" && (
              <Button size="sm" variant="danger" onClick={onDelete}>
                <Trash2 size={14} /> Delete
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function NewStore({ urlTemplate, onClose, onCreated }: { urlTemplate: string; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  // Until the identifier is edited by hand it follows the name.
  const [slug, setSlug] = useState<string | null>(null);
  const identifier = slug ?? suggestSlug(name);
  const create = useMutation({ mutationFn: () => api.createStore(name, identifier), onSuccess: onCreated });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };

  return (
    <Modal title="New store" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name" hint="Shown as the title of the storefront.">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} placeholder="Corner Books" maxLength={60} required autoFocus />
        </Field>
        <Field label="Identifier" hint="Lower-case letters, digits and hyphens. It cannot be changed later.">
          <input
            className={inputClass}
            value={identifier}
            onChange={(e) => setSlug(e.target.value.toLowerCase())}
            placeholder="corner-books"
            pattern="[a-z]([a-z0-9\-]{0,16}[a-z0-9])?"
            maxLength={18}
            required
          />
        </Field>
        {identifier && (
          <dl className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600 ring-1 ring-inset ring-slate-200">
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-slate-500">Address</dt>
              <dd className="truncate">{urlTemplate.replace("{slug}", identifier)}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-slate-500">Namespace</dt>
              <dd>store-{identifier}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-slate-500">Tools</dt>
              <dd>
                <code className="text-xs">store-{identifier}__list_orders</code>, …
              </dd>
            </div>
          </dl>
        )}
        <ErrorNote error={create.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={create.isPending || !name.trim() || !identifier}>
            {create.isPending ? "Creating…" : "Create store"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
