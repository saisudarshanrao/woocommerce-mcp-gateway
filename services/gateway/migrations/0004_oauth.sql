-- Signing in to the gateway from an MCP client with a browser, instead of
-- pasting an API key (OAuth 2.1, as the MCP authorization specification asks).

-- Clients register themselves the first time they connect. A registration is
-- only a name and the addresses the browser may be sent back to.
CREATE TABLE oauth_clients (
    client_id  text PRIMARY KEY,
    name       text NOT NULL,
    info       text NOT NULL,  -- the registration as JSON
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A sign-in in progress: what the client asked for, kept for the few minutes
-- the person takes to sign in to the dashboard and say yes.
CREATE TABLE oauth_requests (
    id         text PRIMARY KEY,
    client_id  text NOT NULL REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
    params     text NOT NULL,  -- JSON: redirect address, code challenge, state
    expires_at timestamptz NOT NULL
);

-- The one-time code handed to the client through the browser after a yes. It
-- is exchanged for tokens within a minute or so. Only its hash is stored.
CREATE TABLE oauth_codes (
    code_hash  text PRIMARY KEY,
    client_id  text NOT NULL REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    params     text NOT NULL,
    expires_at timestamptz NOT NULL
);

-- A grant: one client allowed to act as one user. It holds the hashes of a
-- short-lived access token, sent with every MCP request, and of a refresh
-- token, used only to get the next access token. Deleting the row revokes both.
CREATE TABLE oauth_grants (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id          text NOT NULL REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
    user_id            uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    access_hash        text NOT NULL UNIQUE,
    access_expires_at  timestamptz NOT NULL,
    refresh_hash       text NOT NULL UNIQUE,
    refresh_expires_at timestamptz NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_used_at       timestamptz
);
CREATE INDEX oauth_grants_user_idx ON oauth_grants (user_id);
