"""Signing in from an MCP client with a browser instead of an API key.

This is the OAuth 2.1 flow the MCP authorization specification describes. The
gateway plays both parts: it is the protected resource (/mcp) and the
authorization server that issues tokens for it. From the client's side:

    1. it calls /mcp with no token and gets 401, with a pointer to a document
       that names the authorization server (this gateway)
    2. it reads the server's metadata and registers itself (POST /register)
    3. it opens the browser at /authorize, with a code challenge it made up
    4. the person signs in to the dashboard as usual and clicks Allow
    5. the browser is sent back to the client with a one-time code
    6. the client swaps the code, plus the secret behind the challenge, for an
       access token (POST /token) and uses it as its bearer token

The HTTP endpoints, and the checking of redirect addresses and of the code
challenge (PKCE), come from the MCP SDK. What is written here is the part that
is the gateway's own: where clients, codes and tokens are stored, and step 4,
which ties a token to a dashboard user.

A token is a random string, stored only as a hash, exactly like an API key. So
the gateway treats the two alike from then on: the same per-request lookup, the
same roles and permissions, the same audit log.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.routing import Route

from gateway import security

logger = logging.getLogger(__name__)

ACCESS_TOKEN_PREFIX = "mgo_"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_DAYS = 30
CODE_SECONDS = 120
REQUEST_MINUTES = 10


def new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Grant:
    """One client allowed to act as one user."""

    id: UUID
    client_id: str
    client_name: str
    user_id: UUID
    user_email: str
    user_role: str
    user_active: bool
    created_at: datetime
    last_used_at: datetime | None


_GRANT = (
    "SELECT g.id, g.client_id, c.name AS client_name, g.user_id, u.email AS user_email, u.role AS user_role, "
    "u.active AS user_active, g.created_at, g.last_used_at "
    "FROM oauth_grants g JOIN oauth_clients c ON c.client_id = g.client_id JOIN users u ON u.id = g.user_id "
)


async def find_grant_by_access_token(pool: asyncpg.Pool, token: str) -> Grant | None:
    """The grant an access token belongs to, if the token has not expired or been revoked."""
    row = await pool.fetchrow(
        _GRANT + "WHERE g.access_hash = $1 AND g.access_expires_at > now()", security.hash_token(token)
    )
    return Grant(**row) if row else None


async def touch_grant(pool: asyncpg.Pool, grant_id: UUID) -> None:
    await pool.execute(
        "UPDATE oauth_grants SET last_used_at = now() "
        "WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
        grant_id,
    )


async def list_grants(pool: asyncpg.Pool, user_id: UUID | None) -> list[Grant]:
    rows = await pool.fetch(
        _GRANT + "WHERE ($1::uuid IS NULL OR g.user_id = $1) AND g.refresh_expires_at > now() ORDER BY g.created_at DESC",
        user_id,
    )
    return [Grant(**row) for row in rows]


async def revoke_grant(pool: asyncpg.Pool, grant_id: UUID, owner: UUID | None) -> bool:
    """Ends a grant: its access token stops working on the next request. With `owner`, only that user's."""
    result = await pool.execute(
        "DELETE FROM oauth_grants WHERE id = $1 AND ($2::uuid IS NULL OR user_id = $2)", grant_id, owner
    )
    return result == "DELETE 1"


class OAuthStore:
    """What the SDK's OAuth endpoints ask of the gateway: remember clients, codes and tokens."""

    def __init__(self, pool: asyncpg.Pool, public_url: str):
        self._pool = pool
        self._public_url = public_url

    # --- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self._pool.fetchval("SELECT info FROM oauth_clients WHERE client_id = $1", client_id)
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._pool.execute(
            "INSERT INTO oauth_clients (client_id, name, info) VALUES ($1, $2, $3)",
            client_info.client_id, (client_info.client_name or "An MCP client")[:80], client_info.model_dump_json(),
        )

    # --- the browser step --------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Called when the client sends the browser to /authorize. Returns where the browser goes next.

        Nothing is granted here. The request is put aside under a random id and
        the browser is sent to the dashboard's consent page, where the person
        has to be signed in. `approve` below finishes the job.
        """
        request_id = new_secret()
        await self._pool.execute(
            "INSERT INTO oauth_requests (id, client_id, params, expires_at) VALUES ($1, $2, $3, $4)",
            request_id, client.client_id, params.model_dump_json(),
            datetime.now(UTC) + timedelta(minutes=REQUEST_MINUTES),
        )
        return f"{self._public_url}/consent?request={request_id}"

    async def pending(self, request_id: str) -> tuple[str, AuthorizationParams] | None:
        """A sign-in waiting for an answer: the client's name, and what it asked for."""
        row = await self._pool.fetchrow(
            "SELECT c.name, r.params FROM oauth_requests r JOIN oauth_clients c ON c.client_id = r.client_id "
            "WHERE r.id = $1 AND r.expires_at > now()",
            request_id,
        )
        return (row["name"], AuthorizationParams.model_validate_json(row["params"])) if row else None

    async def approve(self, request_id: str, user_id: UUID) -> str | None:
        """The signed-in user said yes. Returns the address that takes the code back to the client."""
        async with self._pool.acquire() as connection, connection.transaction():
            # Deleting the request as it is read makes a second click do nothing.
            row = await connection.fetchrow(
                "DELETE FROM oauth_requests WHERE id = $1 AND expires_at > now() RETURNING client_id, params", request_id
            )
            if row is None:
                return None
            params = AuthorizationParams.model_validate_json(row["params"])
            code = new_secret()
            await connection.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, user_id, params, expires_at) VALUES ($1, $2, $3, $4, $5)",
                security.hash_token(code), row["client_id"], user_id, row["params"],
                datetime.now(UTC) + timedelta(seconds=CODE_SECONDS),
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def deny(self, request_id: str) -> str | None:
        """The user said no. Returns the address that tells the client so."""
        row = await self._pool.fetchrow("DELETE FROM oauth_requests WHERE id = $1 RETURNING params", request_id)
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # --- codes and tokens ----------------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = await self._pool.fetchrow(
            "SELECT client_id, user_id, params, expires_at FROM oauth_codes WHERE code_hash = $1",
            security.hash_token(authorization_code),
        )
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        # The SDK checks the expiry, the client, the redirect address and the
        # code challenge against what the client presents now.
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [],
            expires_at=row["expires_at"].timestamp(),
            client_id=row["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(row["user_id"]),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        async with self._pool.acquire() as connection, connection.transaction():
            # A code works once: it is deleted in the transaction that issues the tokens.
            user_id = await connection.fetchval(
                "DELETE FROM oauth_codes WHERE code_hash = $1 RETURNING user_id", security.hash_token(authorization_code.code)
            )
            if user_id is None:
                raise TokenError("invalid_grant", "This code has already been used.")
            access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret()
            await connection.execute(
                "INSERT INTO oauth_grants (client_id, user_id, access_hash, access_expires_at, refresh_hash, refresh_expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                client.client_id, user_id, security.hash_token(access), _in(seconds=ACCESS_TOKEN_SECONDS),
                security.hash_token(refresh), _in(days=REFRESH_TOKEN_DAYS),
            )
        return OAuthToken(access_token=access, expires_in=ACCESS_TOKEN_SECONDS, refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await self._pool.fetchrow(
            "SELECT client_id, user_id, refresh_expires_at FROM oauth_grants WHERE refresh_hash = $1 AND client_id = $2",
            security.hash_token(refresh_token), client.client_id,
        )
        if row is None:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=[],
            expires_at=int(row["refresh_expires_at"].timestamp()), subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        """Issues the next access token. The refresh token is replaced too, so a stolen one stops working once used."""
        access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret()
        await self._pool.execute(
            "UPDATE oauth_grants SET access_hash = $2, access_expires_at = $3, refresh_hash = $4, refresh_expires_at = $5 "
            "WHERE refresh_hash = $1",
            security.hash_token(refresh_token.token), security.hash_token(access), _in(seconds=ACCESS_TOKEN_SECONDS),
            security.hash_token(refresh), _in(days=REFRESH_TOKEN_DAYS),
        )
        return OAuthToken(access_token=access, expires_in=ACCESS_TOKEN_SECONDS, refresh_token=refresh)

    async def load_access_token(self, token: str) -> AccessToken | None:
        grant = await find_grant_by_access_token(self._pool, token)
        if grant is None:
            return None
        return AccessToken(token=token, client_id=grant.client_id, scopes=[], subject=str(grant.user_id))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """A client signing out. Either of its tokens ends the whole grant."""
        hashed = security.hash_token(token.token)
        await self._pool.execute("DELETE FROM oauth_grants WHERE access_hash = $1 OR refresh_hash = $1", hashed)

    async def delete_expired(self) -> None:
        await self._pool.execute("DELETE FROM oauth_requests WHERE expires_at <= now()")
        await self._pool.execute("DELETE FROM oauth_codes WHERE expires_at <= now()")
        await self._pool.execute("DELETE FROM oauth_grants WHERE refresh_expires_at <= now()")
        # Clients that registered and have had nobody signed in for a month.
        await self._pool.execute(
            "DELETE FROM oauth_clients c WHERE c.created_at < now() - interval '30 days' "
            "AND NOT EXISTS (SELECT 1 FROM oauth_grants g WHERE g.client_id = c.client_id)"
        )


def _in(**delta: float) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


def allowed_issuer(public_url: str) -> bool:
    """OAuth needs HTTPS, because tokens travel in requests. Plain HTTP is accepted only for
    an address that never leaves this machine: localhost, 127.0.0.1 and names under .localhost."""
    parsed = urlsplit(public_url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" or host in ("localhost", "127.0.0.1") or host.endswith(".localhost")


def resource_metadata_url(public_url: str, mcp_path: str) -> str:
    """Where a client that got a 401 from the MCP endpoint learns how to sign in."""
    return f"{public_url}/.well-known/oauth-protected-resource{mcp_path}"


def oauth_routes(store: OAuthStore, public_url: str, mcp_path: str) -> list[Route]:
    """The endpoints of the authorization server, and the document that points clients to it.

    These are the SDK's handlers, put together by hand rather than with its
    create_auth_routes, which refuses any http address that is not literally
    "localhost" and so would refuse a local cluster at http://gateway.localhost.
    """
    # Parsed so that "https://host" stays exactly that, with no "/" added: clients
    # compare the issuer in the two metadata documents character for character.
    issuer = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True)).validate_python(public_url)
    registration = ClientRegistrationOptions(enabled=True)
    revocation = RevocationOptions(enabled=True)
    metadata = build_metadata(issuer, None, registration, revocation)
    authenticator = ClientAuthenticator(store)
    return [
        Route(
            "/.well-known/oauth-authorization-server",
            cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        # The one endpoint a browser visits. It redirects to the consent page.
        Route("/authorize", AuthorizationHandler(store).handle, methods=["GET", "POST"]),
        Route("/token", cors_middleware(TokenHandler(store, authenticator).handle, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
        Route(
            "/register",
            cors_middleware(RegistrationHandler(store, options=registration).handle, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route("/revoke", cors_middleware(RevocationHandler(store, authenticator).handle, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
        *create_protected_resource_routes(
            AnyHttpUrl(f"{public_url}{mcp_path}"), [issuer], resource_name="Team MCP gateway"
        ),
    ]
