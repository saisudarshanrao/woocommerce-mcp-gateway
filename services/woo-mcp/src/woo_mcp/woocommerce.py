"""A small client for the WooCommerce REST API (wc/v3).

It does three things the tools should not have to think about: sign every
request, turn the many ways a call can fail into one exception with a message
worth showing to a model, and remember the few facts about the store that
almost never change (its name, timezone and currency).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2

from woo_mcp import oauth
from woo_mcp.config import Settings
from woo_mcp.dates import store_timezone

logger = logging.getLogger(__name__)

API_PREFIX = "/wp-json/wc/v3"
STORE_FACTS_TTL_SECONDS = 300


class WooCommerceError(Exception):
    """A call to the store failed. The message is written for the tool's caller."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class StoreFacts:
    name: str
    url: str
    timezone: tzinfo
    timezone_name: str
    currency: str
    utcnow: Callable[[], datetime]

    def now(self) -> datetime:
        """The current time on the store's own clock."""
        return self.utcnow().astimezone(self.timezone)


@dataclass(frozen=True)
class Page:
    """One page of a list endpoint, with the totals WooCommerce reports in headers."""

    items: list[dict[str, Any]]
    total: int
    total_pages: int


class WooCommerce:
    def __init__(
        self,
        settings: Settings,
        transport: httpx2.AsyncBaseTransport | None = None,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        # `transport` and `utcnow` are for tests: a fake store and a fixed clock.
        self._settings = settings
        self._utcnow = utcnow
        self._client = httpx2.AsyncClient(
            base_url=settings.store_internal_url,
            timeout=settings.request_timeout_seconds,
            transport=transport,
            headers={"User-Agent": "woo-mcp", "Accept": "application/json"},
        )
        # WooCommerce rebuilds the signed URL from WordPress's own "home" URL,
        # with the scheme the request actually arrived on. So the signature
        # uses the public host and path with the internal URL's scheme.
        internal, public = urlsplit(settings.store_internal_url), urlsplit(settings.store_public_url)
        self._signing_base = urlunsplit((internal.scheme, public.netloc, public.path.rstrip("/"), "", ""))
        self._facts: StoreFacts | None = None
        self._facts_expire_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def get_page(self, path: str, params: dict[str, Any]) -> Page:
        response = await self._request("GET", path, params=params)
        return Page(
            items=response.json(),
            total=int(response.headers.get("X-WP-Total", 0)),
            total_pages=int(response.headers.get("X-WP-TotalPages", 0)),
        )

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        response = await self._request("POST", path, body=body)
        return response.json()

    async def put(self, path: str, body: dict[str, Any]) -> Any:
        response = await self._request("PUT", path, body=body)
        return response.json()

    async def store_facts(self) -> StoreFacts:
        """Name, timezone and currency of the store, cached for a few minutes."""
        if self._facts and time.monotonic() < self._facts_expire_at:
            return self._facts
        # The site index is public, so this request carries no signature.
        site = (
            await self._send(
                "GET",
                "/wp-json/",
                params={"_fields": "name,url,home,gmt_offset,timezone_string"},
            )
        ).json()
        currency = await self.get("/data/currencies/current")
        tz = store_timezone(site.get("timezone_string"), site.get("gmt_offset"))
        self._facts = StoreFacts(
            name=site.get("name", ""),
            url=site.get("home") or site.get("url", ""),
            timezone=tz,
            timezone_name=site.get("timezone_string") or str(tz),
            currency=currency.get("code", ""),
            utcnow=self._utcnow,
        )
        self._facts_expire_at = time.monotonic() + STORE_FACTS_TTL_SECONDS
        return self._facts

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx2.Response:
        """A signed request to a wc/v3 endpoint."""
        # Drop unset filters and stringify the rest, so the values that are
        # signed are byte for byte the values that are sent.
        query = {key: _query_value(value) for key, value in (params or {}).items() if value is not None}
        signed = oauth.sign(
            method,
            self._signing_base + API_PREFIX + path,
            query,
            self._settings.consumer_key,
            self._settings.consumer_secret,
        )
        headers = {"Authorization": oauth.authorization_header(signed)}
        return await self._send(method, API_PREFIX + path, params=query, body=body, headers=headers)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        started = time.perf_counter()
        try:
            response = await self._client.request(method, url, params=params, json=body, headers=headers)
        except httpx2.TimeoutException as error:
            raise WooCommerceError(
                f"The store did not answer within {self._settings.request_timeout_seconds:g} seconds."
            ) from error
        except httpx2.RequestError as error:
            raise WooCommerceError(f"The store could not be reached: {error}") from error
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info("%s %s finished in %.0f ms", method, url, elapsed_ms)

        if response.is_success:
            return response
        raise _error_from(response)


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _error_from(response: httpx2.Response) -> WooCommerceError:
    """Builds a readable error from WooCommerce's {"code", "message"} body."""
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return WooCommerceError(f"The store returned an unexpected response (HTTP {status}).", status=status)

    code = str(payload.get("code", ""))
    message = str(payload.get("message", "")).strip() or "no details given"
    if status == 404:
        return WooCommerceError("It does not exist in the store.", status=status, code=code)
    if status == 401:
        # Almost always a key problem on this side, not something the caller can fix.
        return WooCommerceError(
            f"The store rejected this server's API key ({code}). Check the key pair in the store's Secret.",
            status=status,
            code=code,
        )
    return WooCommerceError(f"The store refused the request (HTTP {status}, {code}): {message}", status=status, code=code)
