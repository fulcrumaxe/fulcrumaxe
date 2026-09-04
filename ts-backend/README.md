# ts-backend — TypeScript backend (Bun + Hono)

A Bun + Hono TypeScript backend that mirrors the Python backend with 1:1 parity.
Both backends run simultaneously — the TS backend is additive and never replaces
the Python reference until parity is signed off on all routes.

See Discussion #1437 for the full phased plan.

## Quick start

```bash
cd ts-backend
bun install
bun start           # boots on http://127.0.0.1:19099
```

One-command boot:

```bash
cd ts-backend && bun install && bun start
```

## Port

`127.0.0.1:19099` — loopback-only until full parity is proven.

Override: `TS_BACKEND_PORT=19100 bun start`

## API contract

The full machine-readable contract lives at `ts-backend/openapi.json` (OpenAPI 3.1).

Regenerate after any route changes:

```bash
bun run openapi:gen
```

Fetch from the live server (no auth required):

```bash
curl http://127.0.0.1:19099/openapi.json | jq .info
```

Or open the committed file directly — it's always in sync with the generator.

## Routes (P0)

| Method | Path | Auth | Status |
|--------|------|------|--------|
| GET | /health | Public | P0 — at parity |

## Verify parity

One command to sweep all converted routes and get a parity verdict:

```bash
bun run parity
```

This boots the TS backend, probes every safe read-only GET route against the
golden fixture corpus, emits a per-route PASS/FAIL summary, and writes a JSON
report to `ts-backend/parity-report.json`. Exits non-zero if any route diverges.
No Python backend needed — the golden corpus is checked into `fixtures/`.

Live mode (requires Python backend on :18099):

```bash
bash scripts/start-dashboard.sh   # start Python backend first
bun run parity:live               # fans each request to both backends and diffs
```

Custom report path:

```bash
bun run parity -- --report /tmp/my-report.json
```

The JSON report is machine-readable and intended for nightly CI or a future
dashboard tile:

```jsonc
{
  "generated_at": "2026-05-23T18:00:00Z",
  "mode": "golden",
  "total": 12,
  "at_parity": 12,
  "diverged": 0,
  "results": [{ "route": "/health", "diverged": false, ... }, ...]
}
```

## Running the parity harness

```bash
# Unit tests (normalizer rules + parity-sweep logic)
bun test tests/normalizer.test.ts
bun test tests/parity-sweep.test.ts

# Capture golden fixture from live Python backend (must be running on :18099)
bun run golden-capture

# Assert TS /health matches golden fixture
bun run golden-assert

# Live shadow diff: both backends, /health, zero divergence
bun run shadow-diff

# Full sweep across all converted routes (golden corpus)
bun run parity
```

## Phases

- **P0** (this PR): scaffold + normalizer + /health parity
- **P1**: auth/RBAC middleware (requires security-reviewer)
- **P2**: SQLite-backed read-only GETs (/feed, /events, /sessions, /spawn-queue)
- **P3**: DuckDB stats routes (gated on SPIKE-1)
- **P4+**: mutations, SSE/WS, RPC/GraphQL
