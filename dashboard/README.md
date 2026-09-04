# Autonomous Team Dashboard

A browser-based UI for monitoring and interacting with the autonomous team.

## Quick start

```bash
python dashboard/server.py
```

Open http://localhost:8420 — or, to also get the React frontend running, use `bash scripts/start-dashboard.sh` from the repo root instead (starts all four services and prints the URL, `http://localhost:5173` by default; see the main README's Dashboard section).

## What it does

- Spawns `backend/server.py` automatically as a subprocess
- Streams every agent event to all open browser tabs via SSE
- Lets you send prompts to the Team Lead from the browser
- Has a "Run Loop" button to fire an immediate loop iteration
- Status bar shows model name, uptime, connected tabs, last event time

## Configuration

| Env var            | Default                  | Description            |
|--------------------|--------------------------|------------------------|
| `AF_DASHBOARD_PORT`| `8420`                   | HTTP port              |
| `AF_API_KEY`       | —                        | API key (required)     |
| `AF_PROVIDER`      | `openai`                 | Provider name          |
| `AF_MODEL`         | `kimi-k2-0711-preview`   | Model ID               |
| `AF_BASE_URL`      | `https://api.moonshot.cn/v1` | API base URL       |

Or pass `--port` on the command line:

```bash
python dashboard/server.py --port 9000
```

## Run E2E Tests

The KPI page (`/kpi`) has a Puppeteer test suite that runs against a fixture-backed backend.

```bash
cd dashboard
npm install
npm run e2e
```

This boots a Vite dev server and `backend/server.py` in fixture mode, runs four Puppeteer scenarios against the `/kpi` route, and exits 0 when all pass. Screenshots land in `e2e/__artifacts__/` (gitignored).

See [wiki/Team-KPIs-Dashboard.md](../wiki/Team-KPIs-Dashboard.md) for the full JSON-RPC method reference, fixture schema, and scenario list.

## Adding API calls

All network requests in `dashboard/src/` must go through the wrappers in
`src/api/client.ts` — either the named API objects (`projectsApi`, `kpiApi`,
`healthApi`, etc.) or the lower-level `client.get/post/put/patch` and
`jsonRpc()` helpers.

**Do not use raw `fetch()` directly.** The client wrappers include 401-retry
logic and config-cache management added in PR #1064. Bypassing them reintroduces
the config-race bug they were designed to fix.

ESLint enforces this: `no-restricted-syntax` flags any `fetch()` call in
`src/**` and the CI check will fail. The only allowlisted files are
`src/api/client.ts` and `src/lib/jsonrpcClient.ts` (the transport layer itself).

To add a new endpoint, add a method to the appropriate api object in
`src/api/client.ts`. If you need a genuinely different transport (e.g. streaming),
add an `// eslint-disable-next-line no-restricted-syntax` with a brief comment
explaining why.

## Dependencies

- Python 3.11+
- `aiohttp` — `pip install aiohttp`

Everything else is stdlib.
