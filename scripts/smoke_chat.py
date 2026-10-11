#!/usr/bin/env python3
"""End-to-end check of the chat agent against the real model, as a team member.

It walks the "chat agent" part of the definition of done:

    the agent is asked to create a product with a name and a price
    the product appears on the storefront
    an order for it is placed on the storefront
    the agent is asked what orders came in today, and reports that order
    the agent's tool calls are in the audit log under the member, with the source "chat"

With --stores it also runs the multi-store bonus's own test: two stores are
created, an order is placed in each, the agent compares today's orders across
the two, one store is deleted, and its tools and namespace are gone.

What is checked is what the agent did (which tools it called, what changed in
the store) and that its answer contains the facts, not how it words them. It
needs the model's API key to be set (`make llm-key`). Standard library only.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from smoke_gateway import Dashboard, secret, wait_for
from smoke_store import Failure, ok, place_guest_order, request, wp_cli
from smoke_stores import stores_by_slug, wait_for_stores

STORES = {"chat-a": "Chat Test A", "chat-b": "Chat Test B"}


def ask(user, base_url, question, attempts=3):
    """Sends one question to the chat and returns (the tools it ran, its answer).

    The reply is a stream of events. A refusal from the model's API for sending
    too many requests a minute, which the free tier does, is waited out.
    """
    body = json.dumps({"messages": [{"role": "user", "content": question}]}).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(f"{base_url}/api/chat", data=body, method="POST", headers={"Content-Type": "application/json"})
        events = []
        try:
            with user._opener.open(req, timeout=240) as response:
                for raw in response:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
        except urllib.error.HTTPError as error:
            raise Failure(f"the chat answered HTTP {error.code}: {error.read().decode()[:300]}")
        errors = [event["message"] for event in events if event["type"] == "error"]
        if not errors:
            tools = [(event["name"], event["ok"]) for event in events if event["type"] == "tool_result"]
            answer = " ".join(event["text"] for event in events if event["type"] == "text")
            return tools, answer
        if attempt + 1 < attempts and any(word in errors[0].lower() for word in ("rate", "quota", "busy", "too long")):
            print(f"        the model's API said: {errors[0][:110]} Waiting 40 seconds.")
            time.sleep(40)
            continue
        raise Failure(f"the chat failed: {errors[0]}")


def must_have_run(tools, name, question):
    if (name, True) not in tools:
        raise Failure(f"asked {question!r}, the agent did not successfully run {name}; it ran {tools}")


def run(args):
    admin = Dashboard(args.gateway_url)
    print(f"\n== the admin signs in at {args.gateway_url}")
    admin.call("POST", "/api/auth/login", {
        "email": args.admin_email,
        "password": secret(args, args.gateway_namespace, f"{args.gateway_release}-secrets", "admin-password"),
    })
    config = admin.call("GET", "/api/config")
    if not config["chat_enabled"]:
        raise Failure("the chat has no model configured. Run: LLM_API_KEY=<key> make llm-key")
    ok(f"the chat is on, using the model {config['chat_model']}")

    member_id, product_id, order_id, created = None, None, None, []
    store = argparse.Namespace(context=args.context, namespace=args.store_namespace, release=args.store_release,
                               admin_user=args.admin_user)
    try:
        print("\n== user A joins by invitation")
        email = f"chat-{int(time.time())}@example.com"
        invite = admin.call("POST", "/api/invites", {"email": email, "role": "member"})
        member = Dashboard(args.gateway_url)
        member_id = member.call("POST", f"/api/invites/{invite['link'].rsplit('/', 1)[1]}/accept",
                                {"name": "User A", "password": "a-password-only-for-this-run"})["id"]
        ok(f"{email} is signed in to the dashboard as a member")

        print("\n== user A asks the agent to create a product")
        name = f"Desk Lamp {time.strftime('%H%M%S')}"
        question = f"Create a product called '{name}' with a price of 24.50."
        tools, answer = ask(member, args.gateway_url, question)
        must_have_run(tools, "woo__create_product", question)
        ok(f"asked: {question}")
        ok(f"it ran {', '.join(tool for tool, _ in tools)} and answered: {answer[:140]}")
        status, _, found = request("GET", f"{args.store_url}/wp-json/wc/store/v1/products?search={name.replace(' ', '%20')}")
        if status != 200 or len(found) != 1:
            raise Failure(f"the storefront does not show exactly one '{name}' (HTTP {status}, {len(found or [])} found)")
        product_id = found[0]["id"]
        price = int(found[0]["prices"]["price"]) / 10 ** found[0]["prices"]["currency_minor_unit"]
        if price != 24.50:
            raise Failure(f"the product is on the storefront at {price}, not 24.50")
        ok(f"the product is on the storefront: {found[0]['permalink']}, {price:.2f} {found[0]['prices']['currency_code']}")

        print("\n== an order for it is placed on the storefront")
        placed = place_guest_order(args.store_url, product_id)
        order_id = placed["id"]
        ok(f"order #{order_id}, total {placed['total']:.2f} {placed['currency']}, cash on delivery")

        print("\n== user A asks what orders came in today")
        question = "What orders came in today?"
        tools, answer = ask(member, args.gateway_url, question)
        must_have_run(tools, "woo__list_orders", question)
        if str(order_id) not in answer:
            raise Failure(f"the answer does not mention order #{order_id}: {answer[:300]}")
        ok(f"it ran {', '.join(tool for tool, _ in tools)} and answered: {answer[:220]}")

        print("\n== the audit log")
        mine = [e for e in admin.call("GET", "/api/audit?limit=100") if e["user_email"] == email]
        seen = {(e["source"], e["server"], e["tool"], e["outcome"]) for e in mine}
        expected = {("chat", "woo", "create_product", "ok"), ("chat", "woo", "list_orders", "ok")}
        if not expected <= seen:
            raise Failure(f"missing from the audit log: {sorted(expected - seen)}")
        ok(f"{len(mine)} tool calls by {email}, all with the source 'chat' and none with an API key")

        if args.stores:
            run_stores(args, admin, member, created)
    finally:
        if not args.keep:
            if order_id:
                wp_cli(store, "wc", "shop_order", "delete", str(order_id), "--force=true")
            if product_id:
                wp_cli(store, "wc", "product", "delete", str(product_id), "--force=true")
            for other in stores_by_slug(admin).values():
                if other["id"] in created and other["status"] != "deleting":
                    admin.call("DELETE", f"/api/stores/{other['id']}", expect=(202, 404))
            if created:
                wait_for("this run's stores are gone",
                         lambda: not any(s["id"] in created for s in stores_by_slug(admin).values()), seconds=300)
            if member_id:
                admin.call("DELETE", f"/api/members/{member_id}", expect=(204, 404))
            ok("the product, the order, the stores and the member this run created have been removed")

    print("\nChat smoke test passed.")


def run_stores(args, admin, member, created):
    print("\n== the bonus: two stores, compared by the agent")
    for slug, name in STORES.items():
        created.append(member.call("POST", "/api/stores", {"name": name, "slug": slug})["id"])
    ready = wait_for_stores(member, STORES, "ready", minutes=10)
    totals = {}
    for slug, quantity in (("chat-a", 1), ("chat-b", 2)):
        url = ready[slug]["url"]
        _, _, products = request("GET", f"{url}/wp-json/wc/store/v1/products")
        totals[slug] = place_guest_order(url, products[0]["id"], quantity)["total"]
        ok(f"{STORES[slug]} is ready at {url}; an order of {totals[slug]:.2f} was placed there")
    for slug in STORES:
        wait_for(f"store-{slug} is connected", lambda slug=slug: any(
            s["slug"] == f"store-{slug}" and s["status"] == "connected" for s in admin.call("GET", "/api/servers")))

    question = (f"Compare today's orders in the {STORES['chat-a']} store and the {STORES['chat-b']} store. "
                "For each, how many orders and what total?")
    tools, answer = ask(member, args.gateway_url, question)
    for slug in STORES:
        must_have_run(tools, f"store-{slug}__list_orders", question)
    for slug in STORES:
        if f"{totals[slug]:.2f}" not in answer and f"{totals[slug]:g}" not in answer:
            raise Failure(f"the answer does not give {STORES[slug]}'s total of {totals[slug]:.2f}: {answer[:400]}")
    ok(f"asked: {question}")
    ok(f"it ran {', '.join(tool for tool, _ in tools)}")
    ok(f"and answered: {' '.join(answer.split())[:300]}")

    print("\n== one store is deleted")
    namespace = ready["chat-b"]["namespace"]
    member.call("DELETE", f"/api/stores/{ready['chat-b']['id']}", expect=(202,))
    question = f"List today's orders in the {STORES['chat-b']} store."
    tools, answer = ask(member, args.gateway_url, question)
    if any(tool.startswith("store-chat-b__") and succeeded for tool, succeeded in tools):
        raise Failure(f"the agent still reached the deleted store: {tools}")
    ok(f"asked: {question}")
    ok(f"it could not: {' '.join(answer.split())[:200]}")
    wait_for_stores(member, ["chat-b"], None, minutes=5)
    if subprocess.run(["kubectl", "--context", args.context, "get", "namespace", namespace], capture_output=True).returncode == 0:
        raise Failure(f"namespace {namespace} still exists")
    ok(f"namespace {namespace} and everything in it are gone")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway-url", default="http://gateway.localhost")
    parser.add_argument("--store-url", default="http://store.localhost")
    parser.add_argument("--context", default="k3d-urumi", help="kube context")
    parser.add_argument("--gateway-namespace", default="gateway")
    parser.add_argument("--gateway-release", default="gateway")
    parser.add_argument("--store-namespace", default="store-main")
    parser.add_argument("--store-release", default="store")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--admin-user", default="admin", help="the store's WordPress admin, for cleaning up")
    parser.add_argument("--stores", action="store_true", help="also create two stores and have the agent compare them")
    parser.add_argument("--keep", action="store_true", help="leave what this run creates")
    args = parser.parse_args()
    # The helpers shared with the other smoke tests expect these two names.
    args.namespace, args.release = args.gateway_namespace, args.gateway_release
    run(args)


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
