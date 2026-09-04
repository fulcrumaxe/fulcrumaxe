# Dashboard Scenario-Driven Tests

MCP-driven Chrome DevTools scenarios replacing the old Puppeteer `dashboard/e2e/` specs.
Each scenario is a structured JSON file that describes a user journey; the `browser-tester`
agent (see `.claude/agents/browser-tester.md`) executes it by driving `mcp__chrome-devtools__*`
tool calls and returning a `BrowserTourReport`.

## Directory layout

```
dashboard/scenarios/
  README.md                          ← this file
  <area>/
    INVENTORY.md                     ← all original scenarios A–F listed with inputs + expectations
    <name>.scenario.json             ← one scenario per file
    __tests__/
      scenario_validate.test.ts      ← schema validation (vitest)
```

Current areas:
- `loop-controller/` — Loop Controller page scenarios (Discussion #475)

Future areas (one Discussion each):
- `discussion-explorer/`
- `kpi-detail/`
- `pr-inspector/`

## Scenario file format

Each `*.scenario.json` has this shape:

```json
{
  "name": "start-loop-button",
  "goal": "verify clicking Start triggers the loop.start RPC and the loop appears in the active list",
  "url": "http://localhost:5173/loop-controller",
  "preconditions": {
    "localStorage": { "af_dashboard_token": "<test-token>" },
    "env": { "AF_MCP_TEST_ORIGIN": "1" }
  },
  "steps": [
    { "action": "navigate_page", "url": "http://localhost:5173/loop-controller" },
    { "action": "fill", "selector": "textarea", "value": "E2E test prompt" },
    { "action": "click", "selector": "button[type=submit]" }
  ],
  "success_criteria": [
    { "kind": "text_contains", "value": "loop-" },
    { "kind": "no_console_errors" }
  ],
  "must_not_happen": [
    { "kind": "network_request", "url_matches": "/api/innovate/tick" }
  ]
}
```

### Required keys

| Key | Type | Description |
|-----|------|-------------|
| `name` | string | Kebab-case name matching the filename (without `.scenario.json`) |
| `goal` | string | One-sentence description of what is being verified |
| `url` | string | Starting URL for the scenario |
| `steps` | array | Ordered MCP tool calls; each object must have an `action` key |
| `success_criteria` | array | Conditions that must all be true at the end; each must have a `kind` key |

### Optional keys

| Key | Type | Description |
|-----|------|-------------|
| `preconditions` | object | `localStorage` map and/or `env` map set before navigation |
| `must_not_happen` | array | Conditions that must NOT occur (e.g. unwanted network requests) |
| `covers_scenario` | string | Reference to the original Puppeteer scenario letter (A–F) |

### Step actions

Actions map to `mcp__chrome-devtools__*` tool calls:

| Action | Description |
|--------|-------------|
| `navigate_page` | Navigate to `url`; optional `wait_until` (domcontentloaded / networkidle) |
| `fill` | Fill `selector` with `value` |
| `click` | Click element matching `selector` |
| `wait_for` | Wait for `selector` to appear, or `text_contains` to match, or `any_of` list |
| `evaluate_script` | Run `script` in the page context |
| `take_snapshot` | Capture DOM snapshot (for debugging / diffing) |
| `list_console_messages` | Collect console output (used by `no_console_errors` criterion) |
| `list_network_requests` | Collect network calls (used by `network_request_made` / `must_not_happen`) |

### Success criteria kinds

| Kind | Description |
|------|-------------|
| `text_contains` | Page must contain `value` |
| `any_text_present` | At least one of `values` array must be present |
| `no_console_errors` | No uncaught JavaScript errors in console |
| `network_request_made` | A network request matching `url_matches` (and optionally `body_contains`) occurred |
| `infinite_spinner` (must_not_happen) | Loading indicator must resolve within `timeout_ms` |

## How to add a new scenario

1. Create `dashboard/scenarios/<area>/<name>.scenario.json` (kebab-case name, `.scenario.json` suffix).
2. Include all required keys: `name`, `goal`, `url`, `steps`, `success_criteria`.
3. Set `"name"` to match the filename (without `.scenario.json`).
4. Run dry-run validation to confirm the file is correct:
   ```bash
   bash scripts/run-scenarios.sh <area> --scenario <name> --dry-run
   ```
5. The TypeScript schema test (`__tests__/scenario_validate.test.ts`) will automatically
   pick up the new file and validate it on the next test run.

## Running scenarios

### Dry-run (always safe — no agents spawned)

```bash
# Validate and print spawn plan for all scenarios in an area
bash scripts/run-scenarios.sh loop-controller --dry-run

# Validate one scenario only
bash scripts/run-scenarios.sh loop-controller --scenario load-page --dry-run
```

Dry-run output ends with `PRESUM: pass` on success. Exit code is 0 on success, 1 on
validation failures.

### Live run (pending Discussion #467)

Live spawning requires the MCP browser-tester contract from Discussion #467 to be finalized.
Until then, the runner exits with a helpful message when invoked without `--dry-run`.

## AF_MCP_TEST_ORIGIN env-var contract

When running live scenarios, Chrome's UA matches `HeadlessChrome` and the Origin is
`http://localhost:5173`. The backend's `_reject_test_origin_spawn` guard in `backend/api.py`
would block any spawn request from such a UA/Origin pair by default (HTTP 403).

To allow MCP-driven scenarios through cleanly, set `AF_MCP_TEST_ORIGIN=1` on the backend
process before starting `backend/api.py`:

```bash
AF_MCP_TEST_ORIGIN=1 python3 backend/api.py
```

**Important security notes:**
- This env var bypasses ONLY the UA/Origin heuristic — the full authentication gate
  (`af_dashboard_token` cookie/header check) remains active. A request still needs a
  valid token to reach any spawn endpoint.
- Set per-process only. Never persist in `.env` files, `config.json`, or any config
  that auto-loads on server start outside of test runs.
- Default behaviour (env var unset): HeadlessChrome UA or localhost:5173 Origin → 403 rejected.

The existing `AF_ALLOW_TEST_ORIGIN_SPAWNS=1` var continues to work for local human-driven
dev with Puppeteer request-interception installed (legacy bypass, same semantics).

## Route manifest & coverage gate

`App.tsx` is the single source of truth for the dashboard's route list. `routes.manifest.json`
is a generated, committed snapshot of that route table, and a vitest coverage gate proves
every manifest route resolves to at least one scenario file — mechanically, without relying on
anyone remembering to update a checklist by hand (Discussion #1527).

### Generating the manifest

```bash
node dashboard/scenarios/scripts/generate-route-manifest.mjs
```

Parses the `<Route path="..." element={...} />` table in `dashboard/src/App.tsx` and writes
`dashboard/scenarios/routes.manifest.json`. Each entry has this shape:

```json
{
  "route": "/stats",
  "component": "StatsPage",
  "parameterized": false,
  "expected_done_state": "unspecified",
  "needs_seed_fixture": false
}
```

| Field | Description |
|-------|--------------|
| `route` | The route path exactly as declared in `App.tsx` (e.g. `/project/:id`) |
| `component` | The page component rendered for this route (Suspense/lazy wrappers are unwrapped) |
| `parameterized` | `true` when the path contains a `:param` segment |
| `expected_done_state` | `"data" \| "empty" \| "error" \| "unspecified"` — the route's expected rendered state once certified. Hand-set by a human (or the follow-on bug-hunt Discussion); the generator never overwrites an existing value, only fills `"unspecified"` for brand-new routes. |
| `needs_seed_fixture` | `true` when the route needs seeded backend data to render its non-empty state (e.g. parameterized routes like `/project/:id`). Same preserve-on-regenerate rule as `expected_done_state`. |

The catch-all redirect (`path="*"`) is excluded — it isn't a real page and has no
coverage requirement.

**Regeneration never clobbers hand-curated metadata.** If a route already exists in the
committed manifest, its `expected_done_state` and `needs_seed_fixture` values are carried
forward untouched; only the route list itself (additions/removals from `App.tsx`) changes.
This is what makes the `--check` drift guard meaningful without erasing human intent.

### Drift guard (`--check`)

```bash
node dashboard/scenarios/scripts/generate-route-manifest.mjs --check
```

Exits `0` when the committed manifest's route/component/parameterized shape matches a fresh
scan of `App.tsx`. Exits non-zero with a diff summary (added / removed / changed routes) when
someone adds, removes, or renames a `<Route>` without regenerating the manifest. Intended as a
CI check on `dashboard/**` PRs — wiring it into an actual CI job is deferred to the follow-on
Discussion (this Spec is the gate mechanism only, not the CI wiring).

### Coverage-gate test

```bash
cd dashboard && npx vitest run scenarios/__tests__/route-coverage.test.ts
```

Globs every `dashboard/scenarios/**/*.scenario.json`, builds the set of routes each one
declares coverage for (via `visual_verification.routes_touched` or a top-level `route` key),
and checks that against every route in the manifest. The test prints
`covered N / M routes` plus the list of uncovered routes — that list is the backlog for the
follow-on "certify everything" Discussion, which owns authoring the missing scenarios.

This Spec intentionally does NOT hard-fail the whole suite on every uncovered route (most
routes have none yet — that's the known, tracked gap, not a regression). Instead it hard-fails
only if a route that currently HAS declared coverage loses it — a real regression.

### Scoped sweep input (`changed-routes.mjs`)

```bash
node dashboard/scenarios/scripts/changed-routes.mjs <base_ref>
```

Given a git base ref, diffs `dashboard/src/pages/**` against `HEAD` and prints the JSON list
of route paths whose backing component changed (via the manifest's `component` field). Pure
stdout, no side effects, does not invoke Chrome — it's the input a future scoped post-merge
sweep job would consume.

### Sweep cadence policy (documented, not yet wired)

Per the Discussion #1527 panel (performance-expert): a full Chrome MCP sweep across all routes
takes ~12-20 minutes single-threaded, so it must never run synchronously per-PR. The intended
cadence, to be wired up in the follow-on Discussion:

1. **Nightly full sweep** — every route, reusing the `visual-verifier` cadence.
2. **Scoped post-merge sweep** — only routes touched by the merged PR, using
   `changed-routes.mjs` as the input, path-filtered to `dashboard/**`.
3. **Never a synchronous per-PR gate** — 20 PRs/day of full sweeps would burn 4-6 browser-hours
   for zero marginal signal per performance-expert's sizing.
