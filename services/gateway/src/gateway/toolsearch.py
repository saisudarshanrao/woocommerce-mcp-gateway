"""Finding a tool among many, instead of listing them all.

Every registered server adds its tools to the gateway's list, and ten servers
can mean hundreds of tools. Handing a model every definition on every request
costs tokens, and past a few dozen tools a model also chooses worse. So the
gateway can present just two tools in place of the whole list:

    find_tools(query)            the few tools that match, with their arguments
    call_tool(name, arguments)   runs one of them

Both go through the same Gateway object as the full list does. A user finds
only the tools they would have been listed, and a call made this way is checked,
rate-limited and audited exactly like a direct one, under the real tool's name.

Matching is by words, not by meaning: the query and each tool's name and
description are split into words and compared. That is easy to predict and to
explain, needs no second model, and works well here because tool names are
already descriptive (store-books__list_orders).
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp import types

from gateway import naming
from gateway.core import Gateway, Principal, error_result

FIND = "find_tools"
CALL = "call_tool"
DEFAULT_LIMIT = 8
MAX_LIMIT = 20

# Words that say nothing about which tool is wanted.
_FILLER = {"a", "an", "the", "of", "for", "in", "on", "to", "and", "or", "is", "are", "me", "my", "all", "what", "which"}

META_TOOLS = [
    types.Tool(
        name=FIND,
        title="Find tools",
        description=(
            "Searches the tools of every connected server and returns the best matches, each with its name, "
            "what it does and the arguments it takes. Search with a few plain words: the thing you want to act on "
            "and, if you know it, the server or store (for example 'orders today books store'). "
            "Call this before call_tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words describing the tool you need."},
                "limit": {"type": "integer", "description": f"How many matches to return (default {DEFAULT_LIMIT})."},
            },
            "required": ["query"],
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    types.Tool(
        name=CALL,
        title="Call a tool",
        description=(
            "Runs one tool found with find_tools. Give its exact name, and its arguments as an object that "
            "follows the input schema find_tools returned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The tool's full name, for example woo__list_orders."},
                "arguments": {"type": "object", "description": "The tool's arguments."},
            },
            "required": ["name"],
        },
    ),
]


def words(text: str) -> set[str]:
    """The words of a text, lower-cased, with a plural "s" dropped so that order matches orders."""
    found = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _FILLER:
            continue
        found.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return found


def rank(tools: list[types.Tool], query: str, limit: int = DEFAULT_LIMIT) -> list[types.Tool]:
    """The tools that share words with the query, best first.

    A word found in a tool's name counts three times as much as one found only
    in its description: the name holds the server and the action, which is
    what a search usually names.
    """
    wanted = words(query)
    scored = []
    for tool in tools:
        in_name = words(tool.name)
        in_text = words(f"{tool.title or ''} {tool.description or ''}")
        score = sum(3 if word in in_name else 1 for word in wanted if word in in_name or word in in_text)
        if score:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tool for _, _, tool in scored[:limit]]


class ToolSearch:
    def __init__(self, gateway: Gateway):
        self._gateway = gateway

    async def call(self, principal: Principal, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name == FIND:
            return await self._find(principal, arguments)
        if name == CALL:
            target = arguments.get("name")
            inner = arguments.get("arguments") or {}
            if not isinstance(target, str) or not isinstance(inner, dict):
                return error_result("call_tool needs 'name' (a string) and 'arguments' (an object).")
            return await self._gateway.call_tool(principal, target, inner)
        return error_result(f"This endpoint has two tools, {FIND} and {CALL}. Use {CALL} to run '{name}'.")

    async def _find(self, principal: Principal, arguments: dict[str, Any]) -> types.CallToolResult:
        query = str(arguments.get("query") or "")
        try:
            limit = max(1, min(int(arguments.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        available = await self._gateway.list_tools(principal)
        matches = rank(available, query, limit)

        found: dict[str, Any] = {
            "tools": [
                {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
                for tool in matches
            ]
        }
        if not matches:
            # Nothing matched. Say what there is to search, so the next query can do better.
            servers: dict[str, int] = {}
            for tool in available:
                slug = (naming.split(tool.name) or ("?", ""))[0]
                servers[slug] = servers.get(slug, 0) + 1
            found["servers"] = servers
            found["hint"] = "No tool matched. Try the name of a server above, or what you want to do (orders, products, tasks)."
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(found))], structured_content=found
        )
