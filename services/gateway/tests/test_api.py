"""The whole application over HTTP: the REST API, and the MCP endpoint with real clients."""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from gateway import repo, security, seed
from gateway.app import create_app
from gateway.config import Settings

from .conftest import CIPHER, add_server

pytestmark = pytest.mark.anyio

BASE = "http://gateway.localhost"
ADMIN = {"email": "admin@example.com", "password": "admin-password-for-tests"}


@pytest.fixture
async def app(pool, database_url, upstreams):
    # `pool` has already emptied the tables; the app opens its own connections.
    await add_server(pool, "shop", "shop.test")
    await add_server(pool, "warehouse", "warehouse.test", timeout_seconds=1)
    settings = Settings(
        database_url=database_url,
        encryption_key="test-encryption-key",
        public_url=BASE,
        bootstrap_admin_email=ADMIN["email"],
        bootstrap_admin_password=ADMIN["password"],
        probe_interval_seconds=3600,
        probe_timeout_seconds=2,
    )
    application = create_app(settings, http_client_factory=upstreams.http_client)
    async with application.router.lifespan_context(application):
        yield application


def browser(app) -> httpx2.AsyncClient:
    """An HTTP client that keeps cookies, like one browser profile."""
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def admin(app):
    async with browser(app) as client:
        assert (await client.post("/api/auth/login", json=ADMIN)).status_code == 200
        yield client


async def invite_member(app, admin, email="bob@example.com", role="member") -> httpx2.AsyncClient:
    """Runs the invitation flow and returns the new member's signed-in client."""
    invite = (await admin.post("/api/invites", json={"email": email, "role": role})).json()
    token = invite["link"].rsplit("/", 1)[1]
    client = browser(app)
    accepted = await client.post(f"/api/invites/{token}/accept", json={"name": "Bob", "password": "bobs-long-password"})
    assert accepted.status_code == 200, accepted.text
    return client


@pytest.fixture
async def member(app, admin):
    client = await invite_member(app, admin)
    yield client
    await client.aclose()


@asynccontextmanager
async def mcp_client(app, api_key: str, mode: str = "auto"):
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {api_key}"})
    async with http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode=mode) as client:
        yield client


async def new_key(client: httpx2.AsyncClient, name="laptop", **extra) -> dict:
    response = await client.post("/api/keys", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


# --- signing in --------------------------------------------------------------


async def test_the_api_requires_a_session(app):
    async with browser(app) as anonymous:
        assert (await anonymous.get("/api/servers")).status_code == 401
        assert (await anonymous.get("/api/auth/me")).status_code == 401


async def test_login_sets_an_http_only_cookie_and_wrong_passwords_are_refused(app):
    async with browser(app) as client:
        wrong = await client.post("/api/auth/login", json={**ADMIN, "password": "nope"})
        unknown = await client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()  # no hint about which emails exist

        ok = await client.post("/api/auth/login", json=ADMIN)
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert (await client.get("/api/auth/me")).json()["role"] == "admin"

        await client.post("/api/auth/logout")
        assert (await client.get("/api/auth/me")).status_code == 401


async def test_the_session_token_is_stored_only_as_a_hash(app, admin, pool):
    token = admin.cookies["gateway_session"]

    stored = [row["token_hash"] for row in await pool.fetch("SELECT token_hash FROM sessions")]

    assert stored == [security.hash_token(token)]


async def test_a_request_caused_by_another_site_is_refused(admin):
    forged = await admin.post("/api/keys", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    own_site = await admin.post("/api/keys", json={"name": "x"}, headers={"Origin": BASE})

    assert forged.status_code == 403
    assert own_site.status_code == 201


# --- team --------------------------------------------------------------------


async def test_an_invitation_link_creates_a_member_and_works_once(app, admin):
    invite = (await admin.post("/api/invites", json={"email": "Bob@Example.com"})).json()
    token = invite["link"].rsplit("/", 1)[1]
    assert invite["link"].startswith(f"{BASE}/invite/")

    async with browser(app) as bob:
        assert (await bob.get(f"/api/invites/{token}")).json() == {"email": "bob@example.com", "role": "member"}
        too_short = await bob.post(f"/api/invites/{token}/accept", json={"name": "Bob", "password": "short"})
        assert too_short.status_code == 422
        accepted = await bob.post(f"/api/invites/{token}/accept", json={"name": "Bob", "password": "bobs-long-password"})
        assert accepted.json()["role"] == "member"
        assert (await bob.get("/api/auth/me")).json()["email"] == "bob@example.com"

    async with browser(app) as someone_else:
        assert (await someone_else.get(f"/api/invites/{token}")).status_code == 404
        again = await someone_else.post(f"/api/invites/{token}/accept", json={"name": "Eve", "password": "eves-long-password"})
        assert again.status_code == 404


async def test_members_cannot_manage_the_team_or_the_servers(member):
    assert (await member.get("/api/members")).status_code == 403
    assert (await member.post("/api/invites", json={"email": "x@example.com"})).status_code == 403
    assert (await member.post("/api/servers", json={"slug": "x", "name": "X", "url": "http://x.test/mcp"})).status_code == 403
    servers = (await member.get("/api/servers")).json()
    assert (await member.delete(f"/api/servers/{servers[0]['id']}")).status_code == 403
    assert (await member.patch(f"/api/servers/{servers[0]['id']}", json={"enabled": False})).status_code == 403


async def test_the_last_admin_cannot_be_demoted_or_deactivated(admin):
    me = (await admin.get("/api/auth/me")).json()

    assert (await admin.patch(f"/api/members/{me['id']}", json={"role": "member"})).status_code == 409
    assert (await admin.patch(f"/api/members/{me['id']}", json={"active": False})).status_code == 409


async def test_removing_a_member_ends_their_access_but_keeps_their_audit_history(app, admin, member):
    api_key = (await new_key(member))["key"]
    async with mcp_client(app, api_key) as client:
        await client.call_tool("shop__list_orders", {})
    bob = (await member.get("/api/auth/me")).json()
    me = (await admin.get("/api/auth/me")).json()

    assert (await admin.delete(f"/api/members/{me['id']}")).status_code == 409  # not yourself
    assert (await admin.delete(f"/api/members/{bob['id']}")).status_code == 204

    assert (await member.get("/api/auth/me")).status_code == 401
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        refused = await http.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {api_key}"}
        )
    assert refused.status_code == 401
    assert [m["email"] for m in (await admin.get("/api/members")).json()["members"]] == ["admin@example.com"]
    assert (await admin.get("/api/audit")).json()[0]["user_email"] == "bob@example.com"


# --- API keys ----------------------------------------------------------------


async def test_a_key_is_shown_once_and_only_its_hash_is_stored(member, pool):
    created = await new_key(member)
    api_key = created["key"]
    assert api_key.startswith("mgw_") and created["prefix"] == api_key[:12]

    listed = (await member.get("/api/keys")).json()
    assert [key["prefix"] for key in listed] == [created["prefix"]]
    assert "key" not in listed[0]

    row = await pool.fetchrow("SELECT * FROM api_keys")
    assert row["key_hash"] == security.hash_token(api_key)
    assert api_key not in json.dumps({key: str(value) for key, value in row.items()})


async def test_one_url_and_one_key_give_the_tools_of_every_server(app, member):
    api_key = (await new_key(member))["key"]

    async with mcp_client(app, api_key) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        from_shop = await client.call_tool("shop__list_orders", {})
        from_warehouse = await client.call_tool("warehouse__list_orders", {})

    assert names == [
        "shop__broken", "shop__create_product", "shop__list_orders", "warehouse__list_orders", "warehouse__slow",
    ]
    assert from_shop.structured_content == {"result": ["order-1", "order-2"]}
    assert from_warehouse.structured_content == {"result": ["pallet-9"]}


async def test_a_client_that_speaks_the_older_protocol_works_too(app, member):
    api_key = (await new_key(member))["key"]

    async with mcp_client(app, api_key, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        assert len((await client.list_tools()).tools) == 5
        assert not (await client.call_tool("shop__list_orders", {})).is_error


async def test_the_mcp_endpoint_refuses_missing_and_unknown_keys(app):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await http.post("/mcp", json=body)).status_code == 401
        assert (await http.post("/mcp", json=body, headers={"Authorization": "Bearer mgw_made-up"})).status_code == 401
        # A dashboard session is not an API key.
        assert (await http.post("/mcp", json=body, headers={"Cookie": "gateway_session=x"})).status_code == 401


async def test_a_revoked_key_is_refused_on_the_next_call_and_the_attempt_is_audited(app, admin, member):
    created = await new_key(member)
    async with mcp_client(app, created["key"]) as client:
        assert not (await client.call_tool("shop__list_orders", {})).is_error

    assert (await member.delete(f"/api/keys/{created['id']}")).status_code == 204

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        refused = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "shop__list_orders"}},
            headers={"Authorization": f"Bearer {created['key']}", "Mcp-Name": "shop__list_orders"},
        )
    assert refused.status_code == 401
    assert refused.json()["message"] == "This API key has been revoked."

    newest, previous = (await admin.get("/api/audit")).json()[:2]
    assert (newest["user_email"], newest["outcome"], newest["error"]) == (
        "bob@example.com", "denied", "This API key has been revoked.",
    )
    assert newest["tool"] == "shop__list_orders" and newest["api_key_prefix"] == created["prefix"]
    assert (previous["user_email"], previous["server"], previous["tool"], previous["outcome"]) == (
        "bob@example.com", "shop", "list_orders", "ok",
    )


async def test_an_admin_can_issue_and_revoke_a_members_key(app, admin, member):
    bob = (await member.get("/api/auth/me")).json()

    issued = await new_key(admin, name="for bob", user_id=bob["id"])
    assert issued["user_email"] == "bob@example.com"
    async with mcp_client(app, issued["key"]) as client:
        await client.call_tool("shop__list_orders", {})
    assert (await admin.get("/api/audit")).json()[0]["user_email"] == "bob@example.com"

    everyone = (await admin.get("/api/keys", params={"everyone": "true"})).json()
    assert [key["user_email"] for key in everyone] == ["bob@example.com"]
    assert (await member.get("/api/keys", params={"everyone": "true"})).status_code == 403
    assert (await admin.delete(f"/api/keys/{issued['id']}")).status_code == 204


async def test_a_member_cannot_revoke_someone_elses_key(admin, member):
    admins_key = await new_key(admin)

    assert (await member.delete(f"/api/keys/{admins_key['id']}")).status_code == 404


async def test_deactivating_a_member_ends_their_session_and_their_keys(app, admin, member):
    api_key = (await new_key(member))["key"]
    bob = (await member.get("/api/auth/me")).json()

    assert (await admin.patch(f"/api/members/{bob['id']}", json={"active": False})).status_code == 200

    assert (await member.get("/api/auth/me")).status_code == 401
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        refused = await http.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {api_key}"}
        )
    assert refused.status_code == 401 and refused.json()["message"] == "This account has been deactivated."


# --- servers -----------------------------------------------------------------


async def test_servers_are_listed_with_status_and_tools(admin, member):
    seen = {}
    for who, client in (("admin", admin), ("member", member)):
        servers = {server["slug"]: server for server in (await client.get("/api/servers")).json()}
        assert servers["shop"]["status"] == "connected"
        assert [tool["name"] for tool in servers["shop"]["tools"]] == [
            "shop__broken", "shop__create_product", "shop__list_orders",
        ]
        assert servers["warehouse"]["skipped_tools"][0]["name"] == "not.portable"
        seen[who] = servers["shop"]

    # Where a server lives and how it is authenticated is for admins only.
    assert seen["admin"]["url"] == "http://shop.test/mcp" and "auth" in seen["admin"]
    assert "url" not in seen["member"] and "auth" not in seen["member"]


async def test_an_upstream_credential_goes_in_and_never_comes_out(app, admin, member, pool, upstreams):
    secret = "upstream-secret-value-123"
    created = await admin.post(
        "/api/servers",
        json={"slug": "extra", "name": "Extra", "url": "http://shop.test/mcp", "auth_type": "bearer", "credential": secret},
    )
    assert created.status_code == 201, created.text
    assert created.json()["auth"] == {"type": "bearer", "header": None, "credential_set": True}
    assert created.json()["status"] == "connected"
    assert upstreams.seen_headers["shop.test"] == {"Authorization": f"Bearer {secret}"}

    for client in (admin, member):
        assert secret not in (await client.get("/api/servers")).text
    assert secret not in created.text

    stored = await pool.fetchval("SELECT auth_secret FROM upstream_servers WHERE slug = 'extra'")
    assert secret.encode() not in stored
    assert CIPHER.decrypt(stored) == secret

    # A member's MCP client sees the tools, and nothing about how they are reached.
    api_key = (await new_key(member))["key"]
    async with mcp_client(app, api_key) as client:
        listing = (await client.list_tools()).model_dump_json()
    assert "extra__list_orders" in listing and secret not in listing and "shop.test" not in listing


async def test_editing_a_server_keeps_its_credential_unless_a_new_one_is_given(admin, pool, upstreams):
    created = (await admin.post(
        "/api/servers",
        json={"slug": "extra", "name": "Extra", "url": "http://shop.test/mcp", "auth_type": "header",
              "auth_header": "X-API-Key", "credential": "first"},
    )).json()

    renamed = (await admin.patch(f"/api/servers/{created['id']}", json={"name": "Renamed", "slug": "other"})).json()
    assert (renamed["name"], renamed["slug"]) == ("Renamed", "extra")  # the slug cannot change
    assert upstreams.seen_headers["shop.test"] == {"X-API-Key": "first"}

    await admin.patch(f"/api/servers/{created['id']}", json={"credential": "second"})
    assert upstreams.seen_headers["shop.test"] == {"X-API-Key": "second"}

    cleared = (await admin.patch(f"/api/servers/{created['id']}", json={"auth_type": "none"})).json()
    assert cleared["auth"] == {"type": "none", "header": None, "credential_set": False}
    assert upstreams.seen_headers["shop.test"] == {}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"slug": "Bad_Slug", "name": "X", "url": "http://x.test/mcp"}, 422),
        ({"slug": "shop", "name": "X", "url": "http://x.test/mcp"}, 409),
        ({"slug": "x", "name": "X", "url": "ftp://x.test/mcp"}, 422),
        ({"slug": "x", "name": "X", "url": "http://x.test/mcp", "auth_type": "bearer"}, 422),
        ({"slug": "x", "name": "X", "url": "http://x.test/mcp", "auth_type": "header", "credential": "c"}, 422),
    ],
)
async def test_bad_server_definitions_are_rejected(admin, body, status):
    assert (await admin.post("/api/servers", json=body)).status_code == status


async def test_a_server_that_cannot_be_reached_is_registered_and_shown_as_unreachable(admin):
    created = await admin.post("/api/servers", json={"slug": "ghost", "name": "Ghost", "url": "http://ghost.test/mcp"})

    assert created.status_code == 201
    assert created.json()["status"] == "unreachable"
    assert created.json()["error"] == "the connection was refused (is the server running?)"


async def test_disabling_a_server_hides_its_tools_from_clients_at_once(app, admin, member):
    api_key = (await new_key(member))["key"]
    shop = next(server for server in (await admin.get("/api/servers")).json() if server["slug"] == "shop")

    disabled = (await admin.patch(f"/api/servers/{shop['id']}", json={"enabled": False})).json()
    assert disabled["status"] == "disabled" and disabled["tools"] == []

    async with mcp_client(app, api_key) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        refused = await client.call_tool("shop__list_orders", {})
    assert names == ["warehouse__list_orders", "warehouse__slow"]
    assert refused.is_error and refused.content[0].text == "The server 'Shop' is disabled."

    await admin.patch(f"/api/servers/{shop['id']}", json={"enabled": True})
    async with mcp_client(app, api_key) as client:
        assert "shop__list_orders" in [tool.name for tool in (await client.list_tools()).tools]


async def test_removing_a_server_removes_it_everywhere(app, admin, member):
    api_key = (await new_key(member))["key"]
    shop = next(server for server in (await admin.get("/api/servers")).json() if server["slug"] == "shop")

    assert (await admin.delete(f"/api/servers/{shop['id']}")).status_code == 204

    assert [server["slug"] for server in (await admin.get("/api/servers")).json()] == ["warehouse"]
    async with mcp_client(app, api_key) as client:
        assert [tool.name for tool in (await client.list_tools()).tools] == ["warehouse__list_orders", "warehouse__slow"]


# --- audit -------------------------------------------------------------------


async def test_admins_see_every_call_and_members_only_their_own(app, admin, member):
    async with mcp_client(app, (await new_key(admin))["key"]) as client:
        await client.call_tool("shop__list_orders", {})
    async with mcp_client(app, (await new_key(member))["key"]) as client:
        await client.call_tool("warehouse__list_orders", {})
        await client.call_tool("shop__broken", {})

    everything = (await admin.get("/api/audit")).json()
    assert [(entry["user_email"], entry["server"], entry["tool"], entry["outcome"]) for entry in everything] == [
        ("bob@example.com", "shop", "broken", "error"),
        ("bob@example.com", "warehouse", "list_orders", "ok"),
        ("admin@example.com", "shop", "list_orders", "ok"),
    ]
    assert all(entry["source"] == "mcp" and entry["duration_ms"] >= 0 and entry["at"] for entry in everything)

    mine = (await member.get("/api/audit")).json()
    assert {entry["user_email"] for entry in mine} == {"bob@example.com"}
    admin_id = (await admin.get("/api/auth/me")).json()["id"]
    assert {e["user_email"] for e in (await member.get("/api/audit", params={"user_id": admin_id})).json()} == {
        "bob@example.com"
    }

    failures = (await admin.get("/api/audit", params={"outcome": "error"})).json()
    assert [entry["tool"] for entry in failures] == ["broken"]
    assert [e["tool"] for e in (await admin.get("/api/audit", params={"server": "warehouse"})).json()] == ["list_orders"]


# --- starting up -------------------------------------------------------------


async def test_health_endpoints_and_public_config(app):
    async with browser(app) as client:
        assert (await client.get("/healthz")).text == "ok"
        assert (await client.get("/readyz")).text == "ok"
        config = (await client.get("/api/config")).json()
        assert config["public_url"] == BASE and config["mcp_url"] == f"{BASE}/mcp"
        assert config["chat_enabled"] is False and config["chat_model"] == ""


async def test_seed_servers_are_registered_once_and_stay_removed_if_removed(pool, tmp_path):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([
        {"slug": "woo", "name": "WooCommerce store", "url": "http://woo.test/mcp", "auth_type": "bearer",
         "credential": "seed-token"},
    ]))
    settings = Settings(database_url="unused", encryption_key="k", public_url=BASE, seed_servers_file=str(seed_file))

    await seed.seed_servers(pool, CIPHER, settings)
    await seed.seed_servers(pool, CIPHER, settings)
    woo = await repo.get_server_by_slug(pool, "woo")
    assert woo.name == "WooCommerce store" and CIPHER.decrypt(woo.auth_secret) == "seed-token"
    assert len(await repo.list_servers(pool)) == 1

    await repo.delete_server(pool, woo.id)
    await seed.seed_servers(pool, CIPHER, settings)
    assert await repo.list_servers(pool) == []


async def test_the_first_admin_is_created_only_when_there_are_no_users(pool):
    settings = Settings(
        database_url="unused", encryption_key="k", public_url=BASE,
        bootstrap_admin_email="first@example.com", bootstrap_admin_password="a-long-password",
    )

    await seed.bootstrap_admin(pool, settings)
    await seed.bootstrap_admin(pool, settings)

    users = await repo.list_users(pool)
    assert [(user.email, user.role) for user in users] == [("first@example.com", "admin")]


async def test_a_server_url_pointing_inside_the_cluster_or_at_metadata_is_refused(app, admin):
    new = {"slug": "evil", "name": "Evil", "url": "http://169.254.169.254/latest/meta-data/"}
    refused = await admin.post("/api/servers", json=new)
    assert refused.status_code == 422
    assert "link-local" in refused.json()["detail"]

    for url in ("http://127.0.0.1:5432/", "https://10.43.0.1/", "http://user:secret@shop.test/mcp"):
        assert (await admin.post("/api/servers", json=new | {"url": url})).status_code == 422, url
    assert all(server["slug"] != "evil" for server in (await admin.get("/api/servers")).json())

    # Editing an existing server is held to the same rule.
    shop = next(server for server in (await admin.get("/api/servers")).json() if server["slug"] == "shop")
    edited = await admin.patch(f"/api/servers/{shop['id']}", json={"url": "http://[::1]/mcp"})
    assert edited.status_code == 422
    assert "loopback" in edited.json()["detail"]


async def test_metrics_are_served_for_prometheus_and_statistics_for_the_dashboard(app, admin):
    key = (await admin.post("/api/keys", json={"name": "metrics"})).json()["key"]
    async with mcp_client(app, key) as client:
        await client.call_tool("shop__list_orders", {})

    async with browser(app) as anonymous:
        scraped = await anonymous.get("/metrics")
    assert scraped.status_code == 200
    assert 'gateway_tool_calls_total{outcome="ok",server="shop",source="mcp"} 1.0' in scraped.text
    assert 'gateway_upstream_up{server="warehouse"} 1.0' in scraped.text

    stats = (await admin.get("/api/stats")).json()
    assert stats == [{"server": "shop", "calls": 1, "failures": 0, "median_ms": stats[0]["median_ms"], "p95_ms": stats[0]["p95_ms"]}]
