-- People who can sign in. Email is stored lower-cased by the application.
CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    name          text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'member')),
    password_hash text NOT NULL,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Dashboard sessions. The browser holds a random token in a cookie; only its
-- SHA-256 is stored, so a copy of this table cannot be used to sign in.
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- An invitation is a one-time link. Accepting it creates the user.
CREATE TABLE invites (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash  text NOT NULL UNIQUE,
    email       text NOT NULL,
    role        text NOT NULL CHECK (role IN ('admin', 'member')),
    invited_by  uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    accepted_at timestamptz
);

-- Gateway API keys. The key itself is shown once and never stored: key_hash is
-- its SHA-256, and prefix is its first characters, kept so a person can tell
-- their keys apart.
CREATE TABLE api_keys (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name         text NOT NULL,
    prefix       text NOT NULL,
    key_hash     text NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_user_idx ON api_keys (user_id);

-- Registered upstream MCP servers. The slug becomes the prefix of every tool
-- the server exposes (slug__tool) and cannot contain an underscore, which is
-- what makes splitting a tool name on the first "__" unambiguous.
CREATE TABLE upstream_servers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{0,23}$'),
    name            text NOT NULL,
    url             text NOT NULL,
    auth_type       text NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none', 'bearer', 'header')),
    auth_header     text,   -- header name, when auth_type = 'header'
    auth_secret     bytea,  -- the credential, encrypted with a key held outside the database
    enabled         boolean NOT NULL DEFAULT true,
    timeout_seconds integer NOT NULL DEFAULT 30 CHECK (timeout_seconds BETWEEN 1 AND 300),
    -- Which tools members may use: every tool, only tools the server marks
    -- read-only, or none. Admins can always use every tool.
    member_access   text NOT NULL DEFAULT 'all' CHECK (member_access IN ('all', 'read_only', 'none')),
    created_by      uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Slugs that were created from the deployment's seed list. A seeded server that
-- an admin later removes must stay removed after a restart.
CREATE TABLE seeded_servers (
    slug      text PRIMARY KEY,
    seeded_at timestamptz NOT NULL DEFAULT now()
);

-- One row per tool call, and per rejected attempt that could be attributed.
-- It deliberately has no foreign keys: the log has to outlive the user, key
-- and server it describes.
CREATE TABLE audit_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    user_id        uuid,
    user_email     text NOT NULL,
    api_key_id     uuid,
    api_key_prefix text,
    source         text NOT NULL CHECK (source IN ('mcp', 'chat')),
    server_slug    text,
    tool           text,
    duration_ms    integer NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('ok', 'error', 'timeout', 'unreachable', 'denied')),
    error          text
);
CREATE INDEX audit_log_at_idx ON audit_log (at DESC);
CREATE INDEX audit_log_user_idx ON audit_log (user_id, at DESC);
