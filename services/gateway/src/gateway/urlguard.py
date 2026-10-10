"""Which addresses the gateway is willing to connect to as an MCP client.

An admin registers a server by typing a URL, and the gateway then sends
requests to it from inside the cluster. Without a check, that turns the
gateway into a way of reaching things only the cluster can reach: the cloud
provider's metadata service, the Kubernetes API, the gateway's own database,
another team's services. This is server-side request forgery.

The rule, applied to the address a host name actually resolves to:

  * loopback, link-local (where metadata services live) and other special
    ranges are never allowed;
  * a private address is allowed only when the host name is on the operator's
    list of in-cluster MCP servers;
  * a public address is allowed if the deployment allows public servers.

It is applied twice. When a server is added or edited, so the admin gets the
reason straight away; and again on every connection, because what a name
resolves to can change after it was checked. The connection is then made to
the very address that passed, not to the name.

This is the application's layer. The chart's NetworkPolicy on the gateway's
outbound traffic enforces the same boundary in the network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from fnmatch import fnmatchcase

import httpx2

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedAddress(Exception):
    """The gateway refuses to connect to this address. `reason` completes "…because <reason>"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address the host name resolves to. Empty when it does not resolve."""
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(info[4][0] for info in found))


def never_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is off limits whatever its name, or None."""
    if address.is_loopback:
        return "a loopback address, which is the gateway itself"
    if address.is_link_local:
        return "a link-local address, where cloud metadata services live"
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return "a special-purpose address"
    return None


class UrlGuard:
    def __init__(
        self,
        internal_hosts: tuple[str, ...] = (),
        allow_public: bool = True,
        resolver: Resolver = system_resolver,
    ):
        # Patterns such as "*.mcp-servers.svc.cluster.local".
        self._internal_hosts = tuple(pattern.lower() for pattern in internal_hosts)
        self._allow_public = allow_public
        self._resolver = resolver

    def is_internal_host(self, host: str) -> bool:
        return any(fnmatchcase(host, pattern) for pattern in self._internal_hosts)

    async def check(self, url: str) -> str | None:
        """Returns the address to connect to for this URL, or None if its host does not resolve.

        Raises BlockedAddress if any address the host resolves to is not allowed.
        """
        parsed = httpx2.URL(url)
        host = parsed.host.lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            # A host that is already an address needs no lookup.
            addresses = [str(ipaddress.ip_address(host))]
            named = False
        except ValueError:
            addresses = await self._resolver(host, port)
            named = True

        listed = named and self.is_internal_host(host)
        for text in addresses:
            address = ipaddress.ip_address(text)
            # ::ffff:10.0.0.1 is the IPv4 address 10.0.0.1 in IPv6 clothing.
            address = getattr(address, "ipv4_mapped", None) or address
            special = never_allowed(address)
            if special:
                raise BlockedAddress(f"{host} is {special}")
            if address.is_global:
                if not (self._allow_public or listed):
                    raise BlockedAddress(f"{host} is on the public internet, and this gateway only allows listed servers")
            elif not listed:
                # Private ranges: the cluster's own pods and Services, and the node's network.
                raise BlockedAddress(
                    f"{host} is a private address ({address}) and is not on the list of in-cluster MCP servers"
                )
        return addresses[0] if addresses else None


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Sends each request only if its destination passes the guard, and to exactly that address."""

    def __init__(self, guard: UrlGuard, inner: httpx2.AsyncBaseTransport | None = None):
        self._guard = guard
        self._inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        address = await self._guard.check(str(request.url))
        if address is None:
            raise httpx2.ConnectError(f"name resolution failed for {host}", request=request)
        # Connect to the address that was just checked. Left to itself the HTTP
        # client would look the name up a second time, and could be given a
        # different answer (DNS rebinding). The Host header was filled in from
        # the name when the request was built; TLS is told the name here, so the
        # certificate is still verified against it.
        request.url = request.url.copy_with(host=address)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
