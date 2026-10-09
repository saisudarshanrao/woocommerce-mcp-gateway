"""The HTTP application around the MCP server.

Three things sit in front of the tools:

  * a bearer-token check, so only a caller holding the token (the gateway) gets
    as far as MCP;
  * a Host header allowlist, the SDK's guard against a web page tricking a
    browser into calling a server on a private network;
  * a health endpoint for Kubernetes probes, which bypasses both.
"""

from __future__ import annotations

import hmac

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from woo_mcp.config import Settings
from woo_mcp.server import build_server
from woo_mcp.woocommerce import WooCommerce

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"


class BearerTokenMiddleware:
    """Rejects any HTTP request that does not carry the expected bearer token."""

    def __init__(self, app: ASGIApp, token: str, exempt_paths: tuple[str, ...] = ()):
        self.app = app
        self._token = token.encode()
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes are the server's own startup and shutdown events.
        if scope["type"] != "http" or scope["path"] in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        scheme, _, presented = Headers(scope=scope).get("authorization", "").partition(" ")
        # compare_digest takes the same time whether the first byte or the last
        # one differs, so response timing reveals nothing about the token.
        if scheme.lower() == "bearer" and hmac.compare_digest(presented.strip().encode(), self._token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"error": "unauthorized", "message": "A valid bearer token is required."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def build_app(settings: Settings, woo: WooCommerce | None = None) -> Starlette:
    woo = woo or WooCommerce(settings)
    mcp = build_server(woo)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def healthz(_: Request) -> Response:
        # Deliberately does not ask the store. A store outage should surface as
        # a clear tool error, not as this pod dropping out of its Service.
        return PlainTextResponse("ok")

    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless with plain JSON answers: every request stands on its own, so
        # there are no sessions to lose when the pod restarts and any replica
        # can answer any request.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # The check is an exact match on the Host header, which carries a
            # port only when it is not the default. List both forms.
            allowed_hosts=[form for host in settings.allowed_hosts for form in (host, f"{host}:*")],
        ),
    )
    app.add_middleware(BearerTokenMiddleware, token=settings.auth_token, exempt_paths=(HEALTH_PATH,))
    return app
