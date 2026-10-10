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
                    │  gateway.localhost │  │ postgres                           │
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
tools to list, and signing in from an MCP client with a browser (OAuth) as an
alternative to pasting an API key. [docs/PLAN.md](docs/PLAN.md) section 7 says what is and is not
done.

## Documentation

| | |
|---|---|
| [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md) | The brief this is built against |
| [docs/PLAN.md](docs/PLAN.md) | Design decisions and the reasoning behind each |
| [docs/DEMO.md](docs/DEMO.md) | The definition of done, step by step, with two users |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deploying to a VPS, upgrading and rolling back |
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

`make up` builds five images, pushes them to the cluster's own registry and
installs the three charts. The first run takes a few minutes. The whole system
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

[docs/DEMO.md](docs/DEMO.md) walks through using it.

### Check that it works

```bash
make smoke
```

runs five end-to-end checks against the cluster:

| Target | What it proves |
|---|---|
| `make store-smoke` | A guest can buy a product with cash on delivery, and the admin sees the order |
| `make mcp-smoke` | The store's MCP tools create a product, list today's orders, update an order |
| `make gateway-smoke` | The two-user flow: add servers, invite a member, use every server with one key, timeouts, a dead server, disable, revoke, audit log |
| `make isolation-smoke` | Which connections between namespaces open and which are refused |
| `make stores-smoke` | Two stores created at once from the dashboard's API, with the provisioner killed midway; their orders compared through the gateway; one store unable to reach the other; what the provisioner may do in the cluster; a store deleted, tools and volumes gone |

```bash
make test
```

runs the unit tests: the gateway's start a throwaway Postgres in Docker and run
fake upstream servers in-process.

`make help` lists every target. `make down` removes everything; `make
cluster-down` deletes the cluster.

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
scripts/        Cluster bring-up, image builds, end-to-end checks
docs/           The assignment, the design, the demo script, deployment
```

Each chart has `values.yaml` for the shared baseline, `values-local.yaml` and
`values-prod.yaml`.
