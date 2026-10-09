#!/usr/bin/env python3
"""End-to-end check of a store's MCP server, through the tools themselves.

The flow is the one the definition of done describes for the chat agent, minus
the model: create a product with a tool and find it on the storefront, place an
order by hand, then ask the tools what came in today and move the order along.

    create_product       -> the product page exists on the storefront
    (guest checkout)     -> an order for it
    list_orders(today)   -> returns that order
    get_order            -> shows who bought it
    update_order_status  -> the shop admin sees the new status and the note

The MCP server has no address outside the cluster, so the script reaches it
through `kubectl port-forward`. That is an operator's tunnel, opened with
cluster credentials through the Kubernetes API; it is not a path a pod or an
outside client can use.

The MCP client below is deliberately written out by hand rather than taken from
the SDK. It is about the smallest one that works, which makes it a readable
description of what the protocol looks like on the wire. Standard library only.
"""

import argparse
import base64
import itertools
import json
import re
import subprocess
import sys
import time

from smoke_store import Failure, add_common_arguments, ok, place_guest_order, request, wp_cli

PROTOCOL_VERSION = "2026-07-28"
REQUIRED_TOOLS = {"list_products", "create_product", "list_orders", "get_order", "update_order_status"}


class McpClient:
    """A minimal MCP client for Streamable HTTP, protocol revision 2026-07-28.

    In this revision every request stands alone. There is no handshake and no
    session: each call is one HTTP POST carrying a JSON-RPC request, and the
    reply is the JSON-RPC response. The client says who it is and which
    protocol version it speaks inside every request, under `_meta`.
    """

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self._ids = itertools.count(1)

    def rpc(self, method, params=None, name=None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            # The method and tool name are repeated as headers so a proxy can
            # route or log a request without parsing its body.
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = name
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": {"name": "smoke-test", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        status, _, reply = request("POST", self.url, body, headers)
        if status != 200 or not isinstance(reply, dict):
            raise Failure(f"{method}: HTTP {status}: {str(reply)[:200]}")
        if "error" in reply:
            raise Failure(f"{method}: protocol error {reply['error']}")
        return reply["result"]

    def call_tool(self, name, arguments, expect_error=False):
        """Calls a tool and returns its structured result.

        A tool that fails still returns a normal result, flagged `isError`,
        whose text explains what went wrong. That text is meant for the model.
        """
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, name=name)
        text = " ".join(block.get("text", "") for block in result.get("content", []))
        if result.get("isError") != expect_error:
            raise Failure(f"{name}{arguments}: isError={result.get('isError')}: {text[:300]}")
        return text if expect_error else result["structuredContent"]


class PortForward:
    """`kubectl port-forward` to a Service, on a free local port, for the length of a `with` block."""

    def __init__(self, args, service, remote_port=80):
        self._command = [
            "kubectl", "--context", args.context, "--namespace", args.namespace,
            "port-forward", f"service/{service}", f":{remote_port}",
        ]

    def __enter__(self):
        self._process = subprocess.Popen(self._command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self._process.stdout.readline()
        match = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not match:
            raise Failure(f"port-forward did not start: {line or self._process.stderr.read()}".strip())
        return f"http://127.0.0.1:{match.group(1)}"

    def __exit__(self, *_):
        self._process.terminate()
        self._process.wait(timeout=10)


def secret_value(args, key):
    result = subprocess.run(
        ["kubectl", "--context", args.context, "--namespace", args.namespace, "get", "secret",
         f"{args.release}-secrets", "-o", f"go-template={{{{index .data \"{key}\"}}}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise Failure(f"could not read {key} from the store's Secret: {result.stderr.strip()}")
    return base64.b64decode(result.stdout).decode()


def wait_until_healthy(base_url):
    for _ in range(40):
        try:
            if request("GET", f"{base_url}/healthz")[0] == 200:
                return
        except Failure:
            pass
        time.sleep(0.25)
    raise Failure("the MCP server did not answer its health check through the port-forward")


def run(args, base_url):
    endpoint = f"{base_url}/mcp"
    probe = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    print("\n== the endpoint refuses callers without the token")
    for label, headers in (("no token", {}), ("a wrong token", {"Authorization": "Bearer not-the-token"})):
        status = request("POST", endpoint, probe, headers)[0]
        if status != 401:
            raise Failure(f"{label} got HTTP {status}, expected 401")
        ok(f"{label}: HTTP 401")

    mcp = McpClient(endpoint, secret_value(args, "mcp-auth-token"))

    print("\n== discovery and tool listing")
    discovered = mcp.rpc("server/discover")
    info = discovered["_meta"]["io.modelcontextprotocol/serverInfo"]
    if PROTOCOL_VERSION not in discovered["supportedVersions"]:
        raise Failure(f"the server supports {discovered['supportedVersions']}, not {PROTOCOL_VERSION}")
    ok(f"server {info['name']!r} {info['version']} speaks protocol {PROTOCOL_VERSION}")
    tools = {tool["name"]: tool for tool in mcp.rpc("tools/list")["tools"]}
    missing = REQUIRED_TOOLS - set(tools)
    if missing:
        raise Failure(f"required tools are missing: {sorted(missing)}")
    ok(f"{len(tools)} tools: {', '.join(tools)}")

    stamp = str(int(time.time()))
    name = f"Smoke Scarf {stamp}"
    product = order = None
    try:
        print("\n== create a product with a tool, then find it on the storefront")
        product = mcp.call_tool("create_product", {
            "name": name, "regular_price": 23.45, "sku": f"smoke-{stamp}",
            "short_description": "Created by the MCP smoke test.",
        })
        ok(f"create_product: #{product['id']} {product['name']!r} at {product['currency']} {product['price']}, "
           f"{product['status']}")
        status, _, page = request("GET", product["permalink"])
        if status != 200 or name not in page:
            raise Failure(f"its storefront page {product['permalink']} returned HTTP {status} without the name")
        ok(f"its page is live: {product['permalink']}")
        found = mcp.call_tool("list_products", {"search": stamp})
        if [p["id"] for p in found["products"]] != [product["id"]]:
            raise Failure(f"list_products(search={stamp!r}) returned {[p['id'] for p in found['products']]}")
        # Characters that must be percent-encoded exercise the request signing.
        mcp.call_tool("list_products", {"search": "wool & cotton +1 50%"})
        ok("list_products finds it by search, and a search full of awkward characters still signs correctly")

        print("\n== place an order by hand, then ask what came in today")
        placed = place_guest_order(args.url, product["id"], quantity=2)
        ok(f"guest checkout: order #{placed['id']}, {placed['currency']} {placed['total']:.2f}")
        today = mcp.call_tool("list_orders", {"period": "today"})
        order = next((o for o in today["orders"] if o["id"] == placed["id"]), None)
        if order is None:
            raise Failure(f"list_orders(today) did not return order #{placed['id']}; it returned "
                          f"{[o['id'] for o in today['orders']]} for {today['period']}")
        if abs(float(order["total"]) - placed["total"]) > 0.005 or order["items"][0]["name"] != name:
            raise Failure(f"order #{order['id']} came back wrong: {order}")
        ok(f"list_orders(today) returns it: #{order['id']} {order['status']}, {order['currency']} {order['total']}, "
           f"{order['items'][0]['quantity']} x {order['items'][0]['name']}")
        ok(f"'today' was measured from {today['period']['start']} ({today['period']['timezone']})")
        if any(o["id"] == placed["id"] for o in mcp.call_tool("list_orders", {"period": "yesterday"})["orders"]):
            raise Failure("list_orders(yesterday) also returned today's order")
        ok("list_orders(yesterday) does not return it")

        detail = mcp.call_tool("get_order", {"order_id": placed["id"]})
        if detail["customer_email"] != "smoke-test@example.com" or detail["billing"]["city"] != "San Francisco":
            raise Failure(f"get_order returned the wrong customer: {detail['customer_email']}, {detail['billing']}")
        ok(f"get_order: bought by {detail['customer_name']} <{detail['customer_email']}>, paying by "
           f"{detail['payment_method']}")

        print("\n== change the order's status, then check what the shop admin sees")
        note = f"Packed by smoke test {stamp}"
        change = mcp.call_tool("update_order_status", {"order_id": placed["id"], "status": "completed", "note": note})
        if (change["previous_status"], change["order"]["status"]) != ("processing", "completed"):
            raise Failure(f"update_order_status reported {change['previous_status']} -> {change['order']['status']}")
        ok(f"update_order_status: {change['previous_status']} -> {change['order']['status']}")
        admin_status = wp_cli(args, "wc", "shop_order", "get", str(placed["id"]), "--field=status").strip()
        notes = wp_cli(args, "wc", "order_note", "list", str(placed["id"]), "--format=json")
        if admin_status != "completed" or note not in notes:
            raise Failure(f"WooCommerce shows status {admin_status!r}; note recorded: {note in notes}")
        ok("WooCommerce shows the order as completed, with the note on it")

        print("\n== a failure reads as an explanation, not a crash")
        message = mcp.call_tool("get_order", {"order_id": 999999999}, expect_error=True)
        if "does not exist" not in message:
            raise Failure(f"unexpected error text: {message}")
        ok(f"get_order(999999999): {message}")
    finally:
        if not args.keep:
            if order:
                wp_cli(args, "wc", "shop_order", "delete", str(order["id"]), "--force=true")
            if product:
                wp_cli(args, "wc", "product", "delete", str(product["id"]), "--force=true")
            if product:
                ok("test product and order deleted")

    print("\nMCP smoke test passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    args = parser.parse_args()

    with PortForward(args, f"{args.release}-woo-mcp") as base_url:
        wait_until_healthy(base_url)
        run(args, base_url)


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
