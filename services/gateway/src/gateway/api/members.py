"""Team management, for admins: members, invitations and everyone's API keys."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gateway import repo, security
from gateway.api.deps import admin_user, current_user, parts, user_json
from gateway.repo import ApiKey, User

router = APIRouter(prefix="/api", tags=["team"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NewInvite(BaseModel):
    email: str
    role: Literal["admin", "member"] = "member"


class MemberChange(BaseModel):
    role: Literal["admin", "member"] | None = None
    active: bool | None = None


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # Admins may issue a key on behalf of another member.
    user_id: UUID | None = None


def key_json(key: ApiKey) -> dict:
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "user_id": str(key.user_id),
        "user_email": key.user_email,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
    }


@router.get("/members")
async def list_members(request: Request, _: User = Depends(admin_user)) -> dict:
    pool = parts(request).pool
    invites = await repo.list_pending_invites(pool)
    return {
        "members": [user_json(user) for user in await repo.list_users(pool)],
        "invites": [
            {
                "id": str(invite.id),
                "email": invite.email,
                "role": invite.role,
                "created_at": invite.created_at.isoformat(),
                "expires_at": invite.expires_at.isoformat(),
            }
            for invite in invites
        ],
    }


@router.post("/invites", status_code=201)
async def create_invite(body: NewInvite, request: Request, admin: User = Depends(admin_user)) -> dict:
    app = parts(request)
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(422, "That does not look like an email address.")
    if await repo.get_user_with_password(app.pool, email):
        raise HTTPException(409, "Someone with this email is already a member.")
    token = security.new_token()
    expires_at = datetime.now(UTC) + timedelta(hours=app.settings.invite_ttl_hours)
    invite = await repo.create_invite(app.pool, security.hash_token(token), email, body.role, admin.id, expires_at)
    # The link is the only place the token ever appears. It is not stored, so
    # it cannot be shown again; a lost link means a new invitation.
    return {
        "id": str(invite.id),
        "email": invite.email,
        "role": invite.role,
        "expires_at": invite.expires_at.isoformat(),
        "link": f"{app.settings.public_url}/invite/{token}",
    }


@router.delete("/invites/{invite_id}", status_code=204)
async def cancel_invite(invite_id: UUID, request: Request, _: User = Depends(admin_user)) -> None:
    if not await repo.delete_invite(parts(request).pool, invite_id):
        raise HTTPException(404, "No such pending invitation.")


@router.patch("/members/{user_id}")
async def change_member(user_id: UUID, body: MemberChange, request: Request, _: User = Depends(admin_user)) -> dict:
    pool = parts(request).pool
    target = await repo.get_user(pool, user_id)
    if target is None:
        raise HTTPException(404, "No such member.")
    loses_admin = target.role == "admin" and target.active and (body.role == "member" or body.active is False)
    if loses_admin and await repo.count_active_admins(pool) <= 1:
        raise HTTPException(409, "This is the only active admin. Make someone else an admin first.")
    updated = await repo.update_user(pool, user_id, role=body.role, active=body.active)
    if body.active is False:
        # Signed-in sessions end now. API keys stop working too, because every
        # request checks that the key's owner is still active.
        await repo.delete_user_sessions(pool, user_id)
    return user_json(updated)


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(user_id: UUID, request: Request, admin: User = Depends(admin_user)) -> None:
    """Removes a member for good. What they did stays in the audit log, under their email."""
    pool = parts(request).pool
    target = await repo.get_user(pool, user_id)
    if target is None:
        raise HTTPException(404, "No such member.")
    if target.id == admin.id:
        raise HTTPException(409, "You cannot remove your own account.")
    if target.role == "admin" and target.active and await repo.count_active_admins(pool) <= 1:
        raise HTTPException(409, "This is the only active admin. Make someone else an admin first.")
    await repo.delete_user(pool, user_id)


@router.get("/keys")
async def list_keys(request: Request, everyone: bool = False, user: User = Depends(current_user)) -> list[dict]:
    """A user's own keys. With ?everyone=true an admin sees the whole team's."""
    if everyone and user.role != "admin":
        raise HTTPException(403, "Only an admin can see other people's keys.")
    keys = await repo.list_api_keys(parts(request).pool, None if everyone else user.id)
    return [key_json(key) for key in keys]


@router.post("/keys", status_code=201)
async def create_key(body: NewKey, request: Request, user: User = Depends(current_user)) -> dict:
    pool = parts(request).pool
    owner = user
    if body.user_id and body.user_id != user.id:
        if user.role != "admin":
            raise HTTPException(403, "Only an admin can issue a key for someone else.")
        owner = await repo.get_user(pool, body.user_id)
        if owner is None or not owner.active:
            raise HTTPException(404, "No such active member.")
    api_key = security.new_api_key()
    stored = await repo.create_api_key(
        pool, owner.id, body.name.strip(), security.display_prefix(api_key), security.hash_token(api_key)
    )
    # Returned this once. Only its hash was stored.
    return {**key_json(stored), "user_email": owner.email, "key": api_key}


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: UUID, request: Request, user: User = Depends(current_user)) -> None:
    owner = None if user.role == "admin" else user.id
    if not await repo.revoke_api_key(parts(request).pool, key_id, owner):
        raise HTTPException(404, "No such active key.")
