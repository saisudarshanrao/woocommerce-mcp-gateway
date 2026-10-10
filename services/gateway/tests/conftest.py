"""Test fixtures: a real Postgres in Docker, and fake upstream MCP servers in-process."""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import asyncpg
import httpx2
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from gateway import db, repo, security
from gateway.core import Gateway, Principal
from gateway.upstreams import UpstreamManager

POSTGRES_IMAGE = "postgres:18.6-alpine"
TABLES = "oauth_grants, oauth_codes, oauth_requests, oauth_clients, stores, rate_counters, audit_log, seeded_servers, upstream_servers, api_keys, invites, sessions, users"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_url() -> str:
    """Starts a throwaway Postgres container for the test session."""
    container = subprocess.run(
        ["docker", "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=test", "-p", "127.0.0.1::5432", POSTGRES_IMAGE],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    try:
        port = subprocess.run(
            ["docker", "port", container, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout.strip().rsplit(":", 1)[1]
        url = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
        for _ in range(120):
            ready = subprocess.run(["docker", "exec", container, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        # pg_isready turns true once during initialisation, before the final restart.
        time.sleep(1.0)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    for _ in range(40):
        try:
            pool = db.Database(database_url)
            await pool.connect()
            break
        except (OSError, asyncpg.PostgresError):
            await asyncio.sleep(0.25)
    else:
        raise RuntimeError("the test database did not come up")
    await db.migrate(pool)
    await pool.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


# --- fake upstream MCP servers -----------------------------------------------


def shop_server() -> MCPServer:
    mcp = MCPServer("shop")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_orders() -> list[str]:
        """Orders in the shop."""
        return ["order-1", "order-2"]

    @mcp.tool()
    def create_product(name: str) -> str:
        """Create a product."""
        return f"created {name}"

    @mcp.tool()
    def broken() -> str:
        """Always fails."""
        raise ToolError("the shop is closed")

    return mcp


def warehouse_server() -> MCPServer:
    mcp = MCPServer("warehouse")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_orders() -> list[str]:
        """Orders waiting in the warehouse."""
        return ["pallet-9"]

    @mcp.tool()
    async def slow(seconds: float) -> str:
        """Takes a while."""
        await asyncio.sleep(seconds)
        return "done"

    @mcp.tool(name="not.portable")
    def dotted() -> str:
        """A name the gateway cannot expose."""
        return "x"

    return mcp


class Upstreams:
    """In-process MCP servers reachable at http://<name>.test/mcp, some of which can be switched off."""

    def __init__(self) -> None:
        self.apps: dict[str, object] = {}
        self.down: set[str] = set()
        self.seen_headers: dict[str, dict[str, str]] = {}

    def http_client(self, server: repo.Server, headers: dict[str, str], timeout: float) -> httpx2.AsyncClient:
        host = httpx2.URL(server.url).host
        self.seen_headers[host] = headers
        if host in self.down or host not in self.apps:
            def refuse(request: httpx2.Request) -> httpx2.Response:
                raise httpx2.ConnectError("All connection attempts failed", request=request)
            return httpx2.AsyncClient(transport=httpx2.MockTransport(refuse), headers=headers)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.apps[host]), headers=headers, timeout=timeout)


@pytest.fixture
async def upstreams() -> AsyncIterator[Upstreams]:
    fakes = Upstreams()
    async with AsyncExitStack() as stack:
        for host, server in (("shop.test", shop_server()), ("warehouse.test", warehouse_server())):
            app = server.streamable_http_app(
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )
            # An ASGI app's startup normally runs under a web server; do it by hand.
            await stack.enter_async_context(app.router.lifespan_context(app))
            fakes.apps[host] = app
        yield fakes


CIPHER = security.CredentialCipher("test-encryption-key")


@pytest.fixture
async def manager(pool: asyncpg.Pool, upstreams: Upstreams) -> AsyncIterator[UpstreamManager]:
    manager = UpstreamManager(pool, CIPHER, probe_timeout_seconds=2.0, http_client_factory=upstreams.http_client)
    yield manager
    await manager.stop()


@pytest.fixture
def gateway(pool: asyncpg.Pool, manager: UpstreamManager) -> Gateway:
    return Gateway(pool, manager)


async def add_server(pool: asyncpg.Pool, slug: str, host: str, **extra) -> repo.Server:
    values = {"slug": slug, "name": slug.title(), "url": f"http://{host}/mcp", "auth_type": "none", **extra}
    return await repo.create_server(pool, created_by=None, **values)


async def add_user(pool: asyncpg.Pool, email: str, role: str = "member", password: str = "pw-for-tests") -> repo.User:
    return await repo.create_user(pool, email, email.split("@")[0].title(), role, security.hash_password(password))


def principal_for(user: repo.User, source: str = "mcp") -> Principal:
    return Principal(user_id=user.id, email=user.email, role=user.role, source=source)
