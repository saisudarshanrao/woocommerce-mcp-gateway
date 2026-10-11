# System design and tradeoffs

A short account of how the system is built and why. [PLAN.md](PLAN.md) has the
long version, with every mechanism worked through.

## The four layers

| Layer | What it is | Where it runs |
|---|---|---|
| Store | WordPress + WooCommerce and its MariaDB | namespace `store-main` |
| WooCommerce MCP server (`woo-mcp`) | Six tools over the store's products and orders | `store-main`, beside the store |
| Gateway and dashboard | REST API, the single MCP endpoint, the React dashboard, Postgres | namespace `gateway` |
| Chat agent | A tool loop inside the gateway's API process | `gateway` |

Two more MCP servers (a task list and a sandbox) run in `mcp-servers`. Stores
created from the dashboard each get a namespace `store-<slug>`, and servers an
admin adds by image each get `mcp-<slug>`. Only the storefronts and the gateway
have an Ingress. Everything that calls an MCP server goes through the gateway.

## Architecture choices

Each is a choice between two reasonable options. What the other one would have
bought is stated too.

* **One chart per concern, one namespace per release.** Each chart applies a
  default-deny NetworkPolicy to the namespace it lands in, so isolation is a
  property of installing the chart, not a separate step. The cost is that the
  charts refer to each other by label and namespace name, which values files
  have to keep in agreement.
* **WooCommerce is baked into the store image, and a setup container makes the
  database match it on every start.** A store starts in about thirty seconds
  and every store runs the same pinned versions. Installing plugins at run time
  from the internet would have been less to build, and slower and less
  repeatable. Upgrading WooCommerce means building a new image.
* **`woo-mcp` talks to the store through WooCommerce's REST API**, not WP-CLI or
  the database. It is the supported interface, it applies WooCommerce's own
  validation, and its key can be scoped. Inside the cluster the call is plain
  HTTP, which WooCommerce only accepts with a signed request (OAuth 1.0a), so
  the server signs each one. [The details](../services/woo-mcp/README.md).
* **The gateway keeps no state in memory that matters.** Users, keys, servers,
  the audit log and rate-limit counters are in Postgres, and the MCP endpoint
  runs without sessions. Any replica can answer any request, which is what
  makes two replicas work. The cost is a database lookup on every request; that
  lookup is also what makes revoking a key or disabling a server immediate.
* **No connection to an upstream outlives a request.** Each health check and
  each tool call opens its own short connection. Nothing has to be repaired
  when an upstream pod is replaced. A long-lived session per upstream would
  save a handshake on older servers and would tie clients to one replica.
* **Tool names are always `<server>__<tool>`**, not only when two collide, so
  adding a server never renames a tool that clients, permissions or audit
  history already refer to.
* **The chat calls the model through an OpenAI-compatible HTTP API with a
  hand-written loop**, not a vendor SDK or an agent framework. The provider is
  three settings, and the whole loop is one readable file. It does not stream
  the model's words as they are produced, only whole steps.
* **Stores and hosted servers are created by a separate provisioner.** The API
  only records what is wanted in a table; the provisioner, which alone holds
  Kubernetes credentials, makes the cluster match. The internet-facing process
  cannot touch the cluster. The cost is a second component and a delay of up to
  two seconds before work starts.

## How tool aggregation, routing and failure handling work

**Aggregation.** A background task in each gateway replica checks every
enabled server every ten seconds, all at once, each with a five-second limit:
it connects, lists the server's tools, and keeps the result. Listing tools for
a client reads the registry from Postgres, takes each server's tools from that
kept result, drops what this user's role may not use, and returns them under
prefixed names. It never waits on an upstream, so a slow or dead server cannot
hold the list up.

**Routing.** A call to `woo__list_orders` is split at the first `__`. A server
identifier cannot contain an underscore, so the split is unambiguous even if
the tool's own name contains `__`. The gateway looks the server up, checks that
it is enabled and reachable and that the user may use that tool, attaches the
server's own credential, and calls it. Two servers with a tool of the same name
are simply two different prefixed names.

**Failure.** Each call has a deadline, the server's own timeout setting.
Failures are sorted into three kinds, because they mean different things:

| Kind | Example | What happens |
|---|---|---|
| unreachable | connection refused, name does not resolve, credential rejected | The call fails with the reason, and the server is marked unreachable at once, with that reason shown in the dashboard |
| timeout | connected, then no answer in time | The call fails after its deadline. The server's status does not change: one slow answer is not a dead server |
| error | the tool ran and reported a failure | Passed back as the tool's own error |

A failed call is still a normal MCP result with `isError` set and a sentence
explaining it, so the model can read the reason and tell the user. Every call,
whatever its outcome, is written to the audit log. A server that comes back is
picked up by the next check without anyone doing anything.

## What changes for production

Only values, and how images reach the cluster. [DEPLOY.md](DEPLOY.md) has the
steps and what a rehearsal of them found.

| | Local | Production |
|---|---|---|
| DNS | `*.localhost`, which resolves to the machine itself | Real names, plus a wildcard for created stores |
| Ingress | Traefik, plain HTTP | Traefik, HTTPS; certificates from cert-manager; HTTP redirected |
| Storage class | k3s's `local-path`, 1 Gi volumes | The cluster's default (`local-path` on k3s), 10 Gi; another class by setting `persistence.storageClass` |
| Secrets | Generated by the charts into Kubernetes Secrets on first install, kept across upgrades | The same, or your own Secret named in `existingSecret` |
| Images | A registry beside the cluster | Your registry, or copied to the server over SSH |
| Gateway replicas | 1 | 2 |

What is particular to one deployment, its hostnames and emails, lives outside
the charts in `deploy/sites/<name>`.

## Limits worth knowing

* Health is per replica, so two replicas can disagree about a server for up to
  one check interval. Key revocation and disabling a server do not have this
  delay: they are read from the database on every request.
* The store's content volume can be mounted by one pod at a time, so upgrading
  a store is a short outage, not a rolling update.
* The chat agent keeps no conversation on the server; the browser sends the
  whole conversation each time.
* Store emails are not delivered: the cluster has no mail server.
