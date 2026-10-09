# WooCommerce MCP server

An MCP server that exposes one WooCommerce store's products and orders as tools.
Each store gets its own, deployed by the [store chart](../../charts/store). It
speaks MCP over Streamable HTTP and is called only by the gateway.

## Tools

| Tool | What it does | Changes data |
|---|---|---|
| `list_products` | Products, newest first, with search and status filters | no |
| `get_product` | One product by ID | no |
| `create_product` | Creates a simple product; published ones appear on the storefront at once | yes |
| `list_orders` | Orders, newest first, by named period (`today`, `yesterday`, `last_7_days`, ...) or explicit dates, and by status | no |
| `get_order` | One order in full: items, totals, customer, addresses | no |
| `update_order_status` | Changes an order's status and can record a private note | yes |

Every tool declares whether it is read-only (MCP tool annotations), so a client
or the gateway can treat the two that change data differently.

## How it talks to the store, and why

Through the WooCommerce REST API (`/wp-json/wc/v3`), over plain HTTP to the
WordPress Service inside the cluster.

* **It is WooCommerce's supported contract.** A product created or an order
  updated through it goes through the same validation, stock handling, order
  notes and emails as one changed in wp-admin.
* **The coupling is network-only.** The server needs no access to WordPress's
  files, to the database or to the Kubernetes API. That is what lets it run as a
  small stateless container fenced in by a NetworkPolicy.
* **Access is a revocable API key** owned by a dedicated `mcp-bot` user with the
  shop manager role, not by the site administrator.

Rejected: WP-CLI would need either `kubectl exec` rights (cluster credentials
inside this server) or WordPress's volume and database password mounted into
it. Direct SQL would bypass WooCommerce's own logic. Both give up the isolation.

### Request signing

WooCommerce accepts its key pair as HTTP Basic auth only over HTTPS. TLS ends at
the ingress, so inside the cluster the store is reached over plain HTTP and each
request is signed instead, with WooCommerce's one-legged OAuth 1.0a
([oauth.py](src/woo_mcp/oauth.py)). The consumer secret is the HMAC key and
never leaves this process. Each request carries a timestamp and a one-time
nonce, so a captured request cannot be replayed: WooCommerce answers a reused
nonce or a stale timestamp with 401.

One detail matters. WooCommerce verifies the signature against the URL it
rebuilds from WordPress's own "home" URL, not against the address the request
arrived on. So requests are *sent to* `http://store-wordpress` but *signed for*
`http://store.localhost/...`. The two are separate settings for that reason.

## What protects it

1. **No address outside the cluster.** A ClusterIP Service, no Ingress.
2. **NetworkPolicy, both directions.** Only the gateway's pods may connect. The
   server itself may connect only to its own WordPress and to cluster DNS: not to
   the database, the Kubernetes API, the internet or another store.
3. **A bearer token** on every MCP request, compared in constant time. Only the
   gateway is given it. A pod that got past the network rule still gets a 401.
4. **A Host header allowlist**, the MCP SDK's guard against DNS rebinding. The
   server answers only to its own Service names, and to localhost so that a
   port-forward works.

`make isolation-smoke` tries each of these connections and checks which open.

## The protocol on the wire

MCP is JSON-RPC over HTTP POST. The current revision of the protocol,
`2026-07-28`, has no handshake and no session: every request stands alone and
names the protocol version and the client inside itself. A tool call is one
request and one response (abridged here):

```
POST /mcp
Authorization: Bearer <token>
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: list_orders

{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"list_orders","arguments":{"period":"today"},
           "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
                    "io.modelcontextprotocol/clientInfo":{"name":"gateway","version":"1"},
                    "io.modelcontextprotocol/clientCapabilities":{}}}}
```

```
{"jsonrpc":"2.0","id":2,
 "result":{"isError":false,
           "content":[{"type":"text","text":"{ \"orders\": [...] }"}],
           "structuredContent":{"orders":[...],"total":1,"period":{...}}}}
```

`content` is text for a language model. `structuredContent` is the same data as
JSON for a program, matching the tool's published output schema. A tool that
fails still returns a result, with `isError: true` and a message that explains
the failure to the model.

Clients written for the older protocol (an `initialize` handshake first) are
served too. The server keeps no session for them either, so any replica can
answer any request and a restarted pod loses nothing.

[scripts/smoke_mcp.py](../../scripts/smoke_mcp.py) contains a complete client for
this in about fifty lines of standard-library Python.

To try a call by hand, tunnel to the Service in one terminal:

```bash
kubectl -n store-main port-forward service/store-woo-mcp 8090:80
```

and call it in another:

```bash
curl -s http://127.0.0.1:8090/mcp \
  -H "Authorization: Bearer $(kubectl -n store-main get secret store-secrets -o jsonpath='{.data.mcp-auth-token}' | base64 -d)" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_orders","arguments":{"period":"today"}}}'
```

The port-forward is an operator's tunnel through the Kubernetes API. It needs
cluster credentials and is not a route a pod or an outside client can take.

## Design notes

* **"Today" is computed here, not by the model.** `list_orders` takes a named
  period and works out the time range in the store's timezone
  ([dates.py](src/woo_mcp/dates.py)). The range is sent to WooCommerce in UTC.
* **Results are trimmed.** WooCommerce returns hundreds of fields per order; the
  tools return the ones that answer questions ([models.py](src/woo_mcp/models.py)).
* **Input schemas are flat.** Arguments are strings, numbers and enums, with no
  nullable unions, so every model API accepts the tool definitions unchanged.
* **Failures explain themselves.** A missing order, an unreachable store and a
  rejected API key each produce a different sentence
  ([woocommerce.py](src/woo_mcp/woocommerce.py)).
* **Health does not depend on the store.** If the store is down the tools say
  so; the pod stays in its Service.

## Configuration

All from environment variables, set by the chart.

| Variable | Meaning |
|---|---|
| `STORE_INTERNAL_URL` | Where requests are sent, for example `http://store-wordpress` |
| `STORE_PUBLIC_URL` | The store's own URL, which signatures are computed for. Defaults to the internal URL. |
| `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET` | The WooCommerce API key pair, from the store's Secret |
| `MCP_AUTH_TOKEN` | The bearer token callers must present, from the store's Secret |
| `MCP_ALLOWED_HOSTS` | Comma-separated Host names to answer to. Defaults to localhost names. |
| `PORT`, `LOG_LEVEL`, `STORE_REQUEST_TIMEOUT_SECONDS` | Defaults `8080`, `INFO`, `15` |

## Development

Needs [uv](https://docs.astral.sh/uv/). From this directory:

```bash
uv run pytest
```

The tests run the real tools through a real MCP client, in memory, against a
fake store, so they need no cluster. From the repository root, `make mcp-smoke`
runs the same flow against the deployed server and the real store.
