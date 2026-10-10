"""The gateway as an MCP server: one endpoint, authenticated by API key.

The server here has no tools of its own. Listing tools and calling a tool are
both handed to the Gateway core, acting as whoever owns the API key on the
request.
"""

from __future__ import annotations

import logging
from importlib.metadata import version

import asyncpg
from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import oauth, repo, security
from gateway.config import Settings
from gateway.core import Gateway, Principal
from gateway.toolsearch import META_TOOLS, ToolSearch

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# The same tools behind two: find_tools and call_tool. See toolsearch.py.
SEARCH_PATH = "/mcp/search"

INSTRUCTIONS = """\
This gateway gathers the tools of several MCP servers behind one endpoint.
Every tool is named <server>__<tool>, and its description starts with the server's name in brackets.
The list changes when an administrator enables or disables a server.
"""

SEARCH_INSTRUCTIONS = """\
This gateway gathers the tools of several MCP servers. Rather than listing them all, this endpoint has two tools: \
find_tools searches them, and call_tool runs one. Tools are named <server>__<tool>.
"""


class ApiKeyAuth:
    """Admits a request only with a live credential, and records who it belongs to.

    The credential is a bearer token of one of two kinds: an API key made in
    the dashboard, or an access token a client obtained by signing in with a
    browser (oauth.py). Either way it is looked up by its hash on every
    request, which is what makes revoking one take effect on the very next call.
    """

    def __init__(self, app: ASGIApp, pool: asyncpg.Pool, resource_metadata_url: str | None = None):
        self.app = app
        self._pool = pool
        # Sent with every 401 so that a client with no credential can find out
        # how to sign in. None when browser sign-in is not available.
        self._challenge = "Bearer" + (f' resource_metadata="{resource_metadata_url}"' if resource_metadata_url else "")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            await self._reject(scope, receive, send, "Send your gateway API key as a bearer token, or sign in.")
            return

        if presented.startswith(oauth.ACCESS_TOKEN_PREFIX):
            principal, refusal = await self._from_access_token(presented)
        else:
            principal, refusal = await self._from_api_key(presented)
        if principal is None:
            await self._reject(scope, receive, send, refusal)
            return
        if refusal:
            # The credential is known, so the attempt can be attributed even
            # though it is refused. Newer clients name the tool in a header.
            await repo.add_audit(
                self._pool,
                user_id=principal.user_id,
                user_email=principal.email,
                api_key_id=principal.api_key_id,
                api_key_prefix=principal.api_key_prefix,
                source="mcp",
                server_slug=None,
                tool=headers.get("mcp-name") or headers.get("mcp-method"),
                duration_ms=0,
                outcome="denied",
                error=refusal,
            )
            await self._reject(scope, receive, send, refusal)
            return

        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    async def _from_api_key(self, presented: str) -> tuple[Principal | None, str | None]:
        """Who an API key belongs to, and why it is refused if it is."""
        key = await repo.find_api_key(self._pool, security.hash_token(presented))
        if key is None:
            return None, "This API key is not recognised."
        principal = Principal(
            user_id=key.user_id,
            email=key.user_email,
            role=key.user_role,
            source="mcp",
            api_key_id=key.id,
            api_key_prefix=key.prefix,
        )
        if key.revoked_at is not None:
            return principal, "This API key has been revoked."
        if not key.user_active:
            return principal, "This account has been deactivated."
        await repo.touch_api_key(self._pool, key.id)
        return principal, None

    async def _from_access_token(self, presented: str) -> tuple[Principal | None, str | None]:
        """Who signed in to get this access token. An expired or revoked one is simply unknown:
        the client then uses its refresh token, or sends the person to sign in again."""
        grant = await oauth.find_grant_by_access_token(self._pool, presented)
        if grant is None:
            return None, "This access token has expired or been revoked."
        principal = Principal(
            user_id=grant.user_id,
            email=grant.user_email,
            role=grant.user_role,
            source="mcp",
            # In the audit log's "via" column this reads as where the call came from.
            api_key_id=grant.id,
            api_key_prefix=f"signed in: {grant.client_name}"[:60],
        )
        if not grant.user_active:
            return principal, "This account has been deactivated."
        await oauth.touch_grant(self._pool, grant.id)
        return principal, None

    async def _reject(self, scope: Scope, receive: Receive, send: Send, message: str) -> None:
        response = JSONResponse(
            {"error": "unauthorized", "message": message}, status_code=401, headers={"WWW-Authenticate": self._challenge}
        )
        await response(scope, receive, send)


def build_mcp_app(gateway: Gateway, settings: Settings) -> tuple[list[Server], ASGIApp]:
    """Returns the MCP servers and one ASGI app serving both endpoints. It expects ApiKeyAuth in front of it."""

    def principal_of(ctx: ServerRequestContext) -> Principal:
        return ctx.request.state.principal

    async def list_tools(ctx: ServerRequestContext, _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await gateway.list_tools(principal_of(ctx)))

    async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await gateway.call_tool(principal_of(ctx), params.name, dict(params.arguments or {}))

    search = ToolSearch(gateway)

    async def list_meta_tools(_: ServerRequestContext, __: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=META_TOOLS)

    async def call_meta_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await search.call(principal_of(ctx), params.name, dict(params.arguments or {}))

    hosts = [settings.public_host, "localhost", "127.0.0.1", *settings.extra_allowed_hosts]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[form for host in hosts for form in (host, f"{host}:*")],
        # A browser sends Origin; other MCP clients do not. Only pages served
        # from the gateway's own address may call it from a browser.
        allowed_origins=[settings.public_url],
    )

    def serve(path: str, instructions: str, on_list_tools, on_call_tool) -> tuple[Server, Starlette]:
        server = Server(
            "mcp-gateway",
            title="Team MCP gateway",
            version=version("gateway"),
            instructions=instructions,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
        app = server.streamable_http_app(
            streamable_http_path=path,
            # No session is kept for any client, so every gateway replica can
            # answer every request.
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )
        return server, app

    full_server, full_app = serve(MCP_PATH, INSTRUCTIONS, list_tools, call_tool)
    search_server, search_app = serve(SEARCH_PATH, SEARCH_INSTRUCTIONS, list_meta_tools, call_meta_tool)

    async def both(scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        chosen = search_app if path == SEARCH_PATH or path.startswith(f"{SEARCH_PATH}/") else full_app
        await chosen(scope, receive, send)

    return [full_server, search_server], both
