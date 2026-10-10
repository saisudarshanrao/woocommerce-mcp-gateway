# Demo MCP servers

Two small MCP servers that give the gateway more than the store to aggregate.
They are one image; the container's argument picks the server. Both are
deployed with the generic [mcp-server chart](../../charts/mcp-server).

| Server | Tools | Credential | Why it exists |
|---|---|---|---|
| `tasks` | `add_task`, `list_tasks`, `complete_task`, `server_info` | `X-API-Key` header | A second useful server, so one request can span two: "add a packing task for each of today's orders". Shows a credential sent in a custom header. |
| `sandbox` | `echo`, `slow`, `fail`, `server_info` | none | Misbehaves on request: `slow` outlasts the gateway's timeout and `fail` returns an error. |

Both have a tool called `server_info`. That is deliberate: it is the name
collision the gateway's `<server>__<tool>` scheme resolves.

The task list is kept in memory, so restarting the pod empties it.

## Development

```bash
uv run pytest
```
