"""The dashboard's side of browser sign-in for MCP clients: the yes or no, and the list of what was allowed.

The OAuth endpoints themselves (/authorize, /token, /register) are in
gateway/oauth.py. /authorize sends the browser to the dashboard's consent
page, and that page calls the endpoints here. They need a dashboard session,
which is how a sign-in ends up tied to a user.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from gateway import oauth
from gateway.api.deps import current_user, parts
from gateway.repo import User

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

GONE = "This sign-in request has expired or was already answered. Start again from your MCP client."


@router.get("/requests/{request_id}")
async def pending_request(request_id: str, request: Request, _: User = Depends(current_user)) -> dict:
    """What the consent page shows: who is asking, and where the browser will be sent back to."""
    found = await parts(request).oauth.pending(request_id)
    if found is None:
        raise HTTPException(404, GONE)
    client_name, params = found
    return {"client_name": client_name, "returns_to": urlsplit(str(params.redirect_uri)).netloc}


@router.post("/requests/{request_id}/approve")
async def approve(request_id: str, request: Request, user: User = Depends(current_user)) -> dict:
    """The signed-in user allows the client to act as them. The page then follows `redirect`."""
    redirect = await parts(request).oauth.approve(request_id, user.id)
    if redirect is None:
        raise HTTPException(404, GONE)
    return {"redirect": redirect}


@router.post("/requests/{request_id}/deny")
async def deny(request_id: str, request: Request, _: User = Depends(current_user)) -> dict:
    redirect = await parts(request).oauth.deny(request_id)
    if redirect is None:
        raise HTTPException(404, GONE)
    return {"redirect": redirect}


@router.get("/grants")
async def list_grants(request: Request, everyone: bool = False, user: User = Depends(current_user)) -> list[dict]:
    """MCP clients that are signed in. Yours, or with `everyone` (admins only) the whole team's."""
    if everyone and user.role != "admin":
        raise HTTPException(403, "Only an admin can see everyone's.")
    grants = await oauth.list_grants(parts(request).pool, None if everyone else user.id)
    return [
        {
            "id": str(grant.id),
            "client_name": grant.client_name,
            "user_id": str(grant.user_id),
            "user_email": grant.user_email,
            "created_at": grant.created_at.isoformat(),
            "last_used_at": grant.last_used_at.isoformat() if grant.last_used_at else None,
        }
        for grant in grants
    ]


@router.delete("/grants/{grant_id}", status_code=204)
async def revoke_grant(grant_id: UUID, request: Request, user: User = Depends(current_user)) -> None:
    """Signs a client out. Its next call is refused. Admins can do this for anyone."""
    owner = None if user.role == "admin" else user.id
    if not await oauth.revoke_grant(parts(request).pool, grant_id, owner):
        raise HTTPException(404, "No such sign-in.")
