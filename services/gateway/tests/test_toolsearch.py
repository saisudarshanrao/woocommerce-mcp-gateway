"""Finding tools by words, and the two-tool MCP endpoint."""

import httpx2
import pytest
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

from gateway import repo
from gateway.toolsearch import ToolSearch, rank, words

from .conftest import add_server, add_user, principal_for
from .test_api import BASE, admin, app, browser, invite_member, mcp_client, new_key  # noqa: F401  (fixtures)

pytestmark = pytest.mark.anyio


def tool(name: str, description: str) -> types.Tool:
    return types.Tool(name=name, description=description, input_schema={"type": "object"})


CATALOGUE = [
    tool("woo__list_orders", "[WooCommerce store] List orders, newest first."),
    tool("woo__create_product", "[WooCommerce store] Create a product with a name and price."),
    tool("store-books__list_orders", "[Corner Books store] List orders, newest first."),
    tool("store-books__get_order", "[Corner Books store] One order with its line items."),
    tool("store-toys__list_orders", "[Toy Box store] List orders, newest first."),
    tool("tasks__add_task", "[Team tasks] Add a task to the shared list."),
]


def names(query: str, limit: int = 8) -> list[str]:
    return [found.name for found in rank(CATALOGUE, query, limit)]


def test_words_ignore_case_punctuation_filler_and_plurals():
    assert words("What are the ORDERS of store-books__list_orders?") == {"order", "store", "book", "list"}


def test_the_store_and_the_action_together_pick_out_one_tool():
    # Both of that store's order tools tie for first; the model reads the two and chooses.
    assert set(names("today's orders in the books store")[:2]) == {"store-books__list_orders", "store-books__get_order"}
    assert names("toys orders")[0] == "store-toys__list_orders"
    # Asking about orders in general returns every store's order tools, and nothing about tasks.
    assert set(names("orders")) == {
        "woo__list_orders", "store-books__list_orders", "store-books__get_order", "store-toys__list_orders",
    }


def test_a_word_in_the_name_outweighs_one_in_the_description_and_the_limit_is_kept():
    assert names("product")[0] == "woo__create_product"
    assert names("task", limit=1) == ["tasks__add_task"]
    assert names("weather forecast") == []


async def test_finding_and_calling_obey_the_same_rules_as_the_full_list(gateway, pool, manager):
    await add_server(pool, "shop", "shop.test", member_access="read_only")
    await add_server(pool, "warehouse", "warehouse.test", timeout_seconds=1)
    await manager.sync()
    search = ToolSearch(gateway)
    member = principal_for(await add_user(pool, "bob@example.com", "member"))

    found = (await search.call(member, "find_tools", {"query": "shop product orders"})).structured_content["tools"]
    # The member may only read from the shop, so its create_product is not found at all.
    assert [item["name"] for item in found] == ["shop__list_orders", "warehouse__list_orders"]
    assert found[0]["input_schema"]["type"] == "object"

    ok = await search.call(member, "call_tool", {"name": "shop__list_orders", "arguments": {}})
    assert not ok.is_error
    # Knowing a hidden tool's name does not help: the call is refused, and recorded.
    refused = await search.call(member, "call_tool", {"name": "shop__create_product", "arguments": {"name": "x"}})
    assert refused.is_error and "not allowed" in refused.content[0].text
    assert [(entry.tool, entry.outcome) for entry in await repo.list_audit(pool)] == [
        ("create_product", "denied"), ("list_orders", "ok"),
    ]

    nothing = (await search.call(member, "find_tools", {"query": "weather"})).structured_content
    assert nothing["tools"] == [] and nothing["servers"] == {"shop": 1, "warehouse": 2}
    assert (await search.call(member, "call_tool", {"name": 5})).is_error
    assert (await search.call(member, "shop__list_orders", {})).is_error


async def test_the_search_endpoint_lists_two_tools_and_reaches_all_the_others(app, admin):
    key = (await new_key(admin))["key"]
    async with mcp_client(app, key) as full:
        everything = [found.name for found in (await full.list_tools()).tools]
    assert len(everything) == 5

    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {key}"})
    async with http, Client(streamable_http_client(f"{BASE}/mcp/search", http_client=http)) as client:
        assert [found.name for found in (await client.list_tools()).tools] == ["find_tools", "call_tool"]
        found = (await client.call_tool("find_tools", {"query": "warehouse orders"})).structured_content["tools"]
        assert found[0]["name"] == "warehouse__list_orders"
        result = await client.call_tool("call_tool", {"name": found[0]["name"], "arguments": {}})
        assert not result.is_error and "pallet-9" in result.content[0].text

    # Without a key the search endpoint is as closed as the main one.
    async with browser(app) as anonymous:
        assert (await anonymous.post("/mcp/search", json={})).status_code == 401
        config = (await anonymous.get("/api/config")).json()
    assert config["mcp_search_url"] == f"{BASE}/mcp/search"
