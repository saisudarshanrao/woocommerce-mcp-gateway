# Gateway

One MCP endpoint in front of many MCP servers. Admins register servers once;
every team member then reaches all of them with one URL and their own API key,
and the dashboard's chat agent uses the same tools.

The gateway is an MCP **client** to each registered server and an MCP **server**
to everyone else. It is also the REST API behind the [dashboard](../dashboard).

## One tool call, end to end

A team member's MCP client calls `woo__list_orders`:

1. **Ingress.** `POST https://gateway.example.com/mcp` reaches Traefik, which
   forwards it to a gateway pod. Nothing else in the cluster has an Ingress.
2. **Who is calling.** The API key in the `Authorization` header is hashed and
   looked up ([mcp_endpoint.py](src/gateway/mcp_endpoint.py)). Unknown, revoked,
   or belonging to a deactivated member: HTTP 401. The lookup happens on every
   request, which is why revoking a key works on the very next call.
3. **Which server.** The name is split at the first `__` into the server's slug
   and the tool's own name ([naming.py](src/gateway/naming.py)). The server is
   read from the database: missing or disabled ends the call here.
4. **May they.** Admins may use every tool. For members it depends on the
   server's setting: every tool, read-only tools, or none
   ([core.py](src/gateway/core.py)).
5. **The upstream call.** The gateway opens a connection to the server,
   attaching the server's stored credential, calls the tool, and closes the
   connection, all inside the server's timeout
   ([upstreams.py](src/gateway/upstreams.py)).
6. **The answer.** The upstream's result is passed back unchanged. A failure
   anywhere above becomes a result with `isError` and a sentence saying what
   went wrong, so a model can read it.
7. **The audit row.** Written in every case: who, which key, which server and
   tool, when, how long, and how it ended.

The chat agent enters at step 3 with the signed-in user in place of an API key.
From there it is the same code.

## Tool names

Two servers may both have a tool called `list_orders`. Every tool is exposed as
`<server slug>__<tool>`:

* **Always, not only on collision.** Registering a server never renames a tool
  that clients, permissions or audit history already refer to.
* **Unambiguous.** A slug is lower-case letters, digits and hyphens. It cannot
  contain an underscore, so the first `__` is always the separator.
* **Portable.** The result uses only letters, digits, `_` and `-`, in at most 64
  characters, which every major model API accepts as a function name. A tool
  that cannot fit is left out and listed in the dashboard with the reason.
* **Fixed.** The slug is chosen when the server is registered and cannot be
  edited.

Each description is prefixed with the server's name in brackets, so a model can
tell two similar tools apart.

## Slow or dead upstreams

* A background task checks every enabled server every ten seconds, all at once
  and each with its own five-second limit. It records whether the server
  answered, why not if it did not, and its tool list.
* Listing tools only reads what those checks found. It never waits on an
  upstream, so a dead one cannot delay it.
* Only servers that answered their last check have their tools listed.
* A call has the server's timeout (30 seconds by default, set per server). The
  gateway tells three failures apart:

  | Outcome | Meaning | Effect |
  |---|---|---|
  | `unreachable` | Refused, unresolvable, wrong credential, no MCP endpoint | The server is marked unreachable at once, with the reason |
  | `timeout` | It took the request and did not answer in time | Only this call fails; the server stays connected |
  | `error` | It answered with a failure | Passed back to the caller |

* No connection outlives a request. There is no session to repair when an
  upstream pod is replaced. A server that speaks MCP's current revision needs no
  handshake; an older one gets a fresh handshake per call.

## Credentials

| Credential | Stored as | Never reaches |
|---|---|---|
| User password | Argon2id hash | anyone |
| API key | SHA-256 hash, plus its first 12 characters for display. Shown once. | upstream servers |
| Dashboard session | SHA-256 hash; the browser holds an HttpOnly cookie | page script |
| Invitation link | SHA-256 hash; works once | anyone after use |
| Upstream credential | Encrypted with a key held in a Kubernetes Secret, not in the database | the browser and MCP clients |
| Model API key | Kubernetes Secret | the browser |

Passwords get a slow hash because people choose them. Keys and tokens are 256
random bits, so a fast hash is enough and can be looked up by index on every
request. An upstream credential must be sent again, so it is encrypted rather
than hashed ([security.py](src/gateway/security.py)). The REST API reports only
whether a credential is set.

A request from the dashboard that changes something must carry the gateway's
own address in its `Origin` header. That stops another website from using a
signed-in browser's cookie.

## The chat agent

[chat/agent.py](src/gateway/chat/agent.py) is the loop:

1. Ask the model, giving it the conversation and the tools this user may call.
2. If it answers in words, that is the reply.
3. If it asks for tools, run each through the gateway and add the results.
4. Ask again, up to eight rounds.

The model is reached through the OpenAI-compatible chat completions API that
Gemini, Groq, OpenRouter and Ollama all offer
([chat/llm.py](src/gateway/chat/llm.py)), so the provider is three settings: a
base URL, a model name and an API key. MCP tool schemas are reduced to the
subset of JSON Schema that every provider accepts.

The browser holds the conversation and sends it whole with each message. The
gateway keeps no chat state. Without a model API key the gateway runs normally
and the chat page says it is switched off.

## More than one replica

The API keeps nothing in memory that another replica would need:

* Users, keys, sessions, servers and the audit log are in Postgres.
* MCP requests carry no session.
* A replica asked about a server it has not seen yet, because another replica
  registered or edited it a moment ago, checks that server on the spot.

Each replica runs its own health checks, so after an upstream dies two replicas
can disagree about it for up to one check interval. Revoking a key and
disabling a server are read from the database on every request and take effect
everywhere at once.

## REST API

All under `/api`, authenticated by the session cookie. Interactive
documentation is served at `/api/docs`.

| Area | Endpoints |
|---|---|
| Sign-in | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Invitations | `POST /invites` (admin), `GET /invites/{token}`, `POST /invites/{token}/accept` |
| Team (admin) | `GET /members`, `PATCH /members/{id}`, `DELETE /members/{id}` |
| API keys | `GET /keys`, `POST /keys`, `DELETE /keys/{id}` |
| Servers | `GET /servers`; admin: `POST` (by `url`, or by `hosted` image), `PATCH`, `DELETE`, `POST /servers/{id}/refresh`, `POST /servers/{id}/redeploy` |
| Audit log | `GET /audit` (admins see everyone's, members their own), `GET /stats` (per-server figures) |
| Browser sign-in | `GET /oauth/requests/{id}`, `POST .../approve`, `POST .../deny`, `GET /oauth/grants`, `DELETE /oauth/grants/{id}` |
| Stores | `GET /stores`, `POST /stores`, `POST /stores/{id}/retry`, `DELETE /stores/{id}` (owner or admin) |
| Chat | `POST /chat`, answered as a server-sent event stream |

## Configuration

Environment variables, set by the [chart](../../charts/gateway).

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `ENCRYPTION_KEY` | Secret material the key for upstream credentials is derived from |
| `PUBLIC_URL` | The address people use, for example `https://gateway.example.com` |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | The first admin, created only while there are no users |
| `SEED_SERVERS_FILE` | JSON list of servers to register on first start |
| `EXTRA_ALLOWED_HOSTS` | Host names to answer to besides the public one |
| `PROBE_INTERVAL_SECONDS`, `PROBE_TIMEOUT_SECONDS` | Health checks; defaults 10 and 5 |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | The chat model. Without a key the chat is off. |
| `CHAT_REQUESTS_PER_MINUTE` | Chat messages one person may send per minute; default 20, 0 is no limit |
| `CHAT_TOOL_SEARCH_ABOVE` | With more tools than this the chat uses `find_tools` and `call_tool`; default 40 |
| `UPSTREAM_INTERNAL_HOSTS`, `UPSTREAM_ALLOW_PUBLIC` | Which addresses a server URL may point at; see below |
| `RATE_LIMIT_PER_MINUTE` | Tool calls per API key per minute; 0 is no limit |
| `STORES_ENABLED`, `MAX_STORES_PER_USER`, `STORE_URL_TEMPLATE` | Stores from the dashboard |
| `HOSTED_ENABLED`, `HOSTED_IMAGE_PREFIXES`, `CLUSTER_DOMAIN` | Servers added by container image |

## Beyond the core

Each of these is a single module, written to be read on its own. The design
reasoning is in [docs/PLAN.md](../../docs/PLAN.md), sections 4.8 to 4.13.

| Module | What it does |
|---|---|
| [urlguard.py](src/gateway/urlguard.py) | Decides which addresses the gateway will connect to, and pins each connection to the address it checked (server-side request forgery) |
| [metrics.py](src/gateway/metrics.py) | Prometheus counters and histograms, served at `/metrics` |
| [oauth.py](src/gateway/oauth.py), [api/oauth.py](src/gateway/api/oauth.py) | Signing in from an MCP client with a browser: storage for the SDK's OAuth endpoints, and the consent step that ties a token to a user |
| [toolsearch.py](src/gateway/toolsearch.py) | `find_tools` and `call_tool`: every tool behind two, at `/mcp/search` and in the chat |
| [hosted.py](src/gateway/hosted.py) | A server run from a container image: what an admin may ask for, and how it becomes values for the mcp-server chart. The deploying is the provisioner's. |
| [api/stores.py](src/gateway/api/stores.py) | Records that a store should exist or should go. Touches no Kubernetes API. |
| [provisioner/provisioner.py](src/gateway/provisioner/provisioner.py) | The loop that makes the cluster match the stores table and the hosted servers. Runs as its own Deployment: `python -m gateway.provisioner` |
| [provisioner/kube.py](src/gateway/provisioner/kube.py), [helm.py](src/gateway/provisioner/helm.py) | The handful of Kubernetes API calls it makes, and the helm command |

The rate limit is in `Gateway.call_tool` ([core.py](src/gateway/core.py)) and
one SQL statement in [repo.py](src/gateway/repo.py).

## Development

Needs [uv](https://docs.astral.sh/uv/) and Docker. From this directory:

```bash
uv run pytest
```

The tests start a throwaway Postgres container, run fake upstream MCP servers
in-process, and drive the gateway with a real MCP client, so they cover the
whole path without a cluster. `make gateway-smoke`, from the repository root,
runs the two-user flow against the deployed gateway.

The schema is in [migrations/](migrations). Files are applied in name order,
once each, at startup.
