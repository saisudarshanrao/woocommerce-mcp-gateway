"""The gateway as an MCP client: one entry per registered upstream server.

For each server this module keeps what a recent probe found (is it reachable,
which tools does it have) and knows how to call one of its tools. Two rules
shape it:

  * Nothing a client asks for waits on a probe. Probes run in the background,
    every server on its own, and listing tools only reads what they left
    behind. A dead or slow server therefore cannot hold up the others.
  * No connection outlives a request. Each probe and each call opens its own
    short connection, so there is no session to repair when an upstream pod is
    replaced and no state that ties a client to one gateway replica.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any
from uuid import UUID

import asyncpg
import httpx2
from mcp import Client, MCPError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError

from gateway import naming, repo
from gateway.repo import Server
from gateway.security import CredentialCipher

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0

HttpClientFactory = Callable[[Server, dict[str, str], float], httpx2.AsyncClient]


class UpstreamError(Exception):
    """A call to an upstream failed. `outcome` is the audit log's word for how."""

    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome
        self.message = message


@dataclass(frozen=True)
class ExposedTool:
    upstream_name: str
    # The tool as the gateway presents it: prefixed name, labelled description.
    tool: types.Tool
    read_only: bool


@dataclass
class UpstreamState:
    """What the gateway currently knows about one upstream."""

    server: Server
    status: str = "pending"  # pending | connected | unreachable | disabled
    error: str | None = None
    checked_at: datetime | None = None
    latency_ms: int | None = None
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    discover: types.DiscoverResult | None = None
    tools: dict[str, ExposedTool] = field(default_factory=dict)
    # Tools that cannot be exposed under the naming scheme, with the reason.
    skipped: list[dict[str, str]] = field(default_factory=list)


def default_http_client(server: Server, headers: dict[str, str], timeout: float) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)),
    )


def _leaves(error: BaseException) -> list[BaseException]:
    """The underlying errors inside the exception groups the MCP client raises."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


def explain(error: BaseException, statuses: list[int]) -> tuple[bool, str]:
    """Turns a failure into (is it a connectivity problem, a sentence an admin can act on)."""
    leaves = _leaves(error)
    status = statuses[-1] if statuses else None

    for leaf in leaves:
        if isinstance(leaf, httpx2.ConnectError):
            text = str(leaf).lower()
            if any(hint in text for hint in ("name or service", "nodename", "name resolution", "no address")):
                return True, "its host name does not resolve"
            return True, "the connection was refused (is the server running?)"
        if isinstance(leaf, httpx2.TimeoutException):
            return True, "it did not answer in time"
        if isinstance(leaf, httpx2.RequestError):
            return True, f"the connection failed ({type(leaf).__name__})"

    if status in (401, 403):
        return True, f"it rejected the gateway's credential (HTTP {status})"
    if status == 404:
        return True, "there is no MCP endpoint at that URL (HTTP 404)"
    if status is not None and status >= 500:
        return True, f"it answered with a server error (HTTP {status})"

    for leaf in leaves:
        if isinstance(leaf, MCPError):
            return False, f"it answered with an MCP error: {leaf}"
        if isinstance(leaf, ValidationError):
            return False, "its answer does not follow the MCP protocol"
    return False, f"{type(leaves[0]).__name__}: {leaves[0]}"


class UpstreamManager:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: CredentialCipher,
        *,
        probe_interval_seconds: float = 10.0,
        probe_timeout_seconds: float = 5.0,
        http_client_factory: HttpClientFactory = default_http_client,
    ):
        self._pool = pool
        self._cipher = cipher
        self._probe_interval = probe_interval_seconds
        self._probe_timeout = probe_timeout_seconds
        # Tests replace this to reach in-process fake servers.
        self._http_client_factory = http_client_factory
        self._states: dict[UUID, UpstreamState] = {}
        self._task: asyncio.Task[None] | None = None
        self._client_info = types.Implementation(name="mcp-gateway", version=version("gateway"))

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Probes every server once, then keeps doing so in the background."""
        await self.sync()
        self._task = asyncio.create_task(self._run(), name="upstream-probes")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._probe_interval)
            try:
                await self.sync()
            except Exception:
                # For example the database is briefly unavailable. Keep what we
                # know and try again on the next round.
                logger.exception("probe round failed")

    async def sync(self) -> None:
        """Re-reads the registry and probes every enabled server, all at once."""
        servers = {server.id: server for server in await repo.list_servers(self._pool)}
        for server_id in list(self._states):
            if server_id not in servers:
                del self._states[server_id]
        for server in servers.values():
            known = self._states.get(server.id)
            if known is None or known.server.updated_at != server.updated_at:
                # New, or edited since the last look: forget what was known.
                self._states[server.id] = UpstreamState(server=server)
        # gather, not a loop: one server's five-second timeout must not delay the rest.
        await asyncio.gather(*(self._probe(state) for state in list(self._states.values())))

    async def refresh(self, server_id: UUID) -> UpstreamState | None:
        """Re-reads one server and probes it now, for when an admin has just changed it."""
        server = await repo.get_server(self._pool, server_id)
        if server is None:
            self._states.pop(server_id, None)
            return None
        state = self._states[server_id] = UpstreamState(server=server)
        await self._probe(state)
        return state

    def states(self) -> list[UpstreamState]:
        return list(self._states.values())

    def state(self, server_id: UUID) -> UpstreamState | None:
        return self._states.get(server_id)

    # --- talking to an upstream ----------------------------------------------

    def _headers(self, server: Server) -> dict[str, str]:
        """The credential, decrypted only here, at the moment it is sent upstream."""
        if server.auth_type == "none" or not server.auth_secret:
            return {}
        secret = self._cipher.decrypt(server.auth_secret)
        if server.auth_type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        return {server.auth_header or "X-API-Key": secret}

    @asynccontextmanager
    async def _connect(
        self,
        server: Server,
        mode: str,
        prior_discover: types.DiscoverResult | None,
        timeout: float,
        statuses: list[int],
    ) -> AsyncIterator[Client]:
        http = self._http_client_factory(server, self._headers(server), timeout)

        async def record_status(response: httpx2.Response) -> None:
            # The MCP client reports an HTTP failure without its status code,
            # and the code is what tells a wrong credential from a wrong URL.
            statuses.append(response.status_code)

        http.event_hooks["response"].append(record_status)
        async with http:
            client = Client(
                streamable_http_client(server.url, http_client=http),
                mode=mode,
                prior_discover=prior_discover,
                client_info=self._client_info,
                read_timeout_seconds=timeout,
                cache=None,
            )
            async with client:
                yield client

    async def _probe(self, state: UpstreamState) -> None:
        server = state.server
        if not server.enabled:
            state.status, state.error, state.tools, state.skipped = "disabled", None, {}, []
            return

        statuses: list[int] = []
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._probe_timeout):
                # "auto" asks the server which protocol generation it speaks.
                async with self._connect(server, "auto", None, self._probe_timeout, statuses) as client:
                    listed: list[types.Tool] = []
                    cursor: str | None = None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        listed.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    protocol = client.protocol_version
                    info = client.server_info
                    discover = client.session.discover_result
        except TimeoutError:
            self._mark_unreachable(state, f"it did not answer within {self._probe_timeout:g} seconds")
            return
        except Exception as error:
            self._mark_unreachable(state, explain(error, statuses)[1])
            return

        tools: dict[str, ExposedTool] = {}
        skipped: list[dict[str, str]] = []
        for tool in listed:
            reason = naming.why_not_exposable(server.slug, tool.name)
            if reason:
                skipped.append({"name": tool.name, "reason": reason})
                continue
            exposed = tool.model_copy(
                update={
                    "name": naming.exposed_name(server.slug, tool.name),
                    # Lead with the server's name so a model can tell apart two
                    # servers' tools that do similar things.
                    "description": f"[{server.name}] {tool.description or ''}".strip(),
                }
            )
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            tools[tool.name] = ExposedTool(upstream_name=tool.name, tool=exposed, read_only=read_only)

        if state.status != "connected":
            logger.info("upstream %s is connected (%d tools, protocol %s)", server.slug, len(tools), protocol)
        state.status, state.error = "connected", None
        state.checked_at = datetime.now(UTC)
        state.latency_ms = int((time.perf_counter() - started) * 1000)
        state.protocol_version = protocol
        state.server_name = info.name if info else None
        state.server_version = info.version if info else None
        state.discover = discover
        state.tools, state.skipped = tools, skipped

    def _mark_unreachable(self, state: UpstreamState, reason: str) -> None:
        if state.status != "unreachable" or state.error != reason:
            logger.warning("upstream %s is unreachable: %s", state.server.slug, reason)
        state.status, state.error = "unreachable", reason
        state.checked_at = datetime.now(UTC)
        state.latency_ms = None
        state.tools, state.skipped = {}, []

    async def call(self, state: UpstreamState, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Calls one tool, bounded by the server's timeout. Raises UpstreamError on failure."""
        server = state.server
        timeout = float(server.timeout_seconds)
        # A server that speaks the current protocol needs no handshake, and the
        # probe's discovery result is reused, so the call is the only request
        # that must succeed. An older server gets a fresh handshake per call.
        mode = state.protocol_version if state.protocol_version in MODERN_PROTOCOL_VERSIONS else "legacy"
        statuses: list[int] = []
        try:
            async with asyncio.timeout(timeout):
                async with self._connect(server, mode, state.discover, timeout, statuses) as client:
                    return await client.call_tool(tool, arguments)
        except TimeoutError as error:
            raise UpstreamError("timeout", f"{server.name} did not answer within {timeout:g} seconds.") from error
        except Exception as error:
            connectivity, reason = explain(error, statuses)
            if connectivity:
                # Say so at once rather than waiting for the next probe.
                self._mark_unreachable(state, reason)
                raise UpstreamError("unreachable", f"{server.name} is unreachable: {reason}.") from error
            raise UpstreamError("error", f"{server.name} could not run the tool: {reason}.") from error
