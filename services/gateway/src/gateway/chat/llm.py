"""The chat agent's model, reached through an OpenAI-compatible chat completions API.

Gemini, Groq, OpenRouter, Ollama and OpenAI itself all offer this request
shape, so the provider is three settings (base URL, model, API key) and no
code. It is called over plain HTTP rather than through a vendor SDK for the
same reason, and because the whole exchange is then visible in this one file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import types

logger = logging.getLogger(__name__)

# JSON Schema keywords that every provider's tool format accepts. MCP tools may
# use the whole of JSON Schema; anything outside this list is dropped rather
# than risk the provider rejecting every request because of one tool.
_KEPT_KEYWORDS = {
    "type", "description", "enum", "properties", "required", "items", "anyOf",
    "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength",
}


class LLMError(Exception):
    """The model could not be reached or refused the request. The message is fit to show to the user."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The message exactly as the provider sent it. It is appended to the
    # conversation unchanged, because some providers attach fields to a tool
    # call that they need to see again on the next request.
    message: dict[str, Any] = field(default_factory=dict)


def simplify_schema(schema: Any, definitions: dict[str, Any] | None = None) -> Any:
    """Reduces a JSON Schema to the widely supported subset, inlining any $ref."""
    if isinstance(schema, list):
        return [simplify_schema(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if definitions is None:
        definitions = schema.get("$defs") or schema.get("definitions") or {}
    if "$ref" in schema:
        target = definitions.get(schema["$ref"].rsplit("/", 1)[-1], {})
        return simplify_schema({**target, **{k: v for k, v in schema.items() if k != "$ref"}}, definitions)

    simple: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword not in _KEPT_KEYWORDS:
            continue
        if keyword == "properties":
            simple[keyword] = {name: simplify_schema(sub, definitions) for name, sub in value.items()}
        elif keyword in ("items", "anyOf"):
            simple[keyword] = simplify_schema(value, definitions)
        else:
            simple[keyword] = value
    return simple


def tool_definition(tool: types.Tool) -> dict[str, Any]:
    """An MCP tool in the chat completions "function" format."""
    function: dict[str, Any] = {"name": tool.name, "description": (tool.description or tool.name)[:1000]}
    parameters = simplify_schema(tool.input_schema or {})
    # A function with no arguments is declared without a parameters object;
    # some providers reject an object schema that has no properties.
    if parameters.get("properties"):
        parameters.setdefault("type", "object")
        function["parameters"] = parameters
    return {"type": "function", "function": function}


class ChatModel:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 90.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        """Sends the conversation and returns the model's next turn: text, tool calls, or both."""
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            body |= {"tools": tools, "tool_choice": "auto"}
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx2.TimeoutException as error:
            raise LLMError("The model took too long to answer.") from error
        except httpx2.RequestError as error:
            raise LLMError(f"The model's API could not be reached ({type(error).__name__}).") from error

        if response.status_code != 200:
            raise LLMError(_explain(response))
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError("The model's API returned an answer in an unexpected shape.") from error

        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            calls.append(
                ToolCall(
                    id=call.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        text = message.get("content") or ""
        if isinstance(text, list):  # some providers return content as a list of parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return AssistantTurn(text=text, tool_calls=calls, message=message)


def _explain(response: httpx2.Response) -> str:
    try:
        payload = response.json()
        # Providers nest the message differently; take the first one found.
        error = payload[0] if isinstance(payload, list) else payload
        detail = (error.get("error") or {}).get("message") or error.get("message") or ""
    except (ValueError, AttributeError, IndexError):
        detail = ""
    detail = detail.strip().split("\n")[0][:300]
    status = response.status_code
    if status == 429:
        return f"The model's API is rate limiting this key (HTTP 429). Wait a minute and try again. {detail}".strip()
    if status in (401, 403):
        return f"The model's API rejected the API key (HTTP {status}). {detail}".strip()
    if status == 404:
        return f"The model's API does not know this model or URL (HTTP 404). {detail}".strip()
    return f"The model's API returned an error (HTTP {status}). {detail}".strip()
