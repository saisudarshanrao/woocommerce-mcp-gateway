"""Signing in and out, and accepting an invitation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from gateway import repo, security
from gateway.api.deps import SESSION_COOKIE, check_origin, current_user, parts, start_session, user_json
from gateway.repo import User

router = APIRouter(prefix="/api", tags=["auth"])

# Verified when the email is unknown, so that answering takes as long as it
# does for a real account and the timing does not reveal which emails exist.
_DUMMY_HASH = security.hash_password("no such account")


class Login(BaseModel):
    email: str
    password: str


class AcceptInvite(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=10, max_length=200)


@router.post("/auth/login")
async def login(body: Login, request: Request, response: Response) -> dict:
    check_origin(request)
    found = await repo.get_user_with_password(parts(request).pool, body.email.strip())
    user, password_hash = found if found else (None, _DUMMY_HASH)
    if not security.verify_password(password_hash, body.password) or user is None or not user.active:
        raise HTTPException(401, "Wrong email or password.")
    await start_session(request, response, user)
    return user_json(user)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    check_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await repo.delete_session(parts(request).pool, security.hash_token(token))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me")
async def me(user: User = Depends(current_user)) -> dict:
    return user_json(user)


@router.get("/invites/{token}")
async def show_invite(token: str, request: Request) -> dict:
    invite = await repo.get_open_invite(parts(request).pool, security.hash_token(token))
    if invite is None:
        raise HTTPException(404, "This invitation is not valid. It may have expired or been used.")
    return {"email": invite.email, "role": invite.role}


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, body: AcceptInvite, request: Request, response: Response) -> dict:
    check_origin(request)
    pool = parts(request).pool
    invite = await repo.get_open_invite(pool, security.hash_token(token))
    if invite is None:
        raise HTTPException(404, "This invitation is not valid. It may have expired or been used.")
    if await repo.get_user_with_password(pool, invite.email):
        raise HTTPException(409, "An account with this email already exists.")
    user = await repo.accept_invite(pool, invite.id, body.name.strip(), security.hash_password(body.password))
    if user is None:
        raise HTTPException(404, "This invitation is not valid. It may have expired or been used.")
    await start_session(request, response, user)
    return user_json(user)
