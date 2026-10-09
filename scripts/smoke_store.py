#!/usr/bin/env python3
"""End-to-end check of a store, from outside the cluster.

It does what the definition of done asks a person to do by hand:

  1. open the storefront and find a product
  2. as a guest, add it to the cart and check out with cash on delivery
  3. confirm the order is visible to the shop admin

Steps 1 and 2 go through the Ingress and use WooCommerce's Store API, the same
endpoints the cart and checkout pages call from the browser. Step 3 asks
WooCommerce from inside the pod with WP-CLI. The test order is deleted
afterwards unless --keep is given.

Standard library only, so it runs on any Python 3.9+.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

SHOPPER = {
    "first_name": "Smoke",
    "last_name": "Test",
    "address_1": "1 Test Street",
    "city": "San Francisco",
    "state": "CA",
    "postcode": "94103",
    "country": "US",
    "email": "smoke-test@example.com",
    "phone": "5550100",
}


class Failure(Exception):
    pass


def ok(message):
    print(f"  ok    {message}")


def request(method, url, body=None, headers=None):
    """Returns (status, response headers, body). The body is parsed if it is JSON."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status, response_headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, response_headers, raw = error.code, error.headers, error.read()
    except urllib.error.URLError as error:
        raise Failure(f"{method} {url}: {error.reason}")
    text = raw.decode("utf-8", "replace")
    if "json" in response_headers.get("Content-Type", ""):
        return status, response_headers, json.loads(text or "null")
    return status, response_headers, text


def api_error(payload):
    if isinstance(payload, dict):
        return payload.get("message") or payload.get("code") or str(payload)
    return str(payload)[:200]


def wp_cli(args, *command):
    """Runs WP-CLI in the WordPress container, acting as the shop admin."""
    full = [
        "kubectl", "--context", args.context, "--namespace", args.namespace,
        "exec", f"deploy/{args.release}-wordpress", "--container", "wordpress", "--",
        "wp", f"--user={args.admin_user}", *command,
    ]
    result = subprocess.run(full, capture_output=True, text=True)
    if result.returncode != 0:
        raise Failure(f"wp {' '.join(command)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def place_guest_order(store_url, product_id, quantity=1):
    """Buys a product as a guest, paying cash on delivery. Returns the order's id and total."""
    store_api = f"{store_url}/wp-json/wc/store/v1"
    # A cart belongs to a session. The Cart-Token header carries that session
    # between requests, which is what a cookie does for the browser.
    status, headers, cart = request("GET", f"{store_api}/cart")
    token = headers.get("Cart-Token")
    if status != 200 or not token:
        raise Failure(f"could not open a cart (HTTP {status})")
    session = {"Cart-Token": token, "Nonce": headers.get("Nonce", "")}

    status, _, cart = request("POST", f"{store_api}/cart/add-item", {"id": product_id, "quantity": quantity}, session)
    if status not in (200, 201):
        raise Failure(f"add to cart failed (HTTP {status}): {api_error(cart)}")
    totals = cart["totals"]
    total = int(totals["total_price"]) / 10 ** totals["currency_minor_unit"]

    status, _, checkout = request("POST", f"{store_api}/checkout", {
        "billing_address": SHOPPER,
        "shipping_address": {k: v for k, v in SHOPPER.items() if k != "email"},
        "payment_method": "cod",
    }, session)
    if status != 200:
        raise Failure(f"checkout failed (HTTP {status}): {api_error(checkout)}")
    payment = checkout["payment_result"]["payment_status"]
    if payment != "success":
        raise Failure(f"order #{checkout['order_id']} was created but payment status is {payment!r}")
    return {"id": checkout["order_id"], "status": checkout["status"], "total": total, "currency": totals["currency_code"]}


def add_common_arguments(parser):
    parser.add_argument("--url", default="http://store.localhost", help="public URL of the store")
    parser.add_argument("--context", default="k3d-urumi", help="kube context")
    parser.add_argument("--namespace", default="store-main")
    parser.add_argument("--release", default="store", help="Helm release name")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--keep", action="store_true", help="leave the test data in the store")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    args = parser.parse_args()

    print(f"\n== storefront: {args.url}")
    status, _, products = request("GET", f"{args.url}/wp-json/wc/store/v1/products?per_page=20")
    if status != 200:
        raise Failure(f"product listing returned HTTP {status}: {api_error(products)}")
    buyable = [p for p in products if p["is_purchasable"] and p["is_in_stock"]]
    if not buyable:
        raise Failure("the store has no purchasable product to test with")
    product = buyable[0]
    ok(f"{len(products)} product(s) listed, using #{product['id']} {product['name']!r}")

    status, _, page = request("GET", f"{args.url}/")
    if status != 200 or product["name"] not in page:
        raise Failure(f"the front page (HTTP {status}) does not show {product['name']!r}")
    ok("the front page shows it")

    print("\n== guest checkout with cash on delivery")
    placed = place_guest_order(args.url, product["id"], quantity=2)
    ok(f"order #{placed['id']} placed for 2 x {product['name']}: {placed['currency']} {placed['total']:.2f}, "
       f"status {placed['status']!r}")

    print("\n== the order as the shop admin sees it")
    try:
        order = json.loads(wp_cli(args, "wc", "shop_order", "get", str(placed["id"]), "--format=json"))
        problems = []
        if order["payment_method"] != "cod":
            problems.append(f"payment method is {order['payment_method']!r}")
        if order["billing"]["email"] != SHOPPER["email"]:
            problems.append(f"billing email is {order['billing']['email']!r}")
        if abs(float(order["total"]) - placed["total"]) > 0.005:
            problems.append(f"total is {order['total']}, cart said {placed['total']:.2f}")
        if problems:
            raise Failure(f"order #{placed['id']} does not match the checkout: " + "; ".join(problems))
        items = ", ".join(f"{item['quantity']} x {item['name']}" for item in order["line_items"])
        ok(f"order #{placed['id']}: {order['status']}, {order['currency']} {order['total']}, "
           f"{order['payment_method_title']}, {items}")
    finally:
        if not args.keep:
            wp_cli(args, "wc", "shop_order", "delete", str(placed["id"]), "--force=true")
            ok(f"test order #{placed['id']} deleted")

    print("\nStore smoke test passed.")


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
