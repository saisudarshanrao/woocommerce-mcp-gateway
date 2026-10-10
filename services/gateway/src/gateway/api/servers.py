"""The registry of upstream MCP servers, and the audit log."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gateway import naming, repo
from gateway.api.deps import admin_user, current_user, parts
from gateway.repo import Server, User
from gateway.upstreams import UpstreamState

router = APIRouter(prefix="/api", tags=["servers"])

AuthType = Literal["none", "bearer", "header"]
MemberAccess = Literal["all", "read_only", "none"]


class NewServer(BaseModel):
    slug: str = Field(description="Short identifier; becomes the prefix of every tool name.")
    name: str = Field(min_length=1, max_length=80)
    url: str
    auth_type: AuthType = "none"
    auth_header: str | None = Field(default=None, max_length=100)
    credential: str | None = Field(default=None, max_length=4000)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    member_access: MemberAccess = "all"
    enabled: bool = True


class ServerChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = None
    auth_type: AuthType | None = None
    auth_header: str | None = Field(default=None, max_length=100)
    # Left out means "keep the stored credential".
    credential: str | None = Field(default=None, max_length=4000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    member_access: MemberAccess | None = None
    enabled: bool | None = None


def check_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(422, "The URL must start with http:// or https:// and name a host.")
    return url.strip()


def server_json(server: Server, state: UpstreamState | None, viewer: User) -> dict[str, Any]:
    """A server as the dashboard shows it. The credential itself is never included."""
    status = "disabled" if not server.enabled else (state.status if state else "pending")
    live = state if state and server.enabled else None
    payload: dict[str, Any] = {
        "id": str(server.id),
        "slug": server.slug,
        "name": server.name,
        "enabled": server.enabled,
        "status": status,
        "error": live.error if live else None,
        "checked_at": live.checked_at.isoformat() if live and live.checked_at else None,
        "latency_ms": live.latency_ms if live else None,
        "protocol_version": live.protocol_version if live else None,
        "member_access": server.member_access,
        "tools": [
            {
                "name": tool.tool.name,
                "title": tool.tool.title,
                "description": tool.tool.description,
                "read_only": tool.read_only,
            }
            for tool in sorted(live.tools.values(), key=lambda tool: tool.tool.name)
        ]
        if live
        else [],
        "skipped_tools": live.skipped if live else [],
    }
    if viewer.role == "admin":
        payload |= {
            "url": server.url,
            "timeout_seconds": server.timeout_seconds,
            # Enough to show how the server is authenticated, and nothing that
            # would let anyone authenticate as the gateway.
            "auth": {
                "type": server.auth_type,
                "header": server.auth_header,
                "credential_set": server.auth_secret is not None,
            },
        }
    return payload


@router.get("/servers")
async def list_servers(request: Request, user: User = Depends(current_user)) -> list[dict]:
    app = parts(request)
    return [
        server_json(server, app.upstreams.state(server.id), user) for server in await repo.list_servers(app.pool)
    ]


@router.post("/servers", status_code=201)
async def add_server(body: NewServer, request: Request, admin: User = Depends(admin_user)) -> dict:
    app = parts(request)
    slug = body.slug.strip().lower()
    if not naming.is_valid_slug(slug):
        raise HTTPException(
            422, "The identifier must start with a letter and use only lower-case letters, digits and hyphens (max 24)."
        )
    if await repo.get_server_by_slug(app.pool, slug):
        raise HTTPException(409, f"A server with the identifier '{slug}' already exists.")
    if body.auth_type != "none" and not body.credential:
        raise HTTPException(422, "This kind of authentication needs a credential.")
    if body.auth_type == "header" and not body.auth_header:
        raise HTTPException(422, "Name the header the credential is sent in.")

    server = await repo.create_server(
        app.pool,
        created_by=admin.id,
        slug=slug,
        name=body.name.strip(),
        url=check_url(body.url),
        auth_type=body.auth_type,
        auth_header=body.auth_header.strip() if body.auth_type == "header" and body.auth_header else None,
        auth_secret=app.cipher.encrypt(body.credential) if body.auth_type != "none" else None,
        enabled=body.enabled,
        timeout_seconds=body.timeout_seconds,
        member_access=body.member_access,
    )
    return server_json(server, await app.upstreams.refresh(server.id), admin)


@router.patch("/servers/{server_id}")
async def change_server(
    server_id: UUID, body: ServerChange, request: Request, admin: User = Depends(admin_user)
) -> dict:
    app = parts(request)
    current = await repo.get_server(app.pool, server_id)
    if current is None:
        raise HTTPException(404, "No such server.")

    changes: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"credential"})
    if "url" in changes:
        changes["url"] = check_url(changes["url"])
    auth_type = changes.get("auth_type", current.auth_type)
    if auth_type == "none":
        changes |= {"auth_header": None, "auth_secret": None}
    else:
        if body.credential:
            changes["auth_secret"] = app.cipher.encrypt(body.credential)
        elif current.auth_secret is None:
            raise HTTPException(422, "This kind of authentication needs a credential.")
        if auth_type == "header" and not changes.get("auth_header", current.auth_header):
            raise HTTPException(422, "Name the header the credential is sent in.")
        if auth_type == "bearer":
            changes["auth_header"] = None

    server = await repo.update_server(app.pool, server_id, changes)
    return server_json(server, await app.upstreams.refresh(server_id), admin)


@router.delete("/servers/{server_id}", status_code=204)
async def remove_server(server_id: UUID, request: Request, _: User = Depends(admin_user)) -> None:
    app = parts(request)
    if not await repo.delete_server(app.pool, server_id):
        raise HTTPException(404, "No such server.")
    await app.upstreams.refresh(server_id)


@router.post("/servers/{server_id}/refresh")
async def refresh_server(server_id: UUID, request: Request, admin: User = Depends(admin_user)) -> dict:
    app = parts(request)
    state = await app.upstreams.refresh(server_id)
    if state is None:
        raise HTTPException(404, "No such server.")
    return server_json(state.server, state, admin)


@router.get("/audit")
async def audit_log(
    request: Request,
    user_id: UUID | None = None,
    server: str | None = None,
    outcome: str | None = None,
    before: int | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
) -> list[dict]:
    """Tool calls, newest first. Admins see everyone's; a member sees their own."""
    entries = await repo.list_audit(
        parts(request).pool,
        user_id=user_id if user.role == "admin" else user.id,
        server_slug=server or None,
        outcome=outcome or None,
        before_id=before,
        limit=max(1, min(limit, 500)),
    )
    return [
        {
            "id": entry.id,
            "at": entry.at.isoformat(),
            "user_id": str(entry.user_id) if entry.user_id else None,
            "user_email": entry.user_email,
            "api_key_prefix": entry.api_key_prefix,
            "source": entry.source,
            "server": entry.server_slug,
            "tool": entry.tool,
            "duration_ms": entry.duration_ms,
            "outcome": entry.outcome,
            "error": entry.error,
        }
        for entry in entries
    ]
