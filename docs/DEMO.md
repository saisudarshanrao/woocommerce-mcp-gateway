# Demo script

A walk through every line of the assignment's definition of done, in order,
with two users. Each step says what to do and what you should see.

`make gateway-smoke`, `make mcp-smoke`, `make store-smoke` and
`make isolation-smoke` check the same behaviour automatically. This script is
the by-hand version, for the demo video.

## Before you start

```bash
make cluster-up
```

```bash
make up
```

Store the chat model's API key (a free key from Google AI Studio works):

```bash
LLM_API_KEY=<your key> make llm-key
```

Passwords and tokens you will need:

| What | Command |
|---|---|
| Store admin password (user `admin`) | `make store-password` |
| Gateway admin password (user `admin@example.com`) | `make gateway-password` |
| Credential for the tasks server | `make tasks-token` |

## 1. Store

1. Open <http://store.localhost>. Add a product to the cart, go to checkout,
   fill in any address, keep **Cash on delivery**, and place the order.
   *You see "Order received" with an order number.*
2. Open <http://store.localhost/wp-admin/>, sign in, and go to
   **WooCommerce, Orders**. *The order is there, status Processing.*

## 2. Admin: servers and team

1. Open <http://gateway.localhost> and sign in as `admin@example.com`.
2. **Servers.** *"WooCommerce store" is already registered and Connected, with
   six tools named `woo__...`.* Open its tool list.
3. **Add server** twice:

   | Identifier | Name | URL | Authentication |
   |---|---|---|---|
   | `tasks` | Team tasks | `http://tasks-mcp.mcp-servers.svc.cluster.local/mcp` | Custom header `X-API-Key`, value from `make tasks-token` |
   | `sandbox` | Sandbox | `http://sandbox-mcp.mcp-servers.svc.cluster.local/mcp` | None. Set the timeout to 5 seconds. |

   *Both turn Connected. The credential you typed is never shown again: the
   card says only "credential set".* Both servers have a tool called
   `server_info`; the gateway lists them as `tasks__server_info` and
   `sandbox__server_info`.
4. **Team.** Create two invitations, for user A and user B. Open each link in a
   separate private window and create the account.

## 3. Chat agent, as user A

1. In user A's window open **Chat** and ask:
   *"Create a product called Demo Mug priced at 12.50."*
   *You see a `woo__create_product` tool call, then a confirmation.* Reload the
   storefront: the product is there.
2. Buy the Demo Mug on the storefront, as in step 1.
3. Ask: *"What orders came in today?"*
   *You see `woo__list_orders` with `{"period":"today"}`, and the answer names
   the order you just placed.*

## 4. External MCP client, as user B

1. In user B's window open **API keys** and create a key. Copy the
   **Claude Code** command and run it in a terminal. It contains nothing but the
   gateway's URL and the key.
2. In Claude Code run `/mcp`. *The `gateway` server is connected and lists the
   tools of all three servers.*
3. Ask: *"What orders came in today?"* *Same answer as the chat gave user A.*
4. Ask something that needs two servers:
   *"For each order placed today, add a task to pack it."*
   *Claude calls `woo__list_orders` and then `tasks__add_task`.*

No MCP client to hand? MCP Inspector does the same from the command line; the
key page shows the command.

## 5. Team and control

1. **Revoke.** As the admin, open **Team** and revoke user B's key. Ask Claude
   Code anything that needs a tool. *The call is refused: the gateway answers
   HTTP 401.*
2. **Disable.** On **Servers**, disable Sandbox. In user A's chat ask *"which
   tools do you have?"*: no `sandbox__` tools. In an MCP client, list tools
   again (in Claude Code, reconnect with `/mcp`): gone there too. Enable it
   again.
3. **Kill a pod.**

   ```bash
   kubectl -n mcp-servers scale deployment/tasks-mcp --replicas=0
   ```

   *Within about ten seconds the Servers page shows Team tasks as Unreachable,
   with the reason. The store's tools still work in the chat.* Scaling to zero
   keeps the pod gone long enough to see; a plain `kubectl delete pod` is
   undone by the Deployment within a couple of seconds. Bring it back:

   ```bash
   kubectl -n mcp-servers scale deployment/tasks-mcp --replicas=1
   ```

   *It returns to Connected on its own.*
4. **Timeout**, if you want to show it: ask the chat *"call the sandbox slow
   tool for 30 seconds"*. *It fails after 5 seconds with "Sandbox did not
   answer within 5 seconds", and the store's tools keep working meanwhile.*
5. **Audit log.** As the admin open **Audit log**. *Every call above is there:
   user A's from the chat, user B's with their key's prefix, the refused call
   after the revocation, each with server, tool, time, duration and outcome.*
   Sign in as user A: they see only their own. *The table at the top gives
   calls, failures and latency per server for the last 24 hours.*

## 6. Bonus: stores from the dashboard

1. As user A, open **Stores** and create two: *Corner Books* (`books`) and *Toy
   Box* (`toys`). Create the second while the first is still going. *Both show
   Provisioning, each with what it is waiting for, and turn Ready in about half
   a minute, with a link to `http://books.localhost` and `http://toys.localhost`.*
2. *On **Servers**, "Corner Books store" and "Toy Box store" are already there,
   Connected, marked "store", with tools named `store-books__...` and
   `store-toys__...`. Nobody registered them.*
3. In the cluster, each store has its own namespace:

   ```bash
   kubectl get namespaces -l mcp-gateway/store
   kubectl -n store-books get pods,pvc,secret,ingress,networkpolicy,resourcequota
   ```

4. Open both storefronts and buy something in each, as in section 1: one mug in
   Corner Books, three in Toy Box.
5. In the chat: *"Compare today's orders in the Corner Books store and the Toy
   Box store."* *It calls `store-books__list_orders` and
   `store-toys__list_orders` and reports both totals.*
6. On **Stores**, delete Toy Box. *Its server and tools are gone from
   **Servers** and from the chat at once. Within a few seconds:*

   ```bash
   kubectl get namespace store-toys        # not found
   kubectl get pv | grep store-toys        # nothing: the volumes are deleted
   ```

   *Corner Books still answers.* Delete it too when you are done.

`make stores-smoke` runs this section without the chat, and adds what is awkward
to show by hand: killing the provisioner in the middle of two installs, one
store's MCP server failing to connect to the other's database, the limit of
three stores per person, and what the provisioner's own Kubernetes identity is
refused.

## 7. The other extras, briefly

* **Request forgery.** As the admin, try to add a server with the URL
  `http://169.254.169.254/latest/meta-data/` or
  `https://kubernetes.default.svc.cluster.local/`. *Refused, with the reason.*
* **Tool permissions.** Edit a server and set "members may use" to read-only.
  *A member's client lists only its read-only tools; calling another is refused
  and audited as denied.*
* **Sign in with the browser.** Add the gateway to Claude Code with no key:
  `claude mcp add --transport http gateway-oauth http://gateway.localhost/mcp`,
  then run `/mcp` and choose it. *A browser opens on the dashboard's "Allow
  access?" page; after Allow the client is connected as that user, and appears
  under "sign in from the client" on the API keys page, where it can be signed
  out.* (Verified with the MCP SDK's client; not yet with Claude Code itself.)
* **Many tools.** Point an MCP client at `http://gateway.localhost/mcp/search`
  with the same key. *It lists two tools, `find_tools` and `call_tool`, and
  reaches all the others through them.*
* **Metrics.**

  ```bash
  kubectl -n gateway exec deploy/gateway-api -- python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())" | grep '^gateway_'
  ```
