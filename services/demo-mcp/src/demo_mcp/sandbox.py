"""A server that misbehaves on request.

Its tools exist to show how the gateway copes: `slow` runs past the gateway's
timeout, `fail` returns an error, and `server_info` has the same name as a tool
on the tasks server, which is what the gateway's naming scheme is for.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

READ_ONLY = ToolAnnotations(read_only_hint=True)


def build() -> MCPServer:
    mcp = MCPServer("sandbox", title="Sandbox", instructions="Tools for testing how a client handles slow and failing calls.")

    @mcp.tool(title="Echo", annotations=READ_ONLY)
    def echo(text: Annotated[str, Field(description="Anything.")]) -> str:
        """Return the text it was given."""
        return text

    @mcp.tool(title="Slow", annotations=READ_ONLY)
    async def slow(
        seconds: Annotated[float, Field(ge=0, le=600, description="How long to take before answering.")],
    ) -> str:
        """Wait, then answer. Use it to see what a timeout looks like."""
        await asyncio.sleep(seconds)
        return f"answered after {seconds:g} seconds"

    @mcp.tool(title="Fail", annotations=READ_ONLY)
    def fail(message: Annotated[str, Field(description="The error to report.")] = "this tool always fails") -> str:
        """Always report an error. Use it to see how a failed call is shown."""
        raise ToolError(message)

    @mcp.tool(title="Server info", annotations=READ_ONLY)
    def server_info() -> dict[str, str]:
        """Say which server this is."""
        return {"server": "sandbox"}

    return mcp
