# Loop Controller Scenario Inventory

Migrated from `dashboard/e2e/loopController.spec.ts` (archived 2026-05-10).
All 6 original Puppeteer scenarios are listed here. Scenarios A, C, D are
converted to MCP-driven JSON files in this PR. Scenarios B, E, F are
follow-up Discussions.

---

## Scenario A — Happy start (converted: `start-loop-button.scenario.json`)

**Input state:**
- Dashboard served at `http://localhost:5173`
- `af_dashboard_token` set in `localStorage`
- Backend running (api.py + server.py)
- `AF_MCP_TEST_ORIGIN=1` set on backend process

**Interactions:**
1. Navigate to `/loop-controller`
2. Fill the prompt `<textarea>` with `E2E test prompt`
3. Click the submit / Start button (`button[type=submit]`)

**Expected outcomes:**
- A new loop ID matching `loop-` appears in the active loops list
- No console errors from the page

---

## Scenario B — Stop (NOT YET CONVERTED — follow-up Discussion)

**Input state:**
- One loop already running (Scenario A precondition)

**Interactions:**
1. Click ALL stop buttons visible on the page

**Expected outcomes:**
- "No active loops" text appears
- No console errors

---

## Scenario C — Live feed (converted: `view-live-feed.scenario.json`)

**Input state:**
- Dashboard running with live feed panel open
- `.autonomous-team/agent-feed.jsonl` writable by the backend

**Interactions:**
1. Navigate to `/loop-controller` (which renders the agent-feed panel)
2. Append a synthetic JSON event line to `.autonomous-team/agent-feed.jsonl`
   via the backend API

**Expected outcomes:**
- The synthetic event text renders in the feed panel within 3 seconds
- No console errors

---

## Scenario D — Snapshot (converted: `view-iteration-history.scenario.json`)

**Input state:**
- Dashboard running with Team Status panel

**Interactions:**
1. Navigate to `/loop-controller`
2. Wait for the Team Status panel to reach a terminal state

**Expected outcomes:**
- The Team Status panel shows either rendered content (discussions/PRs) OR
  a surfaced error message — it must NOT remain in a loading/spinner state
- No console errors

---

## Scenario E — Auth fail (NOT YET CONVERTED — follow-up Discussion)

**Input state:**
- `af_dashboard_token` cleared from `localStorage`

**Interactions:**
1. Reload the page

**Expected outcomes:**
- Token-gate form appears (text "Enter Token" or "Re-enter" visible)
- No page crash

---

## Scenario F — Backend down (NOT YET CONVERTED — follow-up Discussion)

**Input state:**
- Backend killed mid-session (process terminated)

**Interactions:**
1. Attempt any RPC call or wait for fetch to fail

**Expected outcomes:**
- Error/network text appears in the UI within 12 seconds
- No infinite spinner without user-visible feedback
