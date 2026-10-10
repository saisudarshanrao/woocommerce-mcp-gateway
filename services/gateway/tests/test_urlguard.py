"""The guard on where the gateway may connect: which addresses pass, and that connections are pinned."""

import httpx2
import pytest

from gateway.upstreams import explain
from gateway.urlguard import BlockedAddress, GuardedTransport, UrlGuard

pytestmark = pytest.mark.anyio

# What each name resolves to in these tests. Anything else does not resolve.
DNS = {
    "tasks-mcp.mcp-servers.svc.cluster.local": ["10.42.0.17"],
    "store-wordpress.store-main.svc.cluster.local": ["10.43.80.2"],
    "mcp.example.com": ["93.184.216.34"],
    "rebind.example.com": ["93.184.216.34", "10.42.0.17"],
    "metadata.example.com": ["169.254.169.254"],
    "2130706433": ["127.0.0.1"],
}


async def fake_dns(host: str, port: int) -> list[str]:
    return DNS.get(host, [])


def guard(**options) -> UrlGuard:
    return UrlGuard(internal_hosts=("*.mcp-servers.svc.cluster.local",), resolver=fake_dns, **options)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://tasks-mcp.mcp-servers.svc.cluster.local/mcp", "10.42.0.17"),
        ("http://TASKS-MCP.mcp-servers.svc.cluster.local./mcp", "10.42.0.17"),
        ("https://mcp.example.com/mcp", "93.184.216.34"),
        ("https://93.184.216.34/mcp", "93.184.216.34"),
        ("http://not-registered-anywhere.example/mcp", None),
    ],
)
async def test_allowed_destinations(url, expected):
    assert await guard().check(url) == expected


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://metadata.example.com/", "link-local"),
        ("http://127.0.0.1:5432/", "loopback"),
        ("http://[::1]/mcp", "loopback"),
        ("http://[::ffff:127.0.0.1]/mcp", "loopback"),
        ("http://2130706433/", "loopback"),
        ("http://0.0.0.0/", "special-purpose"),
        # The Kubernetes API, and a Service that is not an MCP server.
        ("https://10.43.0.1/", "private address"),
        ("http://store-wordpress.store-main.svc.cluster.local/", "private address"),
        # One public answer does not excuse a private one.
        ("http://rebind.example.com/mcp", "private address"),
    ],
)
async def test_blocked_destinations(url, reason):
    with pytest.raises(BlockedAddress) as blocked:
        await guard().check(url)
    assert reason in blocked.value.reason


async def test_public_servers_can_be_switched_off():
    listed_only = guard(allow_public=False)
    with pytest.raises(BlockedAddress, match="public internet"):
        await listed_only.check("https://mcp.example.com/mcp")
    assert await listed_only.check("http://tasks-mcp.mcp-servers.svc.cluster.local/mcp") == "10.42.0.17"


def recording_transport(seen: list[httpx2.Request]) -> GuardedTransport:
    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text="ok")

    return GuardedTransport(guard(), inner=httpx2.MockTransport(answer))


async def test_a_request_goes_to_the_address_that_was_checked():
    seen: list[httpx2.Request] = []
    async with httpx2.AsyncClient(transport=recording_transport(seen)) as client:
        await client.post("https://mcp.example.com/mcp")
    (request,) = seen
    # Connected by address, while HTTP and TLS still name the host.
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "mcp.example.com"
    assert request.extensions["sni_hostname"] == "mcp.example.com"


async def test_a_blocked_request_is_never_sent():
    seen: list[httpx2.Request] = []
    async with httpx2.AsyncClient(transport=recording_transport(seen)) as client:
        with pytest.raises(BlockedAddress):
            await client.get("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(httpx2.ConnectError):
            await client.get("http://not-registered-anywhere.example/mcp")
    assert seen == []


def test_a_blocked_address_is_reported_as_unreachable_with_the_reason():
    kind, sentence = explain(BlockedAddress("10.43.0.1 is a private address"), [])
    assert kind == "unreachable"
    assert sentence == "the gateway will not connect there: 10.43.0.1 is a private address"
