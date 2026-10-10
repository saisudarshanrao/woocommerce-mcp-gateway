"""The gateway core against two fake upstream servers and a real database."""

import pytest
from mcp import types

from gateway import repo

from .conftest import add_server, add_user, principal_for

pytestmark = pytest.mark.anyio


def text_of(result: types.CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, types.TextContent))


@pytest.fixture
async def two_servers(pool, manager):
    shop = await add_server(pool, "shop", "shop.test")
    warehouse = await add_server(pool, "warehouse", "warehouse.test", timeout_seconds=1)
    await manager.sync()
    return shop, warehouse


@pytest.fixture
async def alice(pool):
    return principal_for(await add_user(pool, "alice@example.com", "admin"))


@pytest.fixture
async def bob(pool):
    return principal_for(await add_user(pool, "bob@example.com", "member"))


async def test_tools_of_every_server_are_listed_under_prefixed_names(gateway, two_servers, alice):
    names = [tool.name for tool in await gateway.list_tools(alice)]

    assert names == [
        "shop__broken",
        "shop__create_product",
        "shop__list_orders",
        "warehouse__list_orders",
        "warehouse__slow",
    ]


async def test_two_servers_can_share_a_tool_name_and_each_call_reaches_its_own(gateway, two_servers, alice):
    tools = {tool.name: tool for tool in await gateway.list_tools(alice)}
    assert tools["shop__list_orders"].description == "[Shop] Orders in the shop."
    assert tools["warehouse__list_orders"].description == "[Warehouse] Orders waiting in the warehouse."

    from_shop = await gateway.call_tool(alice, "shop__list_orders", {})
    from_warehouse = await gateway.call_tool(alice, "warehouse__list_orders", {})

    assert from_shop.structured_content == {"result": ["order-1", "order-2"]}
    assert from_warehouse.structured_content == {"result": ["pallet-9"]}


async def test_a_tool_whose_name_cannot_be_exposed_is_left_out_and_reported(manager, two_servers):
    warehouse = manager.state(two_servers[1].id)

    assert "not.portable" not in warehouse.tools
    assert warehouse.skipped == [
        {"name": "not.portable", "reason": "its name has characters other than letters, digits, _ and -"}
    ]


async def test_arguments_reach_the_upstream_and_its_result_comes_back(gateway, two_servers, alice):
    result = await gateway.call_tool(alice, "shop__create_product", {"name": "Hat"})

    assert not result.is_error
    assert text_of(result) == "created Hat"


async def test_every_call_is_audited_with_user_server_tool_and_outcome(gateway, pool, two_servers, alice, bob):
    await gateway.call_tool(alice, "shop__list_orders", {})
    await gateway.call_tool(bob, "shop__broken", {})

    newest, oldest = await repo.list_audit(pool)
    assert (oldest.user_email, oldest.server_slug, oldest.tool, oldest.outcome) == (
        "alice@example.com", "shop", "list_orders", "ok",
    )
    assert (newest.user_email, newest.server_slug, newest.tool, newest.outcome) == (
        "bob@example.com", "shop", "broken", "error",
    )
    assert "the shop is closed" in newest.error
    assert oldest.source == "mcp" and oldest.duration_ms >= 0


async def test_a_tool_that_fails_upstream_is_passed_back_as_a_failure(gateway, two_servers, alice):
    result = await gateway.call_tool(alice, "shop__broken", {})

    assert result.is_error
    assert "the shop is closed" in text_of(result)


async def test_a_slow_tool_times_out_with_a_clear_message(gateway, pool, two_servers, alice):
    result = await gateway.call_tool(alice, "warehouse__slow", {"seconds": 10})

    assert result.is_error
    assert text_of(result) == "Warehouse did not answer within 1 seconds."
    entry = (await repo.list_audit(pool))[0]
    assert entry.outcome == "timeout" and 900 <= entry.duration_ms < 3000


async def test_a_slow_call_does_not_hold_up_other_servers(gateway, two_servers, alice):
    import asyncio
    import time

    started = time.perf_counter()
    slow, quick = await asyncio.gather(
        gateway.call_tool(alice, "warehouse__slow", {"seconds": 10}),
        gateway.call_tool(alice, "shop__list_orders", {}),
    )

    assert slow.is_error and not quick.is_error
    assert time.perf_counter() - started < 3


async def test_a_dead_server_is_marked_unreachable_and_the_others_keep_working(
    gateway, manager, upstreams, pool, two_servers, alice
):
    shop, warehouse = two_servers
    upstreams.down.add("warehouse.test")
    await manager.sync()

    assert manager.state(warehouse.id).status == "unreachable"
    assert manager.state(warehouse.id).error == "the connection was refused (is the server running?)"
    assert manager.state(shop.id).status == "connected"

    names = [tool.name for tool in await gateway.list_tools(alice)]
    assert names == ["shop__broken", "shop__create_product", "shop__list_orders"]
    assert not (await gateway.call_tool(alice, "shop__list_orders", {})).is_error

    refused = await gateway.call_tool(alice, "warehouse__list_orders", {})
    assert refused.is_error
    assert text_of(refused) == "Warehouse is unreachable: the connection was refused (is the server running?)."
    assert (await repo.list_audit(pool))[0].outcome == "unreachable"


async def test_a_server_that_dies_between_probes_is_noticed_by_the_first_failed_call(
    gateway, manager, upstreams, two_servers, alice
):
    warehouse = two_servers[1]
    upstreams.down.add("warehouse.test")  # no probe has run since

    result = await gateway.call_tool(alice, "warehouse__list_orders", {})

    assert result.is_error and "is unreachable" in text_of(result)
    assert manager.state(warehouse.id).status == "unreachable"


async def test_a_server_that_comes_back_is_connected_again_after_the_next_probe(
    gateway, manager, upstreams, two_servers, alice
):
    upstreams.down.add("warehouse.test")
    await manager.sync()
    upstreams.down.clear()
    await manager.sync()

    assert manager.state(two_servers[1].id).status == "connected"
    assert not (await gateway.call_tool(alice, "warehouse__list_orders", {})).is_error


async def test_disabling_a_server_removes_its_tools_at_once_and_refuses_calls(gateway, pool, two_servers, alice):
    shop = two_servers[0]
    await repo.update_server(pool, shop.id, {"enabled": False})  # no probe has run since

    names = [tool.name for tool in await gateway.list_tools(alice)]
    result = await gateway.call_tool(alice, "shop__list_orders", {})

    assert names == ["warehouse__list_orders", "warehouse__slow"]
    assert result.is_error and text_of(result) == "The server 'Shop' is disabled."
    assert (await repo.list_audit(pool))[0].outcome == "denied"


async def test_removing_a_server_removes_its_tools(gateway, manager, pool, two_servers, alice):
    await repo.delete_server(pool, two_servers[0].id)

    assert [tool.name for tool in await gateway.list_tools(alice)] == ["warehouse__list_orders", "warehouse__slow"]
    result = await gateway.call_tool(alice, "shop__list_orders", {})
    assert text_of(result) == "There is no server 'shop'."


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("list_orders", "There is no tool called 'list_orders'. Tool names look like <server>__<tool>."),
        ("nowhere__list_orders", "There is no server 'nowhere'."),
        ("shop__nothing", "Shop has no tool called 'nothing'."),
    ],
)
async def test_unknown_tools_are_explained_and_still_audited(gateway, pool, two_servers, alice, name, message):
    result = await gateway.call_tool(alice, name, {})

    assert result.is_error and text_of(result) == message
    assert len(await repo.list_audit(pool)) == 1


async def test_members_can_be_limited_to_read_only_tools(gateway, pool, manager, two_servers, alice, bob):
    await repo.update_server(pool, two_servers[0].id, {"member_access": "read_only"})
    await manager.sync()

    assert "shop__create_product" in [tool.name for tool in await gateway.list_tools(alice)]
    member_tools = [tool.name for tool in await gateway.list_tools(bob)]
    assert "shop__list_orders" in member_tools and "shop__create_product" not in member_tools

    # Hidden from the list, and also refused if called directly.
    result = await gateway.call_tool(bob, "shop__create_product", {"name": "Hat"})
    assert result.is_error and text_of(result) == "Your role is not allowed to use 'shop__create_product'."
    assert (await repo.list_audit(pool))[0].outcome == "denied"
    assert not (await gateway.call_tool(alice, "shop__create_product", {"name": "Hat"})).is_error


async def test_the_stored_credential_is_sent_upstream_as_configured(manager, upstreams, pool):
    from .conftest import CIPHER

    await add_server(pool, "shop", "shop.test", auth_type="bearer", auth_secret=CIPHER.encrypt("shop-token"))
    await add_server(
        pool, "warehouse", "warehouse.test", auth_type="header", auth_header="X-API-Key",
        auth_secret=CIPHER.encrypt("warehouse-key"),
    )
    await manager.sync()

    assert upstreams.seen_headers["shop.test"] == {"Authorization": "Bearer shop-token"}
    assert upstreams.seen_headers["warehouse.test"] == {"X-API-Key": "warehouse-key"}
