"""The bearer-token check in front of the MCP endpoint."""

import httpx2
import pytest
from starlette.responses import PlainTextResponse

from woo_mcp.app import BearerTokenMiddleware, build_app

from .conftest import SETTINGS

pytestmark = pytest.mark.anyio


async def downstream(scope, receive, send):
    await PlainTextResponse("reached")(scope, receive, send)


@pytest.fixture
async def http():
    guarded = BearerTokenMiddleware(downstream, token="right-token", exempt_paths=("/healthz",))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=guarded), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer right-token-and-more"},
        {"Authorization": "Basic right-token"},
        {"Authorization": "right-token"},
        {"Authorization": "Bearer "},
    ],
)
async def test_requests_without_the_exact_token_are_refused(http, headers):
    response = await http.post("/mcp", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.text != "reached"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_the_right_token_gets_through(http, scheme):
    response = await http.post("/mcp", headers={"Authorization": f"{scheme} right-token"})

    assert response.status_code == 200
    assert response.text == "reached"


async def test_the_health_endpoint_needs_no_token(http):
    assert (await http.get("/healthz")).text == "reached"


async def test_startup_and_shutdown_events_pass_straight_through():
    seen = []

    async def record(scope, receive, send):
        seen.append(scope["type"])

    await BearerTokenMiddleware(record, token="t")({"type": "lifespan"}, None, None)

    assert seen == ["lifespan"]


async def test_the_assembled_app_serves_health_without_a_token_and_guards_mcp():
    app = build_app(SETTINGS)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost") as client:
        health = await client.get("/healthz")
        unauthenticated = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert (health.status_code, health.text) == (200, "ok")
    assert unauthenticated.status_code == 401
