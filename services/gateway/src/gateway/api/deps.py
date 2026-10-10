"""What the REST API's request handlers share: the app's parts, and who is signed in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import Depends, HTTPException, Request, Response

from gateway import repo, security
from gateway.chat.llm import ChatModel
from gateway.config import Settings
from gateway.core import Gateway
from gateway.oauth import OAuthStore
from gateway.repo import User
from gateway.security import CredentialCipher
from gateway.upstreams import UpstreamManager
from gateway.urlguard import UrlGuard

SESSION_COOKIE = "gateway_session"


@dataclass
class Parts:
    """The long-lived objects of the running gateway."""

    settings: Settings
    pool: asyncpg.Pool
    cipher: CredentialCipher
    upstreams: UpstreamManager
    gateway: Gateway
    url_guard: UrlGuard
    oauth: OAuthStore
    # None when no model API key is configured; the chat is then switched off.
    chat_model: ChatModel | None = None
    ready: bool = False


def parts(request: Request) -> Parts:
    return request.app.state.parts


def check_origin(request: Request) -> None:
    """Refuses a state-changing request that a different website caused.

    The session lives in a cookie, which a browser attaches to any request to
    this address, including one triggered by a page on another site. A browser
    also states, in the Origin header, which site the request came from, and
    that header cannot be set by page script. So a request that changes
    something is accepted only if its Origin is the gateway's own address.
    Requests with no Origin do not come from a browser page.
    """
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != parts(request).settings.public_url:
        raise HTTPException(403, "This request came from another site.")


async def current_user(request: Request) -> User:
    check_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    user = await repo.get_session_user(parts(request).pool, security.hash_token(token)) if token else None
    if user is None:
        raise HTTPException(401, "Sign in first.")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Only an admin can do this.")
    return user


async def start_session(request: Request, response: Response, user: User) -> None:
    app = parts(request)
    token = security.new_token()
    lifetime = timedelta(hours=app.settings.session_ttl_hours)
    await repo.create_session(app.pool, security.hash_token(token), user.id, datetime.now(UTC) + lifetime)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime.total_seconds()),
        httponly=True,  # page script cannot read it
        samesite="lax",  # not sent on requests another site makes in the background
        secure=app.settings.secure_cookies,
        path="/",
    )


def user_json(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "active": user.active,
        "created_at": user.created_at.isoformat(),
    }
