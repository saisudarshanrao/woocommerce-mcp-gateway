# WooCommerce hosting with a team MCP gateway

A WooCommerce store on Kubernetes, an MCP server that exposes its products and
orders as tools, and a gateway that puts every registered MCP server behind one
endpoint. An admin connects servers once in a dashboard; every team member then
uses all of them from an MCP client or from the dashboard's chat, with their own
API key and without ever seeing an upstream credential.

Everything runs in the cluster and is installed with Helm. The same charts
deploy to a local k3d cluster and to a k3s server, with only values changing.

```
Browser ────────────┐                       ┌─ namespace: gateway ──────────────┐
MCP client ─────────┤  Traefik Ingress      │ dashboard   React, served by nginx │
(Claude Code, ...)  ├─ store.localhost   ┌──┤ api         REST, /mcp, chat agent │
                    │  gateway.localhost │  │ postgres    provisioner            │
                    │                    │  └──────┬──────────────────────────────┘
                    │                    │         │ MCP, with each server's own credential
                    │  ┌─ namespace: store-main ───▼──┐   ┌─ namespace: mcp-servers ─┐
                    └──┤ wordpress   <──  woo-mcp     │   │ tasks-mcp   sandbox-mcp  │
                       │ mariadb                      │   └──────────────────────────┘
                       └──────────────────────────────┘
```

Only the storefront and the gateway have an Ingress. The gateway is the only
thing allowed to call an MCP server, for external clients and for the chat alike.

Beyond the core, the gateway also creates stores: a signed-in user asks for one
in the dashboard, and a provisioner in the `gateway` namespace installs the same
store chart into a new namespace `store-<slug>` and registers the store's MCP
server automatically. The other extras that are in: per-server tool permissions,
a guard against server-side request forgery, rate limits per key, Prometheus
metrics, a two-tool "find, then call" endpoint for when there are too many
tools to list, signing in from an MCP client with a browser (OAuth) as an
alternative to pasting an API key, and MCP servers the platform runs itself
from a container image an admin names. [Bonus and stand-out items](#bonus-and-stand-out-items)
lists each one, its state and where to find it.

## Documentation

| | |
|---|---|
| [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md) | The brief this is built against |
| [docs/DESIGN.md](docs/DESIGN.md) | **System design and tradeoffs**: architecture choices; tool aggregation, routing and failure handling; what changes for production |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | **Threat model**: shared upstream credentials, token passthrough, untrusted upstreams, what one member can do to another |
| [docs/PLAN.md](docs/PLAN.md) | The long design record: every mechanism, and what was learned building it |
| [docs/VIDEO.md](docs/VIDEO.md) | Outline for the demo video: what to show and say for each required area |
| [docs/DEMO.md](docs/DEMO.md) | The definition of done, step by step, with two users |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deploying to a VPS, rehearsing that on your own machine, upgrading and rolling back |
| [services/gateway/README.md](services/gateway/README.md) | How a tool call flows; the naming scheme; timeouts; credentials |
| [services/woo-mcp/README.md](services/woo-mcp/README.md) | How the MCP server talks to the store and why; MCP on the wire |

## Run it locally

### Prerequisites

Docker, [k3d](https://k3d.io), kubectl, Helm and Python 3. On macOS with Homebrew:

```bash
brew install colima docker docker-buildx kubernetes-cli helm k3d
```

Docker needs to be told where Homebrew puts the buildx plugin. Add this to
`~/.docker/config.json`:

```json
{ "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"] }
```

Then start the Docker VM. This is also the command to run after a reboot:

```bash
colima start --cpu 4 --memory 8 --disk 40
```

Docker Desktop or any other Docker engine works in place of Colima. Running the
unit tests also needs [uv](https://docs.astral.sh/uv/) (`brew install uv`);
building and deploying does not.

### Start everything

```bash
make cluster-up
```

```bash
make up
```

`make up` builds six images, pushes them to the cluster's own registry and
installs the three charts. The first run takes a few minutes, most of it
downloading base images; from a fresh clone with those already present it was
measured at about three and a half minutes for both commands. The whole system
uses about 2 GB of memory.

| URL | What | Sign in |
|---|---|---|
| <http://store.localhost> | Storefront | |
| <http://store.localhost/wp-admin/> | WooCommerce admin | `admin`, password from `make store-password` |
| <http://gateway.localhost> | Dashboard | `admin@example.com`, password from `make gateway-password` |
| `http://gateway.localhost/mcp` | The MCP endpoint | An API key, created in the dashboard |

The chat needs a model API key. Any OpenAI-compatible provider works; the
default is Gemini, whose free tier is enough:

```bash
LLM_API_KEY=<your key> make llm-key
```

The provider and model are `llm.baseUrl` and `llm.model` in
[charts/gateway/values.yaml](charts/gateway/values.yaml). Without a key
everything else works and the chat page says it is switched off.

### Check that it works

```bash
make smoke
```

runs six end-to-end checks against the cluster:

| Target | What it proves |
|---|---|
| `make store-smoke` | A guest can buy a product with cash on delivery, and the admin sees the order |
| `make mcp-smoke` | The store's MCP tools create a product, list today's orders, update an order |
| `make gateway-smoke` | The two-user flow: add servers, invite a member, use every server with one key, timeouts, a dead server, disable, revoke, audit log |
| `make isolation-smoke` | Which connections between namespaces open and which are refused |
| `make stores-smoke` | Two stores created at once from the dashboard's API, with the provisioner killed midway; their orders compared through the gateway; one store unable to reach the other; what the provisioner may do in the cluster; a store deleted, tools and volumes gone |
| `make hosted-smoke` | A server added by container image: deployed into its own namespace with a Secret, limits and NetworkPolicy; used through the gateway; unreachable by anything else; changed; removed |

```bash
make chat-smoke
```

is separate, because it calls the real model and so needs the key and uses its
quota: the chat agent creates a product, which appears on the storefront, then
reports an order placed there. `make chat-smoke STORES=1` adds the bonus's test,
the agent comparing today's orders across two freshly created stores.

```bash
make test
```

runs the unit tests: the gateway's start a throwaway Postgres in Docker and run
fake upstream servers in-process.

To try the production deployment without renting a server,
`scripts/vps-rehearsal.sh` builds a stand-in for one: a separate Ubuntu VM with
k3s, HTTPS and the production values. [docs/DEPLOY.md](docs/DEPLOY.md) explains.

`make help` lists every target. `make down` removes everything; `make
cluster-down` deletes the cluster.

## Use it

[docs/DEMO.md](docs/DEMO.md) walks the whole definition of done with two users.
The three things in short:

### Place an order

Open <http://store.localhost>, add a product to the cart, go to checkout, fill
in any address and choose **Cash on delivery**. The order then appears under
WooCommerce, Orders at <http://store.localhost/wp-admin/> (user `admin`,
password from `make store-password`).

### Connect an external MCP client

In the dashboard open **API keys**, create a key and copy it; it is shown once.
The page prints the command for each client with your key filled in. For
Claude Code:

```bash
claude mcp add --transport http gateway http://gateway.localhost/mcp --header "Authorization: Bearer <your key>"
```

For Cursor and other clients configured with JSON:

```json
{ "mcpServers": { "gateway": { "url": "http://gateway.localhost/mcp", "headers": { "Authorization": "Bearer <your key>" } } } }
```

With MCP Inspector, from the command line:

```bash
npx @modelcontextprotocol/inspector --cli http://gateway.localhost/mcp --header "Authorization: Bearer <your key>" --method tools/list
```

The client then lists the tools of every enabled server, named
`<server>__<tool>`. A client that supports it can also be given the URL alone
and sign in through the browser.

### Use the chat agent

Set the model's key with `make llm-key` (above), open **Chat** in the
dashboard, and ask, for example, *"Create a product called Demo Mug priced at
12.50"* or *"What orders came in today?"*. Each tool the agent calls is shown
in the conversation, and in the audit log under your name.

## Deploy to a server

The same charts, with the production values and one directory of your own
settings. [docs/DEPLOY.md](docs/DEPLOY.md) has the reasoning, upgrades and
rollback; these are the steps.

1. Install k3s on the server and add it to your kubeconfig:

   ```bash
   curl -sfL https://get.k3s.io | sh -s - --tls-san <the server's address>
   ```

2. Point DNS names at it: one for the store, one for the gateway, and a
   wildcard for stores created from the dashboard.
3. Copy `deploy/sites/example` to `deploy/sites/<name>` and put your hostnames
   and email in its files.
4. Turn on the HTTPS redirect and certificates:

   ```bash
   kubectl apply -f deploy/sites/<name>/traefik-https-redirect.yaml
   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.yaml
   kubectl apply -f deploy/sites/<name>/cluster-issuer.yaml
   ```

5. Build, deliver and install everything. With a registry:

   ```bash
   make up ENV=prod SITE=<name> KUBE_CONTEXT=<context> REGISTRY=ghcr.io/<you> PLATFORM=linux/amd64
   ```

   Or with no registry, copying the images to the server over SSH:

   ```bash
   make up ENV=prod SITE=<name> KUBE_CONTEXT=<context> REGISTRY=images.local SHIP_TO=<user>@<server> PLATFORM=linux/amd64
   ```

6. Give the chat its key, and check the deployment:

   ```bash
   LLM_API_KEY=<key> make llm-key KUBE_CONTEXT=<context>
   make smoke KUBE_CONTEXT=<context> STORE_URL=https://<store host> GATEWAY_URL=https://<gateway host>
   ```

This has been run end to end against a separate virtual machine with k3s,
production values and HTTPS, which `scripts/vps-rehearsal.sh` sets up on your
own machine. It has not been run on a rented server with a public address.

## Bonus and stand-out items

All were attempted. "Check" is the command that exercises it.

| Item from the brief | State | Where to find it | Check |
|---|---|---|---|
| **Bonus: multi-store provisioning** | Done | [PLAN.md 4.10](docs/PLAN.md), `services/gateway/src/gateway/provisioner/`, `api/stores.py`, `charts/gateway/templates/provisioner.yaml`, `charts/store/templates/quota.yaml`, the dashboard's Stores page | `make stores-smoke`, `make chat-smoke STORES=1` |
| 1. Production-like VPS deployment | Rehearsed on a separate VM with k3s and TLS from cert-manager; not on a public server | [DEPLOY.md](docs/DEPLOY.md), `scripts/vps-rehearsal.sh`, `deploy/sites/` | `scripts/vps-rehearsal.sh`, then the two commands it prints |
| 2. Tool permissions | Done per server: every tool, read-only tools, or none for members | `core.py` (`may_use`), the server form in the dashboard | `make gateway-smoke` |
| 3. OAuth on the gateway | Done, alongside API keys. Tested with the MCP SDK's client, not yet with Claude Code or Cursor | [PLAN.md 4.12](docs/PLAN.md), `oauth.py`, `api/oauth.py`, the Consent page | `make gateway-smoke`, `tests/test_oauth.py` |
| 4. Handling many tools | Done: `find_tools` and `call_tool` at `/mcp/search`, and the chat switches to them above 40 tools | [PLAN.md 4.11](docs/PLAN.md), `toolsearch.py` | `make gateway-smoke` |
| 5. Hosted MCP servers | Done: add a server by container image | [PLAN.md 4.13](docs/PLAN.md), `hosted.py`, `charts/mcp-server` | `make hosted-smoke` |
| 6. Security hardening | Done: request-forgery guard in the application and the network, least-privilege RBAC with an admission policy, default-deny NetworkPolicies, non-root containers, credentials encrypted at rest | [THREAT-MODEL.md](docs/THREAT-MODEL.md), [PLAN.md 4.8](docs/PLAN.md), `urlguard.py`, each chart's `networkpolicy` template | `make isolation-smoke`, `make gateway-smoke` |
| 7. Scaling | Done: verified with two gateway replicas; rate limit per key held in Postgres | [PLAN.md 4.4 and 4.9](docs/PLAN.md), `core.py`, `charts/gateway/values-prod.yaml` | `make gateway-smoke` against two replicas; `tests/test_core.py` |
| 8. Observability | Done: Prometheus metrics, per-server figures in the dashboard, plain reasons for failures. No Prometheus is deployed | [PLAN.md 4.9](docs/PLAN.md), `metrics.py`, the Audit log page | `make gateway-smoke` |
| 9. Upgrades and rollback | Documented, and a rolling update of the gateway measured to drop no requests | [DEPLOY.md](docs/DEPLOY.md) | the `helm` commands in [VIDEO.md](docs/VIDEO.md) section 6 |

## Local domains

`*.localhost` resolves to loopback on its own (RFC 6761) in browsers, curl and
the macOS and Linux resolvers, so there is no `/etc/hosts` entry or local DNS
server to set up. k3d publishes ports 80 and 443 on `127.0.0.1` only, so the
stack is not reachable from the network.

Production uses real DNS names and TLS, set in each chart's `values-prod.yaml`.

## What is in the cluster

| Namespace | Chart | Workloads |
|---|---|---|
| `store-main` | [charts/store](charts/store) | WordPress + WooCommerce, MariaDB, the store's MCP server, a cron job |
| `gateway` | [charts/gateway](charts/gateway) | The gateway API, the dashboard, Postgres, the store provisioner |
| `mcp-<slug>` | [charts/mcp-server](charts/mcp-server), installed by the provisioner | One per MCP server an admin adds by container image |
| `store-<slug>` | [charts/store](charts/store), installed by the provisioner | One per store created in the dashboard: the same workloads as `store-main`, plus a ResourceQuota and LimitRange |
| `mcp-servers` | [charts/mcp-server](charts/mcp-server), installed twice | A task-list server and a sandbox server |

Every workload has readiness and liveness probes and resource requests and
limits, runs as a non-root user on a read-only root filesystem with all
capabilities dropped, and sits behind a NetworkPolicy. Every credential is
generated into a Kubernetes Secret on first install; none is in a values file
or in git.

`helm uninstall` keeps the volumes and Secrets, so reinstalling brings the same
data back. Deleting the namespace removes them.

Two limits to know about. WooCommerce's order emails are not delivered, because
the cluster has no mail server. Volume sizes cannot be changed after the first
install, because k3s's local-path storage cannot resize.

## Layout

```
charts/
  store/        One WooCommerce store, with its database and MCP server
  gateway/      The gateway API, the dashboard, Postgres and the store provisioner
  mcp-server/   Any MCP server image, reachable only by the gateway
services/
  gateway/      The gateway and the provisioner (Python, FastAPI)
  dashboard/    The dashboard (React, TypeScript)
  woo-mcp/      The WooCommerce MCP server (Python)
  demo-mcp/     Two small MCP servers: tasks and sandbox (Python)
images/
  wordpress/    The store image: Dockerfile, wp-config.php, setup script
deploy/local/   The k3d cluster definition
deploy/sites/   One directory per production deployment: its hostnames and emails
scripts/        Cluster bring-up, image builds, end-to-end checks
docs/           The assignment, the design, the demo script, deployment
```

Each chart has `values.yaml` for the shared baseline, `values-local.yaml` and
`values-prod.yaml`.
