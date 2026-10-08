# WooCommerce hosting with a team MCP gateway

A WooCommerce store on Kubernetes, a self-written WooCommerce MCP server, and a
gateway that puts every registered MCP server behind one endpoint with per-user
API keys, an audit log, a dashboard and a chat agent.

* Spec: [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md)
* Design and build order: [docs/PLAN.md](docs/PLAN.md)

**Status:** the local cluster and the store (WordPress, WooCommerce, MariaDB) are
built and verified. The WooCommerce MCP server, gateway, dashboard and chat agent
come next.

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

## Store

```bash
make store-up        # build the image, install the chart, wait until ready
make store-smoke     # place a guest order and check the admin can see it
make store-password  # print the WordPress admin password
make store-status
make store-down      # remove the store and delete its data
```

`make store-up` builds [images/wordpress](images/wordpress), pushes it to the
local registry and installs [charts/store](charts/store) into the `store-main`
namespace. The first start installs WordPress and WooCommerce and takes about a
minute. After that:

* Storefront: <http://store.localhost>, with three sample products and cash on
  delivery enabled.
* Admin: <http://store.localhost/wp-admin/>, user `admin`, password from
  `make store-password`. Orders are under WooCommerce, Orders.

What the chart creates, all in one namespace:

| Object | Purpose |
|---|---|
| StatefulSet `store-mariadb` + volume | The database |
| Deployment `store-wordpress` + volume | Apache and PHP, with `wp-content` on the volume |
| CronJob `store-wordpress-cron` | Triggers WordPress's scheduled tasks every five minutes |
| Secret `store-secrets` | Every credential, generated on first install |
| Ingress `store` | `store.localhost` to WordPress |
| NetworkPolicies | Deny by default. Traefik and the cron job may reach WordPress; only WordPress may reach MariaDB; MariaDB may open no connection at all. |

`helm uninstall` keeps the volumes and the Secret, so reinstalling brings the
same store back. `make store-down` deletes the namespace and with it the data.

Two limits to know about. Order emails are not delivered, because the cluster
has no mail server. Volume sizes cannot be changed after the first install.

The design, and why each piece is the way it is, is in
[docs/PLAN.md](docs/PLAN.md#47-how-a-store-pod-is-put-together).

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

## Layout

```
charts/store/       Helm chart for one store: values.yaml plus values-local.yaml and values-prod.yaml
images/wordpress/   The store image: Dockerfile, wp-config.php, setup script
deploy/local/       k3d cluster definition
scripts/            Cluster bring-up, image build, smoke tests
docs/               The assignment and the plan
```
