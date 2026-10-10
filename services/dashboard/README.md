# Dashboard

The gateway's web interface: a React single-page app, built with Vite and
served as static files by nginx.

| Page | Who | What |
|---|---|---|
| Servers | everyone | Each server's status (Connected, Unreachable with the reason, Disabled) and tools. Admins add, edit, disable and remove servers. |
| Chat | everyone | Ask questions answered with the gateway's tools; each tool call is shown as it happens. |
| API keys | everyone | Create and revoke your own keys, with ready-to-paste settings for Claude Code, Cursor and MCP Inspector. |
| Team | admins | Invite people, change roles, deactivate or remove members, issue and revoke anyone's keys. |
| Audit log | everyone | Every tool call. Admins see the whole team's, members their own. |

It holds no data and no secrets. Everything it shows comes from the gateway's
REST API on the same hostname, so the browser needs no cross-origin permission
and the session cookie, which page script cannot read, is sent automatically.
[src/api.ts](src/api.ts) is the one file that talks to the gateway.

## Development

```bash
npm install
```

```bash
npm run dev
```

`npm run dev` serves the app on its own port and forwards `/api` to
`http://gateway.localhost`, so the gateway has to be running in the local
cluster. Set `GATEWAY_URL` to point it elsewhere.

```bash
npm run build
```

`npm run build` type-checks and writes the production files to `dist/`.
