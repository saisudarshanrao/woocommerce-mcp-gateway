"""What a fresh deployment starts with: one admin, and the servers named in its seed file."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg

from gateway import naming, repo, security
from gateway.config import Settings
from gateway.security import CredentialCipher

logger = logging.getLogger(__name__)


async def bootstrap_admin(pool: asyncpg.Pool, settings: Settings) -> None:
    """Creates the first admin, only while there are no users at all."""
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    if await repo.count_users(pool) > 0:
        return
    try:
        await repo.create_user(
            pool,
            settings.bootstrap_admin_email,
            "Administrator",
            "admin",
            security.hash_password(settings.bootstrap_admin_password),
        )
    except asyncpg.UniqueViolationError:
        # Another replica, started at the same moment on the same empty
        # database, got there first. That is the outcome wanted.
        return
    logger.info("created the first admin, %s", settings.bootstrap_admin_email)


async def seed_servers(pool: asyncpg.Pool, cipher: CredentialCipher, settings: Settings) -> None:
    """Registers the servers in the seed file, each one only once ever.

    A seed is remembered by its slug. If an admin later removes a seeded
    server, it stays removed: the seed is a starting point, not a rule.
    """
    if not settings.seed_servers_file:
        return
    path = Path(settings.seed_servers_file)
    if not path.is_file():
        logger.warning("seed file %s does not exist", path)
        return
    for entry in json.loads(path.read_text() or "[]"):
        slug = entry["slug"]
        if not naming.is_valid_slug(slug):
            logger.error("seed server %r has an invalid slug and was skipped", slug)
            continue
        if not await repo.claim_seed(pool, slug):
            continue
        if await repo.get_server_by_slug(pool, slug):
            continue
        auth_type = entry.get("auth_type", "none")
        credential = entry.get("credential") or ""
        if auth_type != "none" and not credential:
            logger.warning("seed server %r has no credential yet; registering it without one", slug)
            auth_type = "none"
        await repo.create_server(
            pool,
            created_by=None,
            slug=slug,
            name=entry.get("name", slug),
            url=entry["url"],
            auth_type=auth_type,
            auth_header=entry.get("auth_header") if auth_type == "header" else None,
            auth_secret=cipher.encrypt(credential) if auth_type != "none" else None,
            member_access=entry.get("member_access", "all"),
            timeout_seconds=int(entry.get("timeout_seconds", 30)),
        )
        logger.info("registered seed server %s", slug)
