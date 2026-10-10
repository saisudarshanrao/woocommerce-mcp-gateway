"""Entry point: python -m demo_mcp <tasks|sandbox>

An optional static credential is read from the environment, so the same image
can demonstrate both kinds the gateway supports:

    AUTH_TOKEN                the secret a caller must present; unset means no check
    AUTH_HEADER               the header it arrives in. "Authorization" (the
                              default) means a bearer token; anything else is
                              compared as the header's whole value.
    ALLOWED_HOSTS             comma-separated Host names to answer to
"""

from __future__ import annotations

import hmac
import logging
import os
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from demo_mcp import sandbox, tasks

SERVERS = {"tasks": tasks.build, "sandbox": sandbox.build}
HEALTH_PATH = "/healthz"


class StaticCredential:
    """Requires one fixed secret in one header on every request except the health check."""

    def __init__(self, app: ASGIApp, header: str, token: str):
        self.app = app
        self._header = header.lower()
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        presented = Headers(scope=scope).get(self._header, "")
        if self._header == "authorization":
            scheme, _, presented = presented.partition(" ")
            presented = presented if scheme.lower() == "bearer" else ""
        if hmac.compare_digest(presented.strip().encode(), self._token):
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)


def build_app(name: str, env: dict[str, str]) -> ASGIApp:
    mcp = SERVERS[name]()

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    hosts = [host.strip() for host in env.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
    app: ASGIApp = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[form for host in hosts for form in (host, f"{host}:*")],
        ),
    )
    if env.get("AUTH_TOKEN"):
        app = StaticCredential(app, env.get("AUTH_HEADER", "Authorization"), env["AUTH_TOKEN"])
    return app


class _SkipHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return HEALTH_PATH not in record.getMessage()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SERVERS:
        sys.exit(f"usage: python -m demo_mcp <{'|'.join(SERVERS)}>")
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_SkipHealthChecks())
    uvicorn.run(build_app(sys.argv[1], dict(os.environ)), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
