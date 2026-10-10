"""What the gateway does with a tool request, whoever is asking.

The MCP endpoint and the chat agent both come through here, so an external
client and the dashboard's chat see exactly the same tools, are held to exactly
the same rules, and leave the same audit trail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from mcp import types

from gateway import naming, repo
from gateway.metrics import Metrics
from gateway.repo import Server
from gateway.upstreams import ExposedTool, UpstreamError, UpstreamManager, UpstreamState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Who a request is acting as, and how it arrived."""

    user_id: UUID
    email: str
    role: str
    source: str  # "mcp" for an API key, "chat" for the dashboard's chat
    api_key_id: UUID | None = None
    api_key_prefix: str | None = None


def error_result(message: str) -> types.CallToolResult:
    """A failed tool call. It is still a result, so the model reads the reason."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


def may_use(principal: Principal, server: Server, tool: ExposedTool) -> bool:
    """Admins may use every tool. Members get what the server's setting allows."""
    if principal.role == "admin" or server.member_access == "all":
        return True
    return server.member_access == "read_only" and tool.read_only


class Gateway:
    def __init__(
        self,
        pool: asyncpg.Pool,
        upstreams: UpstreamManager,
        *,
        calls_per_minute: int = 0,
        metrics: Metrics | None = None,
    ):
        self._pool = pool
        self._upstreams = upstreams
        # The most tool calls one API key, or one person in the chat, may make in a minute. 0 is no limit.
        self._calls_per_minute = calls_per_minute
        self._metrics = metrics or Metrics()

    async def list_tools(self, principal: Principal) -> list[types.Tool]:
        """Every tool this user may call right now, across all servers."""
        # The registry is read from the database on every request, so disabling
        # or removing a server takes effect at once. Tool details come from the
        # last probe, which is why this never waits on an upstream.
        enabled = [server for server in await repo.list_servers(self._pool) if server.enabled]
        states = await asyncio.gather(*(self._current_state(server) for server in enabled))
        tools: list[types.Tool] = []
        for server, state in zip(enabled, states):
            if state is None or state.status != "connected":
                continue
            tools.extend(tool.tool for tool in state.tools.values() if may_use(principal, server, tool))
        return sorted(tools, key=lambda tool: tool.name)

    async def _current_state(self, server: Server) -> UpstreamState | None:
        """What this process knows about a server in its present form.

        Normally that is whatever the last background probe left. But a server
        may have been added or edited through another gateway replica moments
        ago, in which case this replica has never seen it, or has only seen its
        old settings. Then it is probed now, once, so that every replica gives
        the same answer straight away instead of after its next probe round.
        """
        state = self._upstreams.state(server.id)
        if state is None or state.server.updated_at != server.updated_at:
            state = await self._upstreams.refresh(server.id)
        return state

    async def call_tool(self, principal: Principal, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Routes one call to its server. Always returns a result and always writes an audit row."""
        started = time.perf_counter()
        slug: str | None = None
        tool_name: str | None = None
        # The server as the metrics label it: only ever a registered server, so
        # that made-up names in requests cannot create endless label values.
        known_server = "-"
        outcome, error = "error", None
        try:
            if self._calls_per_minute:
                # An API key is limited by itself; the chat is limited per person.
                subject = f"key:{principal.api_key_id}" if principal.api_key_id else f"user:{principal.user_id}"
                if await repo.count_call(self._pool, subject) > self._calls_per_minute:
                    wait = 60 - datetime.now(UTC).second
                    outcome = "limited"
                    error = (
                        f"Rate limit reached: at most {self._calls_per_minute} tool calls a minute. "
                        f"Try again in {wait} seconds."
                    )
                    return error_result(error)

            parts = naming.split(name)
            if parts is None:
                error = f"There is no tool called '{name}'. Tool names look like <server>__<tool>."
                return error_result(error)
            slug, tool_name = parts

            server = await repo.get_server_by_slug(self._pool, slug)
            if server is None:
                error = f"There is no server '{slug}'."
                return error_result(error)
            known_server = server.slug
            if not server.enabled:
                outcome, error = "denied", f"The server '{server.name}' is disabled."
                return error_result(error)

            state = await self._current_state(server)
            if state is None or state.status == "pending":
                outcome, error = "unreachable", f"{server.name} has not been checked yet. Try again in a moment."
                return error_result(error)
            if state.status != "connected":
                outcome, error = "unreachable", f"{server.name} is unreachable: {state.error}."
                return error_result(error)

            tool = state.tools.get(tool_name)
            if tool is None:
                error = f"{server.name} has no tool called '{tool_name}'."
                return error_result(error)
            if not may_use(principal, server, tool):
                outcome, error = "denied", f"Your role is not allowed to use '{name}'."
                return error_result(error)

            try:
                result = await self._upstreams.call(state, tool_name, arguments)
            except UpstreamError as failure:
                outcome, error = failure.outcome, failure.message
                return error_result(error)

            if result.is_error:
                # The upstream ran the tool and the tool reported a failure.
                error = " ".join(block.text for block in result.content if isinstance(block, types.TextContent))
            else:
                outcome = "ok"
            return result
        finally:
            self._metrics.observe_call(known_server, outcome, principal.source, time.perf_counter() - started)
            await self._audit(principal, slug, tool_name or name, started, outcome, error)

    async def _audit(
        self, principal: Principal, slug: str | None, tool: str | None, started: float, outcome: str, error: str | None
    ) -> None:
        try:
            await repo.add_audit(
                self._pool,
                user_id=principal.user_id,
                user_email=principal.email,
                api_key_id=principal.api_key_id,
                api_key_prefix=principal.api_key_prefix,
                source=principal.source,
                server_slug=slug,
                tool=tool,
                duration_ms=int((time.perf_counter() - started) * 1000),
                outcome=outcome,
                error=error,
            )
        except Exception:
            # The caller still gets their answer, but a missing audit row is a
            # fault worth shouting about.
            logger.exception("could not write the audit log entry for %s by %s", tool, principal.email)
