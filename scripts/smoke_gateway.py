#!/usr/bin/env python3
"""End-to-end check of the gateway, as two different users, from outside the cluster.

It walks the "external MCP client" and "team and control" parts of the
definition of done:

    an admin registers two more servers by URL and credential
    a member joins by invitation and creates an API key
    with only the gateway URL and that key, an MCP client sees every server's tools
    one task uses tools from two servers
    a slow upstream times out without holding up the others
    a dead upstream is shown as unreachable while the others keep working
    a disabled server's tools disappear
    a revoked key is refused on the next call
    every one of those calls is in the audit log under the right user

Everything goes through the Ingress, exactly as a browser and an MCP client
would. The member, the keys and the servers the script adds are removed again
afterwards unless --keep is given. Standard library only.
"""

import argparse
import base64
import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from smoke_mcp import McpClient
from smoke_store import Failure, https_handler, ok

MCP_NAMESPACE = "mcp-servers"


class Dashboard:
    """The REST API as one signed-in browser would use it: it keeps the session cookie."""

    def __init__(self, base_url):
        self.base_url = base_url
        self._opener = urllib.request.build_opener(
            https_handler(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def landing(self, url):
        """Opens a link as this browser would, following redirects, and returns where it ends up."""
        with self._opener.open(url, timeout=40) as response:
            return response.geturl()

    def call(self, method, path, body=None, expect=(200, 201, 204)):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=40) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        payload = json.loads(raw) if raw else None
        if status not in expect:
            raise Failure(f"{method} {path}: HTTP {status}: {payload}")
        return payload


def post_form(url, fields):
    """POSTs a form, as OAuth's token endpoint expects, and returns (status, parsed JSON)."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def sign_in_with_browser(base_url, person, client_name):
    """What an MCP client does when given only the gateway's URL, with `person` playing the browser.

    Returns the tokens it ends up with. Every step uses only what the previous
    one returned, starting from the 401.
    """
    req = urllib.request.Request(f"{base_url}/mcp", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        raise Failure("the MCP endpoint answered without a credential")
    except urllib.error.HTTPError as error:
        # Python forgets `error` when this block ends, so keep what is needed from it.
        status, challenge = error.code, error.headers.get("WWW-Authenticate", "")
    if status != 401 or 'resource_metadata="' not in challenge:
        raise Failure(f"no pointer to sign-in in the 401: {challenge!r}")
    with urllib.request.urlopen(challenge.split('resource_metadata="')[1].rstrip('"'), timeout=20) as response:
        resource = json.loads(response.read())
    issuer = resource["authorization_servers"][0]
    with urllib.request.urlopen(f"{issuer}/.well-known/oauth-authorization-server", timeout=20) as response:
        server = json.loads(response.read())

    callback = "http://localhost:53682/callback"
    registered = json.loads(urllib.request.urlopen(urllib.request.Request(
        server["registration_endpoint"], method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"client_name": client_name, "redirect_uris": [callback],
                         "token_endpoint_auth_method": "none"}).encode()), timeout=20).read())

    # PKCE: the client keeps a random secret and shows only its hash to the browser step.
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": registered["client_id"], "redirect_uri": callback, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource["resource"],
    })
    # The person's browser follows the link, lands on the consent page, and they click Allow.
    consent = urllib.parse.urlsplit(person.landing(f"{server['authorization_endpoint']}?{query}"))
    request_id = urllib.parse.parse_qs(consent.query)["request"][0]
    shown = person.call("GET", f"/api/oauth/requests/{request_id}")
    back = urllib.parse.parse_qs(urllib.parse.urlsplit(
        person.call("POST", f"/api/oauth/requests/{request_id}/approve")["redirect"]).query)
    if back["state"] != [state]:
        raise Failure("the state did not come back unchanged")

    exchange = {"grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": callback,
                "client_id": registered["client_id"]}
    status, _ = post_form(server["token_endpoint"], exchange | {"code_verifier": secrets.token_urlsafe(48)})
    if status == 200:
        raise Failure("the token endpoint accepted a code without the secret behind its challenge")
    status, tokens = post_form(server["token_endpoint"], exchange | {"code_verifier": verifier})
    if status != 200:
        raise Failure(f"the code was not exchanged for tokens: HTTP {status} {tokens}")
    status, _ = post_form(server["token_endpoint"], exchange | {"code_verifier": verifier})
    if status == 200:
        raise Failure("the same code was accepted twice")
    return shown, tokens


def kubectl(args, *command):
    result = subprocess.run(["kubectl", "--context", args.context, *command], capture_output=True, text=True)
    if result.returncode != 0:
        raise Failure(f"kubectl {' '.join(command)}: {result.stderr.strip()}")
    return result.stdout


def secret(args, namespace, name, key):
    encoded = kubectl(args, "-n", namespace, "get", "secret", name, "-o", f"go-template={{{{index .data \"{key}\"}}}}")
    return base64.b64decode(encoded).decode()


def wait_for(description, check, seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(1)
    raise Failure(f"timed out after {seconds}s waiting until {description}")


def server_named(admin, slug):
    return next((s for s in admin.call("GET", "/api/servers") if s["slug"] == slug), None)


def tool_names(mcp):
    return [tool["name"] for tool in mcp.rpc("tools/list")["tools"]]


def raw_status(url, api_key, body):
    """The HTTP status of one MCP request, without the client's error handling."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", "Mcp-Name": body.get("params", {}).get("name", "")},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def run(args):
    mcp_url = f"{args.url}/mcp"
    added_servers, member_id = [], None
    admin = Dashboard(args.url)

    print(f"\n== the admin signs in at {args.url}")
    admin.call("POST", "/api/auth/login", {
        "email": args.admin_email,
        "password": secret(args, args.namespace, f"{args.release}-secrets", "admin-password"),
    })
    ok(f"signed in as {admin.call('GET', '/api/auth/me')['email']}")

    try:
        woo = wait_for("the store's server is connected", lambda: (s := server_named(admin, "woo")) and s["status"] == "connected" and s)
        ok(f"the store's MCP server is already registered: '{woo['name']}', connected, {len(woo['tools'])} tools")

        print("\n== the admin adds two more servers by URL and credential")
        definitions = [
            {"slug": "tasks", "name": "Team tasks", "url": f"http://tasks-mcp.{MCP_NAMESPACE}.svc.cluster.local/mcp",
             "auth_type": "header", "auth_header": "X-API-Key",
             "credential": secret(args, MCP_NAMESPACE, "tasks-mcp-auth", "token")},
            {"slug": "sandbox", "name": "Sandbox", "url": f"http://sandbox-mcp.{MCP_NAMESPACE}.svc.cluster.local/mcp",
             "timeout_seconds": 3},
        ]
        for definition in definitions:
            if server_named(admin, definition["slug"]):
                ok(f"'{definition['slug']}' is already registered; using it as it is")
                continue
            created = admin.call("POST", "/api/servers", definition)
            added_servers.append(created["id"])
            if created["status"] != "connected":
                raise Failure(f"'{definition['slug']}' is {created['status']}: {created['error']}")
            if definition.get("credential") and definition["credential"] in json.dumps(created):
                raise Failure("the API returned an upstream credential")
            ok(f"'{created['slug']}' connected with {len(created['tools'])} tools, credential "
               f"{'stored and not returned' if created['auth']['credential_set'] else 'not needed'}")

        print("\n== the admin tries to point the gateway at things only the cluster can reach")
        forbidden = {
            "a cloud metadata address": "http://169.254.169.254/latest/meta-data/",
            "the gateway's own database": f"http://{args.release}-postgres.{args.namespace}.svc.cluster.local:5432/",
            "the Kubernetes API": "https://kubernetes.default.svc.cluster.local/",
            "the store's WordPress": "http://store-wordpress.store-main.svc.cluster.local/",
            "the gateway itself": "http://127.0.0.1:8080/mcp",
        }
        for what, url in forbidden.items():
            refused = admin.call("POST", "/api/servers", {"slug": "ssrf-probe", "name": "Probe", "url": url}, expect=(422,))
            ok(f"{what}: refused. {refused['detail']}"[:170])
        if server_named(admin, "ssrf-probe"):
            raise Failure("a refused server was registered anyway")

        print("\n== a member joins by invitation and creates an API key")
        email = f"smoke-{int(time.time())}@example.com"
        invite = admin.call("POST", "/api/invites", {"email": email, "role": "member"})
        member = Dashboard(args.url)
        joined = member.call("POST", f"/api/invites/{invite['link'].rsplit('/', 1)[1]}/accept",
                             {"name": "Smoke Member", "password": "a-password-only-for-this-run"})
        member_id = joined["id"]
        key = member.call("POST", "/api/keys", {"name": "smoke test"})
        ok(f"{email} joined as a {joined['role']} and has a key starting {key['prefix']}")
        if "url" in json.dumps(member.call("GET", "/api/servers")):
            raise Failure("a member can see where the upstream servers live")
        ok("the member's view of the servers shows no URL and no credential")

        print("\n== an MCP client with only the gateway URL and that key")
        mcp = McpClient(mcp_url, key["key"])
        names = tool_names(mcp)
        by_server = {slug: [n for n in names if n.startswith(slug + "__")] for slug in ("woo", "tasks", "sandbox")}
        if not all(by_server.values()):
            raise Failure(f"tools are missing for a server: { {k: len(v) for k, v in by_server.items()} }")
        ok(f"{len(names)} tools: " + ", ".join(f"{len(v)} from {k}" for k, v in by_server.items()))
        if not {"tasks__server_info", "sandbox__server_info"} <= set(names):
            raise Failure("the two servers' same-named tool was not exposed under both prefixes")
        both = [mcp.call_tool(f"{slug}__server_info", {})["server"] for slug in ("tasks", "sandbox")]
        if both != ["tasks", "sandbox"]:
            raise Failure(f"same-named tools were routed to the wrong servers: {both}")
        ok("two servers both have a tool called server_info; each prefixed name reaches its own server")
        today = mcp.call_tool("woo__list_orders", {"period": "today"})
        ok(f"woo__list_orders(today): {today['total']} order(s) since {today['period']['start']}")

        print("\n== a client signs in through the browser instead of using a key")
        shown, tokens = sign_in_with_browser(args.url, member, "Smoke client")
        ok(f"given only {mcp_url}, it found where to sign in, registered itself, and the member was asked: "
           f"'{shown['client_name']}', returning to {shown['returns_to']}")
        ok("a code without the client's secret was refused, the right one exchanged for tokens, and a second use refused")
        signed_in = McpClient(mcp_url, tokens["access_token"])
        if tool_names(signed_in) != names:
            raise Failure("the signed-in client does not see the same tools as the key")
        signed_in.call_tool("woo__list_products", {"per_page": 1})
        ok(f"with its access token it sees the same {len(names)} tools and can call them")
        (grant,) = member.call("GET", "/api/oauth/grants")
        member.call("DELETE", f"/api/oauth/grants/{grant['id']}")
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        if (status := raw_status(mcp_url, tokens["access_token"], listing)) != 401:
            raise Failure(f"after the member signed it out, its token still got HTTP {status}")
        ok("the member signs it out in the dashboard; its next request gets HTTP 401")

        print("\n== the same tools through two: find, then call")
        search = McpClient(f"{mcp_url}/search", key["key"])
        if tool_names(search) != ["find_tools", "call_tool"]:
            raise Failure(f"{mcp_url}/search lists {tool_names(search)}")
        found = search.call_tool("find_tools", {"query": "add a task"})["tools"]
        if not found or found[0]["name"] != "tasks__add_task":
            raise Failure(f"searching for 'add a task' found {[tool['name'] for tool in found]}")
        echoed = search.call_tool("call_tool", {"name": "sandbox__echo", "arguments": {"text": "found by search"}})
        ok(f"{mcp_url}/search lists 2 tools instead of {len(names)}; 'add a task' finds {found[0]['name']}; "
           f"a tool called through it answered: {json.dumps(echoed)[:60]}")

        print("\n== one task that needs two servers")
        product = mcp.call_tool("woo__list_products", {"per_page": 1})["products"][0]
        task = mcp.call_tool("tasks__add_task", {"title": f"Restock {product['name']}", "notes": f"product #{product['id']}"})
        listed = [t["title"] for t in mcp.call_tool("tasks__list_tasks", {})["result"]]
        if task["title"] not in listed:
            raise Failure(f"the task is not in the list: {listed}")
        ok(f"read '{product['name']}' from the store, added the task '{task['title']}' to the task list")

        print("\n== a slow upstream times out and does not hold up the others")
        slow = {}

        def call_slow():
            started = time.time()
            slow["message"] = mcp.call_tool("sandbox__slow", {"seconds": 30}, expect_error=True)
            slow["took"] = time.time() - started

        thread = threading.Thread(target=call_slow)
        thread.start()
        time.sleep(0.3)
        started = time.time()
        mcp.call_tool("woo__list_products", {"per_page": 1})
        quick = time.time() - started
        thread.join(timeout=20)
        if "did not answer within 3 seconds" not in slow.get("message", "") or slow["took"] > 8:
            raise Failure(f"the slow call did not time out cleanly: {slow}")
        ok(f"sandbox__slow(30s) failed after {slow['took']:.1f}s: {slow['message']}")
        ok(f"a store call made while it was waiting came back in {quick:.2f}s")

        print("\n== a dead upstream: the tasks server's pod is taken away")
        kubectl(args, "-n", MCP_NAMESPACE, "scale", "deployment/tasks-mcp", "--replicas=0")
        try:
            dead = wait_for("the gateway notices", lambda: (s := server_named(admin, "tasks")) and s["status"] == "unreachable" and s)
            ok(f"the dashboard shows it as unreachable: {dead['error']}")
            # With several gateway replicas each one notices on its own next
            # check, so allow one check interval for them all to agree.
            names = wait_for("its tools leave the list", lambda: (n := tool_names(mcp)) and not any(t.startswith("tasks__") for t in n) and n, 30)
            if not any(n.startswith("woo__") for n in names):
                raise Failure(f"the store's tools are missing too: {names}")
            mcp.call_tool("woo__list_products", {"per_page": 1})
            mcp.call_tool("sandbox__echo", {"text": "still here"})
            ok("its tools are gone from the list; the store and the sandbox still answer")
            refused = mcp.call_tool("tasks__list_tasks", {}, expect_error=True)
            ok(f"calling it anyway: {refused}")
        finally:
            kubectl(args, "-n", MCP_NAMESPACE, "scale", "deployment/tasks-mcp", "--replicas=1")
        wait_for("the tasks server is connected again", lambda: server_named(admin, "tasks")["status"] == "connected", 90)
        wait_for("its tools are back in the list", lambda: any(t.startswith("tasks__") for t in tool_names(mcp)), 30)
        ok("pod restored; the gateway reconnected on its own")

        print("\n== the admin disables a server")
        sandbox = server_named(admin, "sandbox")
        admin.call("PATCH", f"/api/servers/{sandbox['id']}", {"enabled": False})
        try:
            if any(n.startswith("sandbox__") for n in tool_names(mcp)):
                raise Failure("a disabled server's tools are still listed")
            refused = mcp.call_tool("sandbox__echo", {"text": "x"}, expect_error=True)
            ok(f"its tools disappear from the client at once; calling one: {refused}")
        finally:
            admin.call("PATCH", f"/api/servers/{sandbox['id']}", {"enabled": True})

        print("\n== the member's key is revoked")
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "woo__list_products", "arguments": {}}}
        before = raw_status(mcp_url, key["key"], call)
        admin.call("DELETE", f"/api/keys/{key['id']}")
        after = raw_status(mcp_url, key["key"], call)
        if (before, after) != (200, 401):
            raise Failure(f"expected HTTP 200 then 401, got {before} then {after}")
        ok("the same request: HTTP 200 before, HTTP 401 on the very next call")

        print("\n== the audit log")
        entries = [e for e in admin.call("GET", "/api/audit?limit=200") if e["user_email"] == email]
        seen = {(e["server"], e["tool"], e["outcome"]) for e in entries}
        expected = {
            ("woo", "list_orders", "ok"), ("woo", "list_products", "ok"), ("tasks", "add_task", "ok"),
            ("sandbox", "slow", "timeout"),
            ("tasks", "list_tasks", "unreachable"), ("sandbox", "echo", "denied"),
            (None, "woo__list_products", "denied"),
        }
        if not expected <= seen:
            raise Failure(f"missing from the audit log: {sorted(expected - seen, key=str)}")
        ok(f"{len(entries)} entries for {email}, each with server, tool, time, duration and outcome")
        if not any((e["api_key_prefix"] or "").startswith("signed in: Smoke client") for e in entries):
            raise Failure("the call made after signing in with the browser is not marked as such")
        for server, tool, outcome in sorted(expected, key=str):
            entry = next(e for e in entries if (e["server"], e["tool"], e["outcome"]) == (server, tool, outcome))
            ok(f"  {outcome:<11} {server or '-':<8} {tool:<20} {entry['duration_ms']:>5} ms  {entry['error'] or ''}"[:150])
        print("\n== figures: per server in the dashboard, and for Prometheus inside the cluster")
        stats = {row["server"]: row for row in admin.call("GET", "/api/stats")}
        if not {"woo", "tasks", "sandbox"} <= set(stats):
            raise Failure(f"statistics are missing a server: {sorted(stats)}")
        for slug in ("woo", "tasks", "sandbox"):
            row = stats[slug]
            ok(f"{slug:<8} {row['calls']:>3} calls, {row['failures']} failed, typically {row['median_ms']} ms")
        scrape = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics', timeout=5).read().decode())"
        metrics = kubectl(args, "-n", args.namespace, "exec", f"deploy/{args.release}-api", "--", "python", "-c", scrape)
        counted = [line for line in metrics.splitlines() if line.startswith("gateway_tool_calls_total{")]
        if not counted or "gateway_upstream_up{" not in metrics:
            raise Failure("the API pod's /metrics has no tool-call or upstream figures")
        ok(f"/metrics inside the cluster: {len(counted)} tool-call series, plus health per upstream")
        with urllib.request.urlopen(f"{args.url}/metrics", timeout=10) as public:
            if "gateway_tool_calls_total" in public.read().decode():
                raise Failure("/metrics is reachable from outside the cluster")
        ok("the same path from outside the cluster does not reach the API")

        if any(e["user_email"] != email for e in member.call("GET", "/api/audit")):
            raise Failure("a member can see other people's audit entries")
        ok("the member sees only their own entries")
    finally:
        if not args.keep:
            if member_id:
                admin.call("DELETE", f"/api/members/{member_id}", expect=(204, 404))
            for server_id in added_servers:
                admin.call("DELETE", f"/api/servers/{server_id}", expect=(204, 404))
            ok("the test member and the servers this run added have been removed")

    print("\nGateway smoke test passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://gateway.localhost", help="public URL of the gateway")
    parser.add_argument("--context", default="k3d-urumi", help="kube context")
    parser.add_argument("--namespace", default="gateway")
    parser.add_argument("--release", default="gateway", help="Helm release name")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--keep", action="store_true", help="leave the member and servers this run adds")
    run(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
