"""The dashboard's chat: one request in, a stream of events out."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from gateway import repo
from gateway.api.deps import current_user, parts
from gateway.chat.agent import run_chat
from gateway.core import Principal
from gateway.repo import User

router = APIRouter(prefix="/api", tags=["chat"])


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    # The browser holds the conversation and sends it whole each time, so the
    # gateway keeps no chat state and any replica can answer.
    messages: list[Message] = Field(min_length=1, max_length=40)


@router.post("/chat")
async def chat(body: ChatRequest, request: Request, user: User = Depends(current_user)) -> StreamingResponse:
    app = parts(request)
    if app.chat_model is None:
        raise HTTPException(503, "The chat has no model configured. An admin needs to add the model's API key.")
    if body.messages[-1].role != "user":
        raise HTTPException(422, "The last message must be the user's.")
    limit = app.settings.chat_requests_per_minute
    if limit and await repo.count_call(app.pool, f"chat:{user.id}") > limit:
        raise HTTPException(429, f"You have sent {limit} messages in the last minute, which is the limit. Wait a moment.")

    # The agent acts as whoever is signed in: same tools, same rules, and its
    # calls are audited under this user with the source "chat".
    principal = Principal(user_id=user.id, email=user.email, role=user.role, source="chat")
    history = [message.model_dump() for message in body.messages]

    async def events() -> AsyncIterator[str]:
        # Server-sent events: one "data:" line of JSON per event.
        async for event in run_chat(
            app.gateway, app.chat_model, principal, history, search_above=app.settings.chat_tool_search_above
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
