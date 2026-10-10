#!/usr/bin/env python3
"""End-to-end check of stores created from the dashboard (the multi-store bonus).

    two stores are requested at once and provisioned side by side
    the provisioner is killed halfway through, and both stores still come up
    each has its own namespace, database, volumes, Secrets, quota and hostname
    each store's MCP server is registered in the gateway without anyone doing it
    an order is placed in each storefront; an MCP client compares today's orders
      across the two stores through tools whose names say which store they act on
    one store's MCP server cannot reach the other store's WordPress or database
    asking for the same store twice gives one store; the limit per person holds
    the provisioner's cluster permissions stop at the namespaces it created
    deleting a store removes its tools at once, then its namespace and volumes

Everything a user would do goes through the Ingress. The cluster is inspected
with kubectl. The stores this run creates are removed again unless --keep is
given. Standard library only.
"""

import argparse
import json
import subprocess
import sys
import time

from smoke_gateway import Dashboard, kubectl, secret, tool_names, wait_for
from smoke_mcp import McpClient
from smoke_store import Failure, ok, place_guest_order, request

STORES = {"smoke-a": "Smoke Test A", "smoke-b": "Smoke Test B"}


def stores_by_slug(user):
    return {store["slug"]: store for store in user.call("GET", "/api/stores")}


def wait_for_stores(user, slugs, status, minutes):
    """Waits until every named store has the status, printing each change on the way."""
    seen = {}
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        current = stores_by_slug(user)
        for slug in slugs:
            store = current.get(slug)
            line = f"{store['status']}: {store['detail'] or 'done'}" if store else "gone"
            if seen.get(slug) != line:
                seen[slug] = line
                print(f"        {slug:<8} {line}"[:150])
            if store and store["status"] == "failed" and status != "failed":
                raise Failure(f"store {slug} failed: {store['detail']}")
        # A status of None waits for the stores to be gone altogether.
        if all((current.get(slug) or {}).get("status") == status for slug in slugs):
            return current
        time.sleep(2)
    raise Failure(f"stores {', '.join(slugs)} were not {status} within {minutes} minutes")


def can_connect(args, namespace, target, host, port):
    """Whether a TCP connection opens from a pod. Uses the Python in the MCP server's image."""
    script = "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4).close()"
    result = subprocess.run(
        ["kubectl", "--context", args.context, "-n", namespace, "exec", target, "--", "python", "-c", script, host, str(port)],
        capture_output=True,
    )
    return result.returncode == 0


def as_provisioner(args, *command):
    """Runs kubectl with the provisioner's identity and returns (allowed, what Kubernetes said)."""
    account = f"system:serviceaccount:{args.namespace}:{args.release}-provisioner"
    result = subprocess.run(
        ["kubectl", "--context", args.context, f"--as={account}", *command], capture_output=True, text=True
    )
    said = (result.stderr or result.stdout).strip().splitlines()[0]
    # An admission policy's own message comes after this marker; RBAC's reason after "forbidden: ".
    for marker in ("denied request: ", "is forbidden: "):
        if marker in said:
            said = said.split(marker, 1)[1]
    return result.returncode == 0, said


def run(args):
    admin = Dashboard(args.url)
    print(f"\n== the admin signs in at {args.url}")
    admin.call("POST", "/api/auth/login", {
        "email": args.admin_email,
        "password": secret(args, args.namespace, f"{args.release}-secrets", "admin-password"),
    })
    config = admin.call("GET", "/api/config")
    if not config["stores_enabled"]:
        raise Failure("creating stores is not switched on (provisioner.enabled in the gateway chart)")
    leftovers = set(STORES) & set(stores_by_slug(admin))
    if leftovers:
        raise Failure(f"stores from an earlier run are still there: {sorted(leftovers)}. Delete them first.")
    ok(f"stores can be created here, up to {config['max_stores_per_user']} per person")

    member, member_id, created = None, None, []
    try:
        print("\n== a member joins and asks for two stores at once")
        email = f"stores-{int(time.time())}@example.com"
        invite = admin.call("POST", "/api/invites", {"email": email, "role": "member"})
        member = Dashboard(args.url)
        member_id = member.call("POST", f"/api/invites/{invite['link'].rsplit('/', 1)[1]}/accept",
                                {"name": "Store Owner", "password": "a-password-only-for-this-run"})["id"]
        for slug, name in STORES.items():
            store = member.call("POST", "/api/stores", {"name": name, "slug": slug})
            created.append(store["id"])
            ok(f"asked for '{name}': {store['status']}, namespace {store['namespace']}, tools {store['tool_prefix']}...")

        repeat = member.call("POST", "/api/stores", {"name": "Again", "slug": "smoke-a"}, expect=(409,))
        ok(f"asking for the same store again does not make a second one: {repeat['detail']}")

        print("\n== both are provisioned side by side, and the provisioner is killed halfway")
        wait_for("both stores are being installed", lambda: all(
            "Waiting for store-" in (s.get("detail") or "") for slug, s in stores_by_slug(member).items() if slug in STORES
        ), seconds=90)
        kubectl(args, "-n", args.namespace, "delete", "pod", "-l", "app.kubernetes.io/component=provisioner", "--wait=false")
        ok("both were mid-install; the provisioner's pod has been deleted")
        ready = wait_for_stores(member, STORES, "ready", minutes=args.timeout_minutes)
        ok("both stores are ready: the replacement provisioner carried on from where the first one stopped")

        print("\n== what each store got in the cluster")
        for slug in STORES:
            namespace = ready[slug]["namespace"]
            labels = json.loads(kubectl(args, "get", "namespace", namespace, "-o", "json"))["metadata"]["labels"]
            if labels.get("mcp-gateway/store") != slug or labels.get("pod-security.kubernetes.io/enforce") != "restricted":
                raise Failure(f"namespace {namespace} has the wrong labels: {labels}")
            kinds = "statefulset,deployment,pvc,secret,ingress,networkpolicy,resourcequota,limitrange"
            found = kubectl(args, "-n", namespace, "get", kinds, "-o", "name").split()
            counted = {kind: sum(1 for name in found if name.split("/")[0].split(".")[0] == kind) for kind in
                       ("statefulset", "deployment", "persistentvolumeclaim", "secret", "ingress", "networkpolicy",
                        "resourcequota", "limitrange")}
            if not all(counted.values()):
                raise Failure(f"namespace {namespace} is missing something: {counted}")
            host = kubectl(args, "-n", namespace, "get", "ingress", "store", "-o", "jsonpath={.spec.rules[0].host}")
            ok(f"{namespace}: own database, {counted['persistentvolumeclaim']} volumes, {counted['secret']} Secrets, "
               f"{counted['networkpolicy']} network policies, a quota, host {host}; pods held to the 'restricted' standard")

        print("\n== each store's MCP server is already in the gateway")
        servers = {server["slug"]: server for server in admin.call("GET", "/api/servers")}
        for slug, name in STORES.items():
            server = wait_for(f"store-{slug} is connected", lambda slug=slug: (
                s := next((s for s in admin.call("GET", "/api/servers") if s["slug"] == f"store-{slug}"), None)
            ) and s["status"] == "connected" and s)
            if not server["managed"] or "credential" in json.dumps(server).replace("credential_set", ""):
                raise Failure(f"store-{slug} is not registered as a store's server, or leaks its credential")
            ok(f"'{server['name']}' is connected with {len(server['tools'])} tools; nobody registered it by hand")
        refused = admin.call("DELETE", f"/api/servers/{servers.get('store-smoke-a', server)['id']}", expect=(409,))
        ok(f"it cannot be removed on its own: {refused['detail']}")

        print("\n== an order is placed in each storefront, then compared through the gateway")
        placed = {}
        for slug, quantity in (("smoke-a", 1), ("smoke-b", 3)):
            url = ready[slug]["url"]
            status, _, products = request("GET", f"{url}/wp-json/wc/store/v1/products")
            if status != 200 or not products:
                raise Failure(f"{url} has no products to buy (HTTP {status})")
            placed[slug] = place_guest_order(url, products[0]["id"], quantity)
            ok(f"{url}: order #{placed[slug]['id']} for {quantity} x {products[0]['name']}, total {placed[slug]['total']:.2f}")

        key = member.call("POST", "/api/keys", {"name": "stores smoke"})
        mcp = McpClient(f"{args.url}/mcp", key["key"])
        names = tool_names(mcp)
        for slug in STORES:
            if f"store-{slug}__list_orders" not in names:
                raise Failure(f"the gateway does not list store-{slug}__list_orders")
        ok(f"one endpoint, one key, {len(names)} tools; each store's tools carry its name: "
           "store-smoke-a__list_orders, store-smoke-b__list_orders")
        totals = {}
        for slug in STORES:
            orders = mcp.call_tool(f"store-{slug}__list_orders", {"period": "today"})["orders"]
            if [order["id"] for order in orders] != [placed[slug]["id"]]:
                raise Failure(f"store-{slug}__list_orders(today) returned {orders}, expected order #{placed[slug]['id']}")
            totals[slug] = float(orders[0]["total"])
        if totals["smoke-b"] <= totals["smoke-a"]:
            raise Failure(f"the two stores' totals do not differ as expected: {totals}")
        ok(f"today's orders compared across stores: A took {totals['smoke-a']:.2f}, B took {totals['smoke-b']:.2f}; "
           "each answer came from its own store")

        print("\n== one store cannot reach into another")
        a, b = ready["smoke-a"]["namespace"], ready["smoke-b"]["namespace"]
        wait_for("store A's MCP server reaches its own WordPress",
                 lambda: can_connect(args, a, "deploy/store-woo-mcp", "store-wordpress", 80))
        ok("allowed  store A's MCP server -> its own WordPress (control: the probe works)")
        for label, host, port in (
            ("store B's WordPress", f"store-wordpress.{b}.svc.cluster.local", 80),
            ("store B's database", f"store-mariadb.{b}.svc.cluster.local", 3306),
            ("store B's MCP server", f"store-woo-mcp.{b}.svc.cluster.local", 80),
            ("the first store's database", "store-mariadb.store-main.svc.cluster.local", 3306),
        ):
            if can_connect(args, a, "deploy/store-woo-mcp", host, port):
                raise Failure(f"store A's MCP server can open a connection to {label}")
            ok(f"blocked  store A's MCP server -> {label}")

        print("\n== the limit on stores per person")
        limit = config["max_stores_per_user"]
        extra = []
        for n in range(len(STORES), limit):
            extra.append(member.call("POST", "/api/stores", {"name": f"Extra {n}", "slug": f"smoke-x{n}"}))
            created.append(extra[-1]["id"])
        over = member.call("POST", "/api/stores", {"name": "One too many", "slug": "smoke-over"}, expect=(409,))
        ok(f"with {limit} stores, the next request is refused: {over['detail']}")
        for store in extra:
            # Deleted while still being created: the creation stops and is undone.
            member.call("DELETE", f"/api/stores/{store['id']}", expect=(202,))
        wait_for("the extra stores are gone", lambda: not any(s.startswith("smoke-x") for s in stores_by_slug(member)), seconds=180)
        ok("a store deleted while it was still being created is cleaned up")

        print("\n== what the provisioner is allowed to do in the cluster")
        checks = [
            (False, "read Secrets in the gateway's namespace", ("-n", args.namespace, "get", "secrets")),
            (False, "read Secrets in kube-system", ("-n", "kube-system", "get", "secrets")),
            (False, "read pods of the first store", ("-n", "store-main", "get", "pods")),
            (False, "delete the first store's namespace", ("delete", "namespace", "store-main", "--dry-run=server")),
            (False, "create a namespace that is not a store's", ("create", "namespace", "not-a-store", "--dry-run=server")),
            (False, "give itself the installer role in kube-system", (
                "-n", "kube-system", "create", "rolebinding", "grab", f"--clusterrole={args.release}-store-installer",
                f"--serviceaccount={args.namespace}:{args.release}-provisioner", "--dry-run=server")),
            (False, "give itself cluster-admin in a store's namespace", (
                "-n", a, "create", "rolebinding", "grab", "--clusterrole=cluster-admin",
                f"--serviceaccount={args.namespace}:{args.release}-provisioner", "--dry-run=server")),
            (True, "read the pods of a store it created", ("-n", a, "get", "pods", "-o", "name")),
        ]
        for expected, what, command in checks:
            allowed, said = as_provisioner(args, *command)
            if allowed != expected:
                raise Failure(f"the provisioner {'can' if allowed else 'cannot'} {what}: {said}")
            ok(f"{'allowed ' if allowed else 'refused '} {what}" + ("" if allowed else f": {said}"[:150]))

        print("\n== deleting a store")
        volumes = kubectl(args, "-n", b, "get", "pvc", "-o", "jsonpath={.items[*].spec.volumeName}").split()
        member.call("DELETE", f"/api/stores/{ready['smoke-b']['id']}", expect=(202,))
        names = tool_names(mcp)
        if any(name.startswith("store-smoke-b__") for name in names):
            raise Failure("the deleted store's tools are still listed")
        gone = mcp.call_tool("store-smoke-b__list_orders", {}, expect_error=True)
        ok(f"its tools left the gateway at once ({len(names)} tools remain); calling one: {gone}")
        wait_for_stores(member, ["smoke-b"], None, minutes=5)
        if subprocess.run(["kubectl", "--context", args.context, "get", "namespace", b], capture_output=True).returncode == 0:
            raise Failure(f"namespace {b} still exists")
        wait_for("its volumes are removed", lambda: not set(volumes) & set(
            kubectl(args, "get", "pv", "-o", "jsonpath={.items[*].metadata.name}").split()), seconds=90)
        ok(f"namespace {b} is gone, with its pods, Secrets and Ingress; its {len(volumes)} volumes are deleted")
        if mcp.call_tool("store-smoke-a__list_orders", {"period": "today"})["total"] != 1:
            raise Failure("the other store stopped answering")
        ok("the other store is untouched and still answers")

        print("\n== the audit log")
        mine = [e for e in admin.call("GET", "/api/audit?limit=100") if e["user_email"] == email]
        seen = {(e["server"], e["tool"], e["outcome"]) for e in mine}
        expected = {("store-smoke-a", "list_orders", "ok"), ("store-smoke-b", "list_orders", "ok"),
                    ("store-smoke-b", "list_orders", "error")}
        if not expected <= seen:
            raise Failure(f"missing from the audit log: {sorted(expected - seen)}")
        ok(f"{len(mine)} calls by {email}, each against the store it named, including the one made after deletion")
    finally:
        if not args.keep:
            for store in stores_by_slug(admin).values():
                if store["id"] in created and store["status"] != "deleting":
                    admin.call("DELETE", f"/api/stores/{store['id']}", expect=(202, 404))
            try:
                wait_for("this run's stores are gone",
                         lambda: not any(s["id"] in created for s in stores_by_slug(admin).values()), seconds=300)
            finally:
                if member_id:
                    admin.call("DELETE", f"/api/members/{member_id}", expect=(204, 404))
            ok("the stores and the member this run created have been removed")

    print("\nStores smoke test passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://gateway.localhost", help="public URL of the gateway")
    parser.add_argument("--context", default="k3d-urumi", help="kube context")
    parser.add_argument("--namespace", default="gateway")
    parser.add_argument("--release", default="gateway", help="Helm release name")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--timeout-minutes", type=int, default=10, help="how long both stores may take to be ready")
    parser.add_argument("--keep", action="store_true", help="leave the stores and the member this run creates")
    run(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
