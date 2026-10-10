"""The chat agent: a model, the gateway's tools, and the loop between them.

The loop is the whole of it:

    ask the model, giving it the conversation and the tools this user may call
    if it answers in words, that is the reply, and the loop ends
    if it asks for tools, run each one through the gateway and add the results
    ask again

Tools are listed and called through the same Gateway object that serves the
MCP endpoint, as the signed-in user. So the chat sees exactly the tools an
external client with that user's key would see, a disabled server's tools are
gone here too, and every call lands in the audit log under the user's name.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from mcp import types

from gateway.chat.llm import ChatModel, LLMError, tool_definition
from gateway.core import Gateway, Principal

logger = logging.getLogger(__name__)

# One reply may take several rounds of tool calls. This bounds a model that
# keeps calling tools without ever answering.
MAX_ROUNDS = 8
# How much of one tool result the model is given. A result larger than this is
# cut off, and the model is told so.
MAX_RESULT_CHARS = 20_000

SYSTEM_PROMPT = """\
You are the assistant in a team's operations dashboard. You answer by calling tools.

The tools come from several servers. Each tool is named <server>__<tool> and its description \
starts with the server's name in brackets. Use them to find facts; never invent data such as \
orders, products or prices. If no tool can answer the question, say so.

When a question is about today's or recent orders, pass the tool a named period rather than \
working out dates yourself. If a tool call fails, tell the user plainly what failed and why.

Keep answers short and concrete. Give amounts with their currency, and IDs where they help.
The current time is {now} UTC.
"""


def result_text(result: types.CallToolResult) -> str:
    """What a tool returned, as the text handed back to the model."""
    text = "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[result cut off: it was longer than the gateway passes to the model]"
    return text or "(the tool returned nothing)"


async def run_chat(
    gateway: Gateway, model: ChatModel, principal: Principal, history: list[dict[str, str]]
) -> AsyncIterator[dict[str, Any]]:
    """Runs one reply and yields what happens as it happens.

    Events: {"type": "tool_call"}, {"type": "tool_result"}, {"type": "text"},
    {"type": "error"}, and finally {"type": "done"}.
    """
    tools = await gateway.list_tools(principal)
    definitions = [tool_definition(tool) for tool in tools]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"))},
        *history,
    ]

    try:
        for _ in range(MAX_ROUNDS):
            turn = await model.complete(messages, definitions)
            if turn.text:
                yield {"type": "text", "text": turn.text}
            if not turn.tool_calls:
                break

            messages.append(turn.message)
            for call in turn.tool_calls:
                yield {"type": "tool_call", "id": call.id, "name": call.name, "arguments": call.arguments}
                started = time.perf_counter()
                # The gateway never raises here: a failure comes back as a
                # result the model can read, and is audited either way.
                result = await gateway.call_tool(principal, call.name, call.arguments)
                text = result_text(result)
                yield {
                    "type": "tool_result",
                    "id": call.id,
                    "name": call.name,
                    "ok": not result.is_error,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "preview": text[:400],
                }
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        else:
            yield {
                "type": "text",
                "text": f"I stopped after {MAX_ROUNDS} rounds of tool calls without reaching an answer.",
            }
    except LLMError as error:
        logger.warning("chat for %s failed: %s", principal.email, error)
        yield {"type": "error", "message": str(error)}
    yield {"type": "done"}
