-- Stores created from the dashboard. A row is the request: the API writes it,
-- and the provisioner, a separate component with access to the Kubernetes API,
-- makes the cluster match it and reports back here.
--
-- The slug names everything the store owns: the namespace store-<slug>, the
-- hostname <slug>.<domain>, and the gateway server store-<slug>, which is the
-- prefix of the store's tools.
CREATE TABLE stores (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z]([a-z0-9-]{0,16}[a-z0-9])?$'),
    name        text NOT NULL,
    owner_id    uuid REFERENCES users (id) ON DELETE SET NULL,
    owner_email text NOT NULL,
    --   provisioning  being created (also: being retried)
    --   ready         installed, and registered in the gateway
    --   failed        gave up; `detail` says why. Can be retried or deleted.
    --   deleting      being removed. The row goes when the namespace is gone.
    status      text NOT NULL DEFAULT 'provisioning'
                CHECK (status IN ('provisioning', 'ready', 'failed', 'deleting')),
    -- What the provisioner is doing right now or, for a failed store, why it failed.
    detail      text,
    url         text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- When the current attempt began. The provisioning timeout counts from here,
    -- so it survives a restart of the provisioner.
    started_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- A store's MCP server is registered for it, and goes when the store goes.
ALTER TABLE upstream_servers ADD COLUMN store_id uuid UNIQUE REFERENCES stores (id) ON DELETE CASCADE;
