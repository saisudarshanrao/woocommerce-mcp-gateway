import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from starlette.responses import PlainTextResponse

from demo_mcp import sandbox, tasks
from demo_mcp.__main__ import StaticCredential, build_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_tasks_can_be_added_listed_and_completed():
    async with Client(tasks.build(), raise_exceptions=True) as client:
        added = (await client.call_tool("add_task", {"title": "Pack order 7"})).structured_content
        await client.call_tool("add_task", {"title": "Pack order 8", "notes": "fragile"})
        assert added["id"] == 1 and added["done"] is False

        done = (await client.call_tool("complete_task", {"task_id": 1})).structured_content
        assert done["done"] is True

        still_open = (await client.call_tool("list_tasks", {})).structured_content["result"]
        everything = (await client.call_tool("list_tasks", {"show": "all"})).structured_content["result"]
        assert [task["title"] for task in still_open] == ["Pack order 8"]
        assert len(everything) == 2

        missing = await client.call_tool("complete_task", {"task_id": 99})
        assert missing.is_error and "There is no task 99." in missing.content[0].text


async def test_each_new_tasks_server_starts_empty():
    async with Client(tasks.build()) as client:
        assert (await client.call_tool("list_tasks", {"show": "all"})).structured_content == {"result": []}


async def test_the_sandbox_echoes_fails_and_waits():
    async with Client(sandbox.build(), raise_exceptions=True) as client:
        assert (await client.call_tool("echo", {"text": "hi"})).structured_content == {"result": "hi"}
        failed = await client.call_tool("fail", {"message": "on purpose"})
        assert failed.is_error and "on purpose" in failed.content[0].text
        waited = await client.call_tool("slow", {"seconds": 0.01})
        assert waited.structured_content == {"result": "answered after 0.01 seconds"}


async def test_both_servers_have_a_tool_with_the_same_name():
    async with Client(tasks.build()) as one, Client(sandbox.build()) as other:
        shared = {t.name for t in (await one.list_tools()).tools} & {t.name for t in (await other.list_tools()).tools}
    assert shared == {"server_info"}


async def reached(scope, receive, send):
    await PlainTextResponse("reached")(scope, receive, send)


@pytest.mark.parametrize(
    ("header", "good", "bad"),
    [
        ("Authorization", {"Authorization": "Bearer s3cret"}, {"Authorization": "Bearer nope"}),
        ("Authorization", {"Authorization": "bearer s3cret"}, {"Authorization": "s3cret"}),
        ("X-API-Key", {"X-API-Key": "s3cret"}, {"Authorization": "Bearer s3cret"}),
    ],
)
async def test_a_configured_credential_is_required_except_for_the_health_check(header, good, bad):
    guarded = StaticCredential(reached, header, "s3cret")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=guarded), base_url="http://localhost") as http:
        assert (await http.get("/healthz")).text == "reached"
        assert (await http.post("/mcp")).status_code == 401
        assert (await http.post("/mcp", headers=bad)).status_code == 401
        assert (await http.post("/mcp", headers=good)).text == "reached"


async def test_the_assembled_app_serves_health_and_guards_mcp_when_a_token_is_set():
    app = build_app("tasks", {"AUTH_TOKEN": "s3cret", "AUTH_HEADER": "X-API-Key"})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost") as http:
        assert (await http.get("/healthz")).text == "ok"
        assert (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 401
