"""Browser sign-in for MCP clients: the whole OAuth flow, driven by the MCP SDK's own client."""

from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from gateway import repo
from gateway.app import create_app
from gateway.config import Settings
from gateway.oauth import allowed_issuer

from .test_api import ADMIN, BASE, admin, app, browser, invite_member  # noqa: F401  (fixtures)

pytestmark = pytest.mark.anyio

CALLBACK = "http://localhost:53682/callback"


class Memory:
    """Where the client keeps its registration and tokens between requests."""

    def __init__(self):
        self.tokens = None
        self.client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


class Person:
    """Plays the human: follows the client's link in a browser that is signed in to the dashboard, and answers."""

    def __init__(self, app, session: httpx2.AsyncClient, answer: str = "approve"):
        self.app, self.session, self.answer = app, session, answer
        self.returned: dict[str, list[str]] = {}
        self.consent: dict = {}

    async def open(self, authorize_url: str) -> None:
        # /authorize puts the request aside and sends the browser to the consent page.
        sent_to = await self.session.get(authorize_url)
        assert sent_to.status_code == 302, sent_to.text
        consent_page = urlsplit(sent_to.headers["location"])
        assert consent_page.path == "/consent"
        request_id = parse_qs(consent_page.query)["request"][0]
        self.consent = (await self.session.get(f"/api/oauth/requests/{request_id}")).json()
        answered = await self.session.post(f"/api/oauth/requests/{request_id}/{self.answer}")
        assert answered.status_code == 200, answered.text
        self.returned = parse_qs(urlsplit(answered.json()["redirect"]).query)
        self.request_id = request_id

    async def callback(self) -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code=self.returned["code"][0], state=self.returned["state"][0])


def signing_in(app, person: Person, storage: Memory) -> httpx2.AsyncClient:
    """An HTTP client that, like Claude Code or Cursor, has only the gateway's URL."""
    auth = OAuthClientProvider(
        server_url=f"{BASE}/mcp",
        client_metadata=OAuthClientMetadata(client_name="Test Client", redirect_uris=[CALLBACK]),
        storage=storage,
        redirect_handler=person.open,
        callback_handler=person.callback,
    )
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), auth=auth)


async def test_a_client_with_only_the_url_signs_in_through_the_browser_and_acts_as_that_user(app, admin, pool):
    bob = await invite_member(app, admin)
    person, storage = Person(app, bob), Memory()

    async with signing_in(app, person, storage) as http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        assert "shop__list_orders" in tools
        assert not (await client.call_tool("shop__list_orders", {})).is_error

    # The person was shown who was asking before they said yes.
    assert person.consent == {"client_name": "Test Client", "returns_to": "localhost:53682"}
    # The client ended up with tokens of its own, and never held a password or an API key.
    assert storage.tokens.access_token.startswith("mgo_") and storage.tokens.refresh_token
    # The call is in the audit log under the person who approved it.
    (entry,) = await repo.list_audit(pool)
    assert (entry.user_email, entry.tool, entry.outcome) == ("bob@example.com", "list_orders", "ok")
    assert entry.api_key_prefix == "signed in: Test Client"
    # Nothing readable is stored: the database holds hashes of the tokens.
    stored = await pool.fetchrow("SELECT access_hash, refresh_hash FROM oauth_grants")
    assert storage.tokens.access_token not in stored.values() and storage.tokens.refresh_token not in stored.values()

    grants = (await bob.get("/api/oauth/grants")).json()
    assert [(grant["client_name"], grant["user_email"]) for grant in grants] == [("Test Client", "bob@example.com")]
    await bob.aclose()


async def test_a_request_without_a_credential_is_told_where_to_sign_in(app):
    async with browser(app) as anonymous:
        refused = await anonymous.post("/mcp", json={})
        assert refused.status_code == 401
        pointer = refused.headers["www-authenticate"]
        assert pointer == f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'

        resource = (await anonymous.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == f"{BASE}/mcp" and resource["authorization_servers"] == [BASE]

        server = (await anonymous.get("/.well-known/oauth-authorization-server")).json()
        # The issuer has to be the same string in both documents: clients compare them exactly.
        assert server["issuer"] == BASE
        assert server["authorization_endpoint"] == f"{BASE}/authorize" and server["token_endpoint"] == f"{BASE}/token"
        assert server["registration_endpoint"] == f"{BASE}/register"
        assert server["code_challenge_methods_supported"] == ["S256"]


async def test_revoking_a_sign_in_refuses_the_next_call(app, admin):
    bob = await invite_member(app, admin)
    person, storage = Person(app, bob), Memory()
    async with signing_in(app, person, storage) as http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
        await client.list_tools()
    token = {"Authorization": f"Bearer {storage.tokens.access_token}"}
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = token | {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}

    async with browser(app) as raw:
        assert (await raw.post("/mcp", json=listing, headers=headers)).status_code != 401
        # Another member cannot end Bob's sign-in; Bob, or an admin, can.
        (grant,) = (await admin.get("/api/oauth/grants?everyone=true")).json()
        assert (await bob.get("/api/oauth/grants?everyone=true")).status_code == 403
        assert (await admin.delete(f"/api/oauth/grants/{grant['id']}")).status_code == 204
        assert (await raw.post("/mcp", json=listing, headers=headers)).status_code == 401
    await bob.aclose()


async def test_an_access_token_expires_and_the_refresh_token_gets_a_new_one_once(app, admin, pool):
    bob = await invite_member(app, admin)
    person, storage = Person(app, bob), Memory()
    async with signing_in(app, person, storage) as http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
        await client.list_tools()
    first = storage.tokens
    client_id = storage.client_info.client_id
    await pool.execute("UPDATE oauth_grants SET access_expires_at = now() - interval '1 second'")

    async with browser(app) as raw:
        expired = await raw.post("/mcp", json={}, headers={"Authorization": f"Bearer {first.access_token}"})
        assert expired.status_code == 401
        form = {"grant_type": "refresh_token", "refresh_token": first.refresh_token, "client_id": client_id,
                "client_secret": storage.client_info.client_secret}
        renewed = await raw.post("/token", data=form)
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["access_token"] not in (first.access_token, "")
        # The old refresh token was replaced along with it, so it cannot be used again.
        assert (await raw.post("/token", data=form)).status_code == 400
    assert await pool.fetchval("SELECT count(*) FROM oauth_grants") == 1
    await bob.aclose()


async def test_saying_no_gives_the_client_nothing_and_a_code_works_only_once(app, admin, pool):
    bob = await invite_member(app, admin)
    refusing = Person(app, bob, answer="deny")
    storage = Memory()
    with pytest.raises(Exception):
        async with signing_in(app, refusing, storage) as http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            await client.list_tools()
    assert refusing.returned["error"] == ["access_denied"] and storage.tokens is None
    assert await pool.fetchval("SELECT count(*) FROM oauth_grants") == 0
    # The same consent request cannot be answered a second time, by anyone.
    assert (await bob.post(f"/api/oauth/requests/{refusing.request_id}/approve")).status_code == 404

    # Without a dashboard session there is no consent at all.
    async with browser(app) as anonymous:
        assert (await anonymous.post("/api/oauth/requests/anything/approve")).status_code == 401
    await bob.aclose()


def test_plain_http_is_accepted_only_for_this_machine():
    assert allowed_issuer("https://gateway.example.com")
    assert allowed_issuer("http://gateway.localhost") and allowed_issuer("http://localhost:8080")
    assert not allowed_issuer("http://gateway.example.com")


async def test_without_https_browser_sign_in_is_off_and_api_keys_still_work(pool, database_url, upstreams):
    settings = Settings(
        database_url=database_url, encryption_key="test-encryption-key", public_url="http://gateway.example.com",
        bootstrap_admin_email=ADMIN["email"], bootstrap_admin_password=ADMIN["password"], probe_interval_seconds=3600,
    )
    application = create_app(settings, http_client_factory=upstreams.http_client)
    async with application.router.lifespan_context(application):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application), base_url="http://gateway.example.com") as client:
            refused = await client.post("/mcp", json={})
            assert refused.status_code == 401 and refused.headers["www-authenticate"] == "Bearer"
            assert (await client.get("/.well-known/oauth-authorization-server")).status_code in (401, 404)
            assert (await client.get("/api/config")).json()["oauth_enabled"] is False
