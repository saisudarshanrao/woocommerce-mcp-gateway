"""Stores created from the dashboard.

Nothing here touches Kubernetes. Creating a store writes a row saying it
should exist; deleting one marks the row. The provisioner, a separate component
that holds the cluster permissions this API deliberately does not have, does
the work and reports progress back through the same row.
"""

from __future__ import annotations

import re
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gateway import repo
from gateway.api.deps import Parts, current_user, parts
from gateway.repo import Store, User

router = APIRouter(prefix="/api", tags=["stores"])

# A store's identifier becomes part of a namespace name and of a hostname, so
# it has to be a valid DNS label; the length leaves room for the "store-" prefix
# within the gateway's limit on server identifiers.
SLUG_PATTERN = re.compile(r"^[a-z]([a-z0-9-]{0,16}[a-z0-9])?$")


class NewStore(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    slug: str = Field(description="Short identifier: the store's hostname and the prefix of its tools.")


def enabled(request: Request) -> Parts:
    app = parts(request)
    if not app.settings.stores_enabled:
        raise HTTPException(404, "Creating stores is not switched on for this gateway.")
    return app


def store_json(store: Store, viewer: User) -> dict:
    return {
        "id": str(store.id),
        "slug": store.slug,
        "name": store.name,
        "status": store.status,
        "detail": store.detail,
        "url": store.url,
        "namespace": store.namespace,
        "tool_prefix": f"{store.server_slug}__",
        "owner_email": store.owner_email,
        "created_at": store.created_at.isoformat(),
        "can_manage": viewer.role == "admin" or store.owner_id == viewer.id,
    }


async def managed_store(request: Request, store_id: UUID, user: User) -> Store:
    """The store, if this user may change it: its owner, or an admin."""
    store = await repo.get_store(parts(request).pool, store_id)
    if store is None:
        raise HTTPException(404, "No such store.")
    if user.role != "admin" and store.owner_id != user.id:
        raise HTTPException(403, "Only the store's owner or an admin can do this.")
    return store


@router.get("/stores")
async def list_stores(request: Request, user: User = Depends(current_user)) -> list[dict]:
    app = enabled(request)
    return [store_json(store, user) for store in await repo.list_stores(app.pool)]


@router.post("/stores", status_code=201)
async def create_store(body: NewStore, request: Request, user: User = Depends(current_user)) -> dict:
    app = enabled(request)
    slug = body.slug.strip().lower()
    if not SLUG_PATTERN.match(slug):
        raise HTTPException(
            422,
            "The identifier must start with a letter, end with a letter or digit, and use only lower-case "
            "letters, digits and hyphens (max 18).",
        )
    if await repo.get_server_by_slug(app.pool, f"store-{slug}"):
        raise HTTPException(409, f"The gateway already has a server with the identifier 'store-{slug}'.")
    limit = app.settings.max_stores_per_user
    try:
        store = await repo.create_store(app.pool, slug=slug, name=body.name.strip(), owner=user, limit=limit)
    except asyncpg.UniqueViolationError:
        # Also what a second click on "create" gets: one store, not two.
        raise HTTPException(409, f"There is already a store with the identifier '{slug}'.") from None
    if store is None:
        raise HTTPException(409, f"You already have {limit} stores, the most one person may have. Delete one first.")
    return store_json(store, user)


@router.post("/stores/{store_id}/retry")
async def retry_store(store_id: UUID, request: Request, user: User = Depends(current_user)) -> dict:
    """Has another go at a store that failed. Whatever was already created is reused."""
    app = enabled(request)
    await managed_store(request, store_id, user)
    store = await repo.retry_store(app.pool, store_id)
    if store is None:
        raise HTTPException(409, "Only a store that has failed can be retried.")
    return store_json(store, user)


@router.delete("/stores/{store_id}", status_code=202)
async def delete_store(store_id: UUID, request: Request, user: User = Depends(current_user)) -> dict:
    """Starts removing a store. Its tools leave the gateway now; the cluster follows."""
    app = enabled(request)
    await managed_store(request, store_id, user)
    store = await repo.mark_store_deleting(app.pool, store_id)
    if store is None:
        raise HTTPException(404, "No such store.")
    return store_json(store, user)
