# WooCommerce hosting with a team MCP gateway

A WooCommerce store on Kubernetes, a self-written WooCommerce MCP server, and a
gateway that puts every registered MCP server behind one endpoint with per-user
API keys, an audit log, a dashboard and a chat agent.

* Spec: [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md)
* Design and build order: [docs/PLAN.md](docs/PLAN.md)

**Status:** the local cluster is set up and verified. Charts and services come next.

## Prerequisites

Docker, [k3d](https://k3d.io), kubectl and Helm. On macOS with Homebrew:

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

Docker Desktop or any other Docker engine works in place of Colima.

## Local cluster

```bash
make cluster-up      # create the k3d cluster, or start it if it exists
make cluster-smoke   # verify registry, storage, ingress and NetworkPolicy
make cluster-status
make cluster-stop    # stop, keep data
make cluster-down    # delete everything
```

The cluster is defined in [deploy/local/k3d.yaml](deploy/local/k3d.yaml): one k3s
node pinned to the version a VPS gets from the k3s `stable` channel, with k3s's
own Traefik, ServiceLB, local-path storage and NetworkPolicy controller left as
they are. Images are pushed to and pulled from `localhost:5001`.

## Local domains

| URL | What |
|---|---|
| `http://store.localhost` | Storefront and wp-admin |
| `http://gateway.localhost` | Dashboard, `/api`, and the MCP endpoint at `/mcp` |

`*.localhost` resolves to loopback on its own (RFC 6761) in browsers, curl and the
macOS and Linux resolvers, so there is no `/etc/hosts` entry or local DNS server
to set up. k3d publishes ports 80 and 443 on `127.0.0.1` only, so the stack is not
reachable from the network.

Production uses real DNS names and TLS, set in each chart's `values-prod.yaml`.
