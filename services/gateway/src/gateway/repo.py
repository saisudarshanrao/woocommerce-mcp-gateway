"""Every SQL statement the gateway runs, one function each.

Functions take a connection pool and plain values, and return dataclasses, so
the rest of the code never sees a database row or writes SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class User:
    id: UUID
    email: str
    name: str
    role: str
    active: bool
    created_at: datetime


@dataclass(frozen=True)
class ApiKey:
    id: UUID
    user_id: UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    # Filled when the query joins the owner.
    user_email: str = ""
    user_name: str = ""
    user_role: str = ""
    user_active: bool = True


@dataclass(frozen=True)
class Invite:
    id: UUID
    email: str
    role: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None


@dataclass(frozen=True)
class Server:
    id: UUID
    slug: str
    name: str
    url: str
    auth_type: str
    auth_header: str | None
    auth_secret: bytes | None
    enabled: bool
    timeout_seconds: int
    member_access: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: datetime
    user_id: UUID | None
    user_email: str
    api_key_prefix: str | None
    source: str
    server_slug: str | None
    tool: str | None
    duration_ms: int
    outcome: str
    error: str | None


def _to(cls, row: asyncpg.Record | None):
    if row is None:
        return None
    names = {field.name for field in fields(cls)}
    return cls(**{key: value for key, value in row.items() if key in names})


USER_COLUMNS = "id, email, name, role, active, created_at"
SERVER_COLUMNS = (
    "id, slug, name, url, auth_type, auth_header, auth_secret, enabled, timeout_seconds, member_access, "
    "created_at, updated_at"
)

# --- users -------------------------------------------------------------------


async def count_users(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT count(*) FROM users")


async def create_user(pool: asyncpg.Pool, email: str, name: str, role: str, password_hash: str) -> User:
    row = await pool.fetchrow(
        f"INSERT INTO users (email, name, role, password_hash) VALUES ($1, $2, $3, $4) RETURNING {USER_COLUMNS}",
        email.lower(), name, role, password_hash,
    )
    return _to(User, row)


async def get_user(pool: asyncpg.Pool, user_id: UUID) -> User | None:
    return _to(User, await pool.fetchrow(f"SELECT {USER_COLUMNS} FROM users WHERE id = $1", user_id))


async def get_user_with_password(pool: asyncpg.Pool, email: str) -> tuple[User, str] | None:
    row = await pool.fetchrow(f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE email = $1", email.lower())
    return (_to(User, row), row["password_hash"]) if row else None


async def list_users(pool: asyncpg.Pool) -> list[User]:
    rows = await pool.fetch(f"SELECT {USER_COLUMNS} FROM users ORDER BY created_at")
    return [_to(User, row) for row in rows]


async def update_user(pool: asyncpg.Pool, user_id: UUID, *, role: str | None, active: bool | None) -> User | None:
    row = await pool.fetchrow(
        f"UPDATE users SET role = COALESCE($2, role), active = COALESCE($3, active) "
        f"WHERE id = $1 RETURNING {USER_COLUMNS}",
        user_id, role, active,
    )
    return _to(User, row)


async def delete_user(pool: asyncpg.Pool, user_id: UUID) -> bool:
    """Removes a user. Their sessions and keys go with them; their audit history stays."""
    return await pool.execute("DELETE FROM users WHERE id = $1", user_id) == "DELETE 1"


async def count_active_admins(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT count(*) FROM users WHERE role = 'admin' AND active")


# --- sessions ----------------------------------------------------------------


async def create_session(pool: asyncpg.Pool, token_hash: str, user_id: UUID, expires_at: datetime) -> None:
    await pool.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ($1, $2, $3)", token_hash, user_id, expires_at
    )


async def get_session_user(pool: asyncpg.Pool, token_hash: str) -> User | None:
    """The signed-in user for a session token, if the session is live and the account active."""
    row = await pool.fetchrow(
        "SELECT u.id, u.email, u.name, u.role, u.active, u.created_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = $1 AND s.expires_at > now() AND u.active",
        token_hash,
    )
    return _to(User, row)


async def delete_session(pool: asyncpg.Pool, token_hash: str) -> None:
    await pool.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)


async def delete_user_sessions(pool: asyncpg.Pool, user_id: UUID) -> None:
    await pool.execute("DELETE FROM sessions WHERE user_id = $1", user_id)


async def delete_expired_sessions(pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM sessions WHERE expires_at <= now()")


# --- invites -----------------------------------------------------------------

INVITE_COLUMNS = "id, email, role, created_at, expires_at, accepted_at"


async def create_invite(
    pool: asyncpg.Pool, token_hash: str, email: str, role: str, invited_by: UUID, expires_at: datetime
) -> Invite:
    row = await pool.fetchrow(
        f"INSERT INTO invites (token_hash, email, role, invited_by, expires_at) VALUES ($1, $2, $3, $4, $5) "
        f"RETURNING {INVITE_COLUMNS}",
        token_hash, email.lower(), role, invited_by, expires_at,
    )
    return _to(Invite, row)


async def list_pending_invites(pool: asyncpg.Pool) -> list[Invite]:
    rows = await pool.fetch(
        f"SELECT {INVITE_COLUMNS} FROM invites WHERE accepted_at IS NULL AND expires_at > now() ORDER BY created_at"
    )
    return [_to(Invite, row) for row in rows]


async def get_open_invite(pool: asyncpg.Pool, token_hash: str) -> Invite | None:
    row = await pool.fetchrow(
        f"SELECT {INVITE_COLUMNS} FROM invites WHERE token_hash = $1 AND accepted_at IS NULL AND expires_at > now()",
        token_hash,
    )
    return _to(Invite, row)


async def accept_invite(pool: asyncpg.Pool, invite_id: UUID, name: str, password_hash: str) -> User | None:
    """Creates the user and closes the invite together, so a link works exactly once."""
    async with pool.acquire() as connection, connection.transaction():
        invite = await connection.fetchrow(
            "UPDATE invites SET accepted_at = now() WHERE id = $1 AND accepted_at IS NULL AND expires_at > now() "
            "RETURNING email, role",
            invite_id,
        )
        if invite is None:
            return None
        row = await connection.fetchrow(
            f"INSERT INTO users (email, name, role, password_hash) VALUES ($1, $2, $3, $4) RETURNING {USER_COLUMNS}",
            invite["email"], name, invite["role"], password_hash,
        )
        return _to(User, row)


async def delete_invite(pool: asyncpg.Pool, invite_id: UUID) -> bool:
    return await pool.execute("DELETE FROM invites WHERE id = $1 AND accepted_at IS NULL", invite_id) == "DELETE 1"


# --- API keys ----------------------------------------------------------------

_KEY_WITH_OWNER = (
    "SELECT k.id, k.user_id, k.name, k.prefix, k.created_at, k.last_used_at, k.revoked_at, "
    "u.email AS user_email, u.name AS user_name, u.role AS user_role, u.active AS user_active "
    "FROM api_keys k JOIN users u ON u.id = k.user_id "
)


async def create_api_key(pool: asyncpg.Pool, user_id: UUID, name: str, prefix: str, key_hash: str) -> ApiKey:
    row = await pool.fetchrow(
        "INSERT INTO api_keys (user_id, name, prefix, key_hash) VALUES ($1, $2, $3, $4) "
        "RETURNING id, user_id, name, prefix, created_at, last_used_at, revoked_at",
        user_id, name, prefix, key_hash,
    )
    return _to(ApiKey, row)


async def find_api_key(pool: asyncpg.Pool, key_hash: str) -> ApiKey | None:
    """Looks a key up by its hash. Revoked keys are returned too, so that a
    rejected attempt can still be attributed to its owner in the audit log."""
    return _to(ApiKey, await pool.fetchrow(_KEY_WITH_OWNER + "WHERE k.key_hash = $1", key_hash))


async def list_api_keys(pool: asyncpg.Pool, user_id: UUID | None = None) -> list[ApiKey]:
    if user_id is None:
        rows = await pool.fetch(_KEY_WITH_OWNER + "ORDER BY k.created_at DESC")
    else:
        rows = await pool.fetch(_KEY_WITH_OWNER + "WHERE k.user_id = $1 ORDER BY k.created_at DESC", user_id)
    return [_to(ApiKey, row) for row in rows]


async def revoke_api_key(pool: asyncpg.Pool, key_id: UUID, owner: UUID | None = None) -> bool:
    """Revokes a key. With `owner`, only if it belongs to that user."""
    result = await pool.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL "
        "AND ($2::uuid IS NULL OR user_id = $2)",
        key_id, owner,
    )
    return result == "UPDATE 1"


async def touch_api_key(pool: asyncpg.Pool, key_id: UUID) -> None:
    # At most one write a minute per key, however busy the key is.
    await pool.execute(
        "UPDATE api_keys SET last_used_at = now() "
        "WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
        key_id,
    )


# --- upstream servers --------------------------------------------------------


async def list_servers(pool: asyncpg.Pool) -> list[Server]:
    rows = await pool.fetch(f"SELECT {SERVER_COLUMNS} FROM upstream_servers ORDER BY created_at")
    return [_to(Server, row) for row in rows]


async def get_server(pool: asyncpg.Pool, server_id: UUID) -> Server | None:
    return _to(Server, await pool.fetchrow(f"SELECT {SERVER_COLUMNS} FROM upstream_servers WHERE id = $1", server_id))


async def get_server_by_slug(pool: asyncpg.Pool, slug: str) -> Server | None:
    return _to(Server, await pool.fetchrow(f"SELECT {SERVER_COLUMNS} FROM upstream_servers WHERE slug = $1", slug))


async def create_server(pool: asyncpg.Pool, *, created_by: UUID | None, **values: Any) -> Server:
    row = await pool.fetchrow(
        "INSERT INTO upstream_servers "
        "(slug, name, url, auth_type, auth_header, auth_secret, enabled, timeout_seconds, member_access, created_by) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING {SERVER_COLUMNS}",
        values["slug"], values["name"], values["url"], values["auth_type"], values.get("auth_header"),
        values.get("auth_secret"), values.get("enabled", True), values.get("timeout_seconds", 30),
        values.get("member_access", "all"), created_by,
    )
    return _to(Server, row)


# Columns an edit may change. The slug is not among them: it is part of every
# tool name the server exposes.
_EDITABLE = ("name", "url", "auth_type", "auth_header", "auth_secret", "enabled", "timeout_seconds", "member_access")


async def update_server(pool: asyncpg.Pool, server_id: UUID, changes: dict[str, Any]) -> Server | None:
    changes = {column: value for column, value in changes.items() if column in _EDITABLE}
    if not changes:
        return await get_server(pool, server_id)
    assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(changes, start=2))
    row = await pool.fetchrow(
        f"UPDATE upstream_servers SET {assignments}, updated_at = now() WHERE id = $1 RETURNING {SERVER_COLUMNS}",
        server_id, *changes.values(),
    )
    return _to(Server, row)


async def delete_server(pool: asyncpg.Pool, server_id: UUID) -> bool:
    return await pool.execute("DELETE FROM upstream_servers WHERE id = $1", server_id) == "DELETE 1"


async def claim_seed(pool: asyncpg.Pool, slug: str) -> bool:
    """Records that a seed has been applied. False if it already was."""
    return await pool.execute(
        "INSERT INTO seeded_servers (slug) VALUES ($1) ON CONFLICT DO NOTHING", slug
    ) == "INSERT 0 1"


# --- audit log ---------------------------------------------------------------


async def add_audit(
    pool: asyncpg.Pool,
    *,
    user_id: UUID | None,
    user_email: str,
    api_key_id: UUID | None,
    api_key_prefix: str | None,
    source: str,
    server_slug: str | None,
    tool: str | None,
    duration_ms: int,
    outcome: str,
    error: str | None,
) -> None:
    await pool.execute(
        "INSERT INTO audit_log (user_id, user_email, api_key_id, api_key_prefix, source, server_slug, tool, "
        "duration_ms, outcome, error) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        user_id, user_email, api_key_id, api_key_prefix, source, server_slug, tool, duration_ms, outcome,
        error[:500] if error else None,
    )


async def list_audit(
    pool: asyncpg.Pool,
    *,
    user_id: UUID | None = None,
    server_slug: str | None = None,
    outcome: str | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """Newest first. Every filter is optional; a NULL parameter matches everything."""
    rows = await pool.fetch(
        "SELECT id, at, user_id, user_email, api_key_prefix, source, server_slug, tool, duration_ms, outcome, error "
        "FROM audit_log "
        "WHERE ($1::uuid IS NULL OR user_id = $1) AND ($2::text IS NULL OR server_slug = $2) "
        "AND ($3::text IS NULL OR outcome = $3) AND ($4::bigint IS NULL OR id < $4) "
        "ORDER BY id DESC LIMIT $5",
        user_id, server_slug, outcome, before_id, limit,
    )
    return [_to(AuditEntry, row) for row in rows]


@dataclass(frozen=True)
class ServerStats:
    server_slug: str
    calls: int
    failures: int
    median_ms: float
    p95_ms: float


async def server_stats(pool: asyncpg.Pool, *, hours: int, user_id: UUID | None = None) -> list[ServerStats]:
    """Calls, failures and latency per server over the last `hours`, worked out from the audit log."""
    rows = await pool.fetch(
        "SELECT server_slug, count(*) AS calls, "
        "count(*) FILTER (WHERE outcome <> 'ok') AS failures, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS median_ms, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms "
        "FROM audit_log "
        "WHERE at > now() - make_interval(hours => $1) AND server_slug IS NOT NULL "
        "AND ($2::uuid IS NULL OR user_id = $2) "
        "GROUP BY server_slug ORDER BY calls DESC",
        hours, user_id,
    )
    return [_to(ServerStats, row) for row in rows]


# --- rate limits ---------------------------------------------------------------


async def count_call(pool: asyncpg.Pool, subject: str) -> int:
    """Counts one more call for the subject in the current minute and returns the new total.

    One statement, so two replicas counting at the same moment cannot both read
    the old total. It also drops the subject's row for any earlier minute.
    """
    return await pool.fetchval(
        "WITH old AS (DELETE FROM rate_counters WHERE subject = $1 AND minute < date_trunc('minute', now())) "
        "INSERT INTO rate_counters (subject, minute, calls) VALUES ($1, date_trunc('minute', now()), 1) "
        "ON CONFLICT (subject, minute) DO UPDATE SET calls = rate_counters.calls + 1 "
        "RETURNING calls",
        subject,
    )
