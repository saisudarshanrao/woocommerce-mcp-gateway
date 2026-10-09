"""Shared test fixtures: a fake WooCommerce store behind the real client and tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from mcp import Client

from woo_mcp.config import Settings
from woo_mcp.server import build_server
from woo_mcp.woocommerce import WooCommerce

# The two URLs differ on purpose: requests go to the internal one, signatures
# are computed against the public one.
SETTINGS = Settings(
    store_internal_url="http://wordpress.internal",
    store_public_url="https://shop.example.com",
    consumer_key="ck_test",
    consumer_secret="cs_test",
    auth_token="test-token",
)

# 2026-10-09 05:00 UTC is 10:30 on the same day in Asia/Kolkata.
NOW = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)

ORDER = {
    "id": 41,
    "status": "processing",
    "total": "37.98",
    "currency": "USD",
    "date_created_gmt": "2026-10-09T04:15:00",
    "payment_method": "cod",
    "payment_method_title": "Cash on delivery",
    "customer_note": "",
    "shipping_total": "0.00",
    "discount_total": "0.00",
    "total_tax": "0.00",
    "date_paid_gmt": None,
    "date_completed_gmt": None,
    "billing": {"first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "city": "Pune"},
    "shipping": {"first_name": "Asha", "last_name": "Rao"},
    "line_items": [
        {"product_id": 10, "name": "Classic T-Shirt", "sku": "tee", "quantity": 2, "subtotal": "37.98", "total": "37.98"}
    ],
}

PRODUCT = {
    "id": 10,
    "name": "Classic T-Shirt",
    "status": "publish",
    "sku": "tee",
    "price": "18.99",
    "regular_price": "18.99",
    "sale_price": "",
    "stock_status": "instock",
    "total_sales": 3,
    "permalink": "https://shop.example.com/product/classic-t-shirt/",
    "short_description": "<p>A soft cotton <strong>tee</strong>.</p>\n",
    "date_created_gmt": "2026-10-08T17:15:40",
}


class FakeStore:
    """Answers the handful of WooCommerce endpoints the tools use, and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.orders: dict[int, dict[str, Any]] = {41: dict(ORDER)}
        self.fail_with: Exception | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail_with:
            raise self.fail_with
        path, method = request.url.path, request.method

        if path == "/wp-json/":
            return httpx2.Response(
                200,
                json={"name": "Test Shop", "home": "https://shop.example.com", "gmt_offset": 5.5, "timezone_string": "Asia/Kolkata"},
            )
        if path.endswith("/data/currencies/current"):
            return httpx2.Response(200, json={"code": "USD"})
        if path.endswith("/products") and method == "GET":
            return httpx2.Response(200, json=[PRODUCT], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})
        if path.endswith("/products") and method == "POST":
            body = json.loads(request.content)
            return httpx2.Response(201, json={**PRODUCT, "id": 77, **body, "price": body["regular_price"]})
        if path.endswith("/orders") and method == "GET":
            orders = list(self.orders.values())
            return httpx2.Response(
                200, json=orders, headers={"X-WP-Total": str(len(orders)), "X-WP-TotalPages": "1"}
            )
        if "/orders/" in path:
            parts = path.split("/orders/")[1].split("/")
            order = self.orders.get(int(parts[0]))
            if order is None:
                return httpx2.Response(
                    404, json={"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}
                )
            if len(parts) == 2 and parts[1] == "notes":
                return httpx2.Response(201, json={"id": 1, **json.loads(request.content)})
            if method == "PUT":
                order.update(json.loads(request.content))
            return httpx2.Response(200, json=order)
        return httpx2.Response(404, json={"code": "rest_no_route", "message": "No route."})

    def last(self, method: str, path_suffix: str) -> httpx2.Request:
        for request in reversed(self.requests):
            if request.method == method and request.url.path.endswith(path_suffix):
                return request
        raise AssertionError(f"no {method} request to ...{path_suffix} was made")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def client(store: FakeStore):
    """An MCP client connected in memory to the real server, which talks to the fake store."""
    woo = WooCommerce(SETTINGS, transport=httpx2.MockTransport(store), utcnow=lambda: NOW)
    async with Client(build_server(woo), raise_exceptions=True) as connected:
        yield connected
    await woo.aclose()
