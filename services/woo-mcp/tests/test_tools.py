"""The tools, driven through a real MCP client, against a fake store."""

import json
import re
from urllib.parse import parse_qsl, unquote

import httpx2
import pytest

from woo_mcp import oauth

pytestmark = pytest.mark.anyio


async def test_the_six_tools_are_listed_with_read_only_hints(client):
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {
        "list_products",
        "get_product",
        "create_product",
        "list_orders",
        "get_order",
        "update_order_status",
    }
    read_only = {name for name, tool in tools.items() if tool.annotations.read_only_hint}
    assert read_only == {"list_products", "get_product", "list_orders", "get_order"}
    assert all(tool.output_schema for tool in tools.values())


async def test_list_products_returns_trimmed_products_in_the_store_currency(client):
    result = await client.call_tool("list_products", {})

    assert not result.is_error
    listing = result.structured_content
    assert listing["total"] == 1
    assert listing["products"][0] == {
        "id": 10,
        "name": "Classic T-Shirt",
        "status": "publish",
        "sku": "tee",
        "price": "18.99",
        "regular_price": "18.99",
        "sale_price": "",
        "currency": "USD",
        "stock_status": "instock",
        "total_sales": 3,
        "permalink": "https://shop.example.com/product/classic-t-shirt/",
        "short_description": "A soft cotton tee.",
        "created_at": "2026-10-08T22:45:40+05:30",
    }


async def test_requests_are_signed_against_the_public_url_not_the_one_they_are_sent_to(client, store):
    await client.call_tool("list_products", {"search": "tee shirt"})

    request = store.last("GET", "/products")
    assert request.url.host == "wordpress.internal"

    header = request.headers["Authorization"]
    assert header.startswith("OAuth ")
    sent = {key: unquote(value) for key, value in re.findall(r'(oauth_\w+)="([^"]*)"', header)}
    # Recompute the signature the way WooCommerce does: its own home URL, with
    # the scheme the request arrived on (http), and the query actually sent.
    expected = oauth.sign(
        "GET",
        "http://shop.example.com/wp-json/wc/v3/products",
        dict(parse_qsl(request.url.query.decode())),
        "ck_test",
        "cs_test",
        nonce=sent["oauth_nonce"],
        timestamp=int(sent["oauth_timestamp"]),
    )
    assert sent["oauth_signature"] == expected["oauth_signature"]
    # The signed parameters travel in the header only, and the secret not at all.
    assert "oauth_" not in request.url.query.decode()
    assert "cs_test" not in header


async def test_the_public_site_index_is_fetched_without_a_signature(client, store):
    await client.call_tool("list_products", {})

    assert "Authorization" not in store.last("GET", "/wp-json/").headers


async def test_orders_today_asks_for_everything_since_midnight_in_the_store_timezone(client, store):
    result = await client.call_tool("list_orders", {"period": "today"})

    query = dict(parse_qsl(store.last("GET", "/orders").url.query.decode()))
    # The fixed clock reads 10:30 on 9 October in Kolkata. Midnight there is
    # 18:30 UTC the day before.
    assert query["after"] == "2026-10-08T18:30:00"
    assert query["dates_are_gmt"] == "true"
    assert "before" not in query

    listing = result.structured_content
    assert listing["period"] == {
        "name": "today",
        "timezone": "Asia/Kolkata",
        "start": "2026-10-09T00:00:00+05:30",
        "end": None,
    }
    assert listing["listed_orders_value"] == "37.98"
    assert listing["orders"][0]["created_at"] == "2026-10-09T09:45:00+05:30"
    assert listing["orders"][0]["customer_name"] == "Asha Rao"
    assert listing["orders"][0]["items"] == [
        {"product_id": 10, "name": "Classic T-Shirt", "sku": "tee", "quantity": 2, "total": "37.98"}
    ]


async def test_listing_all_orders_sends_no_date_filter(client, store):
    await client.call_tool("list_orders", {})

    query = dict(parse_qsl(store.last("GET", "/orders").url.query.decode()))
    assert not {"after", "before", "dates_are_gmt"} & set(query)


async def test_explicit_dates_override_the_period(client, store):
    result = await client.call_tool(
        "list_orders", {"period": "today", "after": "2026-10-01", "before": "2026-10-02T00:00:00+00:00"}
    )

    query = dict(parse_qsl(store.last("GET", "/orders").url.query.decode()))
    assert query["after"] == "2026-09-30T18:30:00"  # store-local midnight
    assert query["before"] == "2026-10-02T00:00:00"  # the offset given was UTC
    assert result.structured_content["period"]["name"] == "custom"


async def test_a_bad_date_is_explained_to_the_model_without_calling_the_store(client, store):
    result = await client.call_tool("list_orders", {"after": "next tuesday"})

    assert result.is_error
    assert "ISO 8601" in result.content[0].text
    assert not [request for request in store.requests if request.url.path.endswith("/orders")]


async def test_create_product_posts_a_published_simple_product(client, store):
    result = await client.call_tool("create_product", {"name": "Wool Hat", "regular_price": 12.5})

    body = json.loads(store.last("POST", "/products").content)
    assert body == {
        "name": "Wool Hat",
        "type": "simple",
        "regular_price": "12.50",
        "description": "",
        "short_description": "",
        "status": "publish",
    }
    assert result.structured_content["id"] == 77
    assert result.structured_content["price"] == "12.50"


@pytest.mark.parametrize("arguments", [{"name": "", "regular_price": 5}, {"name": "Hat", "regular_price": 0}, {"name": "Hat"}])
async def test_create_product_rejects_bad_arguments_before_calling_the_store(client, store, arguments):
    result = await client.call_tool("create_product", arguments)

    assert result.is_error
    assert not store.requests


async def test_get_order_includes_addresses_and_totals(client):
    result = await client.call_tool("get_order", {"order_id": 41})

    order = result.structured_content
    assert order["billing"]["name"] == "Asha Rao"
    assert order["billing"]["city"] == "Pune"
    assert order["subtotal"] == "37.98"
    assert order["payment_method"] == "Cash on delivery"
    assert order["paid_at"] is None


async def test_a_missing_order_is_reported_as_missing(client):
    result = await client.call_tool("get_order", {"order_id": 999})

    assert result.is_error
    assert "Could not get order 999. It does not exist in the store." in result.content[0].text


async def test_update_order_status_reports_the_change_and_records_the_note(client, store):
    result = await client.call_tool(
        "update_order_status", {"order_id": 41, "status": "completed", "note": "Shipped by courier"}
    )

    assert json.loads(store.last("PUT", "/orders/41").content) == {"status": "completed"}
    assert json.loads(store.last("POST", "/orders/41/notes").content) == {
        "note": "Shipped by courier",
        "customer_note": False,
    }
    change = result.structured_content
    assert change["previous_status"] == "processing"
    assert change["order"]["status"] == "completed"
    assert change["note_added"] is True


async def test_update_order_status_without_a_note_adds_none(client, store):
    result = await client.call_tool("update_order_status", {"order_id": 41, "status": "on-hold"})

    assert result.structured_content["note_added"] is False
    assert not [request for request in store.requests if request.url.path.endswith("/notes")]


async def test_an_unknown_status_is_rejected_before_calling_the_store(client, store):
    result = await client.call_tool("update_order_status", {"order_id": 41, "status": "shipped"})

    assert result.is_error
    assert not store.requests


async def test_an_unreachable_store_is_a_readable_tool_error(client, store):
    store.fail_with = httpx2.ConnectError("connection refused")

    result = await client.call_tool("list_products", {})

    assert result.is_error
    assert "The store could not be reached" in result.content[0].text


async def test_a_slow_store_is_reported_as_a_timeout(client, store):
    store.fail_with = httpx2.ReadTimeout("timed out")

    result = await client.call_tool("get_order", {"order_id": 41})

    assert result.is_error
    assert "did not answer within 15 seconds" in result.content[0].text


async def test_a_rejected_api_key_points_at_the_secret_not_at_the_caller(store):
    from mcp import Client

    from woo_mcp.server import build_server
    from woo_mcp.woocommerce import WooCommerce

    from .conftest import SETTINGS

    def reject(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/wp-json/":
            return store(request)
        return httpx2.Response(401, json={"code": "woocommerce_rest_authentication_error", "message": "Invalid signature"})

    woo = WooCommerce(SETTINGS, transport=httpx2.MockTransport(reject))
    async with Client(build_server(woo)) as client:
        result = await client.call_tool("list_products", {})

    assert result.is_error
    assert "rejected this server's API key" in result.content[0].text
