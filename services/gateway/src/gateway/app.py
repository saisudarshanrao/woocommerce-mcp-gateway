"""Assembles the gateway: the REST API for the dashboard, and the MCP endpoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST

from gateway import db, oauth, repo, seed
from gateway.api import auth, chat, members, oauth as oauth_api, servers, stores
from gateway.api.deps import Parts
from gateway.chat.llm import ChatModel
from gateway.config import Settings
from gateway.core import Gateway
from gateway.metrics import Metrics
from gateway.mcp_endpoint import MCP_PATH, SEARCH_PATH, ApiKeyAuth, build_mcp_app
from gateway.security import CredentialCipher
from gateway.upstreams import HttpClientFactory, UpstreamManager, guarded_http_client
from gateway.urlguard import UrlGuard

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    http_client_factory: HttpClientFactory | None = None,
    chat_model: ChatModel | None = None,
) -> FastAPI:
    # Everything is wired together here without touching the network. The
    # database pool connects, and probing begins, when the app starts up.
    pool = db.Database(settings.database_url)
    cipher = CredentialCipher(settings.encryption_key)
    url_guard = UrlGuard(settings.upstream_internal_hosts, settings.upstream_allow_public)
    upstreams = UpstreamManager(
        pool,
        cipher,
        probe_interval_seconds=settings.probe_interval_seconds,
        probe_timeout_seconds=settings.probe_timeout_seconds,
        http_client_factory=http_client_factory or guarded_http_client(url_guard),
    )
    metrics = Metrics()
    gateway = Gateway(pool, upstreams, calls_per_minute=settings.rate_limit_per_minute, metrics=metrics)
    mcp_servers, mcp_app = build_mcp_app(gateway, settings)
    if chat_model is None and settings.llm_api_key and settings.llm_model and settings.llm_base_url:
        chat_model = ChatModel(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    oauth_store = oauth.OAuthStore(pool, settings.public_url)
    # Browser sign-in for MCP clients needs HTTPS, or an address on this machine.
    oauth_on = oauth.allowed_issuer(settings.public_url)
    app_parts = Parts(
        settings=settings,
        pool=pool,
        cipher=cipher,
        upstreams=upstreams,
        gateway=gateway,
        url_guard=url_guard,
        oauth=oauth_store,
        chat_model=chat_model,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await pool.connect(retry_for_seconds=90)
        await db.migrate(pool)
        await repo.delete_expired_sessions(pool)
        await oauth_store.delete_expired()
        await seed.bootstrap_admin(pool, settings)
        await seed.seed_servers(pool, cipher, settings)
        await upstreams.start()
        # The MCP apps are mounted inside this one, and a mounted app's own
        # startup never runs, so their session managers are started here.
        async with AsyncExitStack() as running:
            for mcp_server in mcp_servers:
                await running.enter_async_context(mcp_server.session_manager.run())
            app_parts.ready = True
            logger.info("gateway ready at %s", settings.public_url)
            try:
                yield
            finally:
                app_parts.ready = False
                await upstreams.stop()
                await pool.close()

    app = FastAPI(
        title="Team MCP gateway",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.parts = app_parts

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        """Liveness: the process is serving."""
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> PlainTextResponse:
        """Readiness: started up, and the database answers."""
        if not app_parts.ready:
            return PlainTextResponse("starting", status_code=503)
        try:
            await pool.fetchval("SELECT 1")
        except Exception:
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        """Figures for Prometheus. The Ingress does not route here, so only the cluster can ask."""
        return Response(metrics.render(upstreams.states()), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/config")
    async def config() -> dict:
        """What the dashboard needs to know about this deployment. Nothing secret."""
        return {
            "public_url": settings.public_url,
            "mcp_url": f"{settings.public_url}{MCP_PATH}",
            # The same tools presented as two, for when there are too many to list.
            "mcp_search_url": f"{settings.public_url}{SEARCH_PATH}",
            # Whether an MCP client can sign in with a browser instead of a key.
            "oauth_enabled": oauth_on,
            "chat_tool_search_above": settings.chat_tool_search_above,
            "chat_enabled": app_parts.chat_model is not None,
            "chat_model": app_parts.chat_model.model if app_parts.chat_model else "",
            "rate_limit_per_minute": settings.rate_limit_per_minute,
            "stores_enabled": settings.stores_enabled,
            # Whether a server can be added by container image, and which images are accepted.
            "hosted_enabled": settings.hosted_enabled,
            "hosted_image_prefixes": list(settings.hosted_image_prefixes),
            "max_stores_per_user": settings.max_stores_per_user,
            "store_url_template": settings.store_url_template,
        }

    app.include_router(auth.router)
    app.include_router(members.router)
    app.include_router(servers.router)
    app.include_router(chat.router)
    app.include_router(stores.router)
    app.include_router(oauth_api.router)
    if oauth_on:
        app.router.routes.extend(oauth.oauth_routes(oauth_store, settings.public_url, MCP_PATH))
    else:
        logger.info("browser sign-in for MCP clients is off: it needs an https address")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, error: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=error)
        return JSONResponse({"detail": "The gateway hit an unexpected error."}, status_code=500)

    # Everything not matched above goes to the MCP app, which serves /mcp and
    # answers 404 for anything else. API-key authentication wraps only it.
    sign_in = oauth.resource_metadata_url(settings.public_url, MCP_PATH) if oauth_on else None
    app.mount("/", ApiKeyAuth(mcp_app, pool, sign_in))
    return app
