# ts-backend Parity Caveats

Known, intentional, or unavoidable divergences from the Python backend.
Future reviewers: do not re-chase these — they are documented here on purpose.

---

## 1. Auth: bare `Authorization: Bearer ` header (empty token)

**Status:** Unavoidable framework-level divergence. Both implementations DENY.

Python distinguishes two cases for a `Bearer` header:
- `Authorization: Bearer <token>` (non-empty) → 403 Forbidden (wrong token)
- `Authorization: Bearer ` (empty / whitespace-only after the prefix) → treated as "missing" → 401 Unauthorized

Hono strips trailing whitespace from header values, so `"Bearer "` becomes `"Bearer"`.
The `authHeader.startsWith("Bearer ")` check therefore fails, and the request falls through to the
"missing" branch → 401, matching Python's *second* case. Both resolve to a DENY; only the status
code (401 vs 403) differs for this edge case.

Changing this would require intercepting raw header bytes before Hono normalization — not practical.

---

## 2. Stats timestamp fields: sub-second precision

**Status:** Intentional canonical form. Spec-decided.

TS handlers normalize all generated timestamps (e.g. `generated_at`, `checked_at`) to
second-granularity ISO-8601 UTC: `2026-05-23T14:00:00Z`.

Python's `datetime.now(UTC).isoformat()` preserves microseconds: `2026-05-23T14:00:00.123456+00:00`.

The TS form is the agreed canonical output. Dashboard consumers parse ISO-8601 and do not rely
on sub-second precision; normalizing is strictly cleaner.

---

## 3. /events event-type coverage

**Status:** Known limitation. Deferred to P5b (externalize the event bus).

TS `/events` sources events from the persisted `agent-feed.jsonl` file. In practice the feed
contains primarily `AgentOutputEvent` entries written by the post-agent hook.

Python's `/events` subscribes to the in-process event bus, which emits all four event types:
`AgentOutputEvent`, `BudgetSpendEvent`, `GateChangeEvent`, `LoopIterationEvent`.

Full parity requires externalizing the Python event bus (write all event types to the JSONL feed
or a separate bus file). That is the scope of D#1437 P5b, not this PR.

The `inferEventType()` discriminator in `routes/sse.ts` is ready for all four types once the feed
contains them.

---

## 4. budget-init: non-positive ceiling — TS 400 vs Python 200

**Status:** RESOLVED — D#1437 faithful-mirror fix (2026-05-23).

The TS-only 400 guard for non-positive ceiling has been removed.
`ts-backend/src/routes/budget-init.ts` now mirrors Python exactly:
any numeric ceiling value (positive, zero, or negative) is accepted and written,
returning HTTP 200.

Python's `budget_init` handler does `ceiling = body.get("ceiling")` then passes it
straight to `BudgetTracker.init_session()` with no validation. The TS port now does
the same. Parity is proven in `tests/budget-init.test.ts` §3 (negative + zero ceiling
parity harness tests run against both Python and TS on temp blackboard dirs).

---

## 5. stats.dora: gh-dependent fields in degraded mode

**Status:** Known, documented degraded-mode values. Not a parity break.

`stats.dora.lead_time_minutes_p50` and `stats.dora.change_failure_rate_pct` both
depend on the `gh` CLI being authenticated in the environment (same constraint as
`stats.weekly_velocity` which shells `gh pr list`).

When `gh` is unavailable or returns a non-zero exit code:
- `lead_time_minutes_p50` returns `-1.0` (the Python `release_manager.compute_dora_snapshot()`
  fallback when no PRs are reachable)
- `change_failure_rate_pct` returns `"n/a"` (the Python `analytics_engineer._compute_cfr()`
  fallback on any `gh` error or timeout)

These are the documented Python fallback values, not a divergence. In an environment where
`gh` is authenticated, the TS and Python handlers produce field-for-field identical output.

---

## 6. Discussion status: TS has neither the anchored read nor `BLOCKED-BY:`

**Status:** Known divergence, TS side is behind. Python is authoritative.

`ts-backend` is additive and does not spawn work — the live selector and spawn gate are the
shell + Python path. Two status-parsing fixes have landed on the Python side and are not
ported:

- **D#1798 (anchored read).** Python's `extract_status_anchored()` / `is_spec_ready()` read
  the STATUS marker only from the first non-empty line. TS still substring-tests the whole
  body: `/STATUS:\s*SPEC_READY/` in `src/spawn/spawn-agent.ts:185,685` and
  `body.includes("STATUS:SPEC_READY")` in `src/loop/loop-phased-step5.ts:807,850`. A body
  quoting the marker in prose or a code fence reads as SPEC_READY to TS.
- **D#1755 (`BLOCKED-BY:`).** TS has no equivalent of `extract_blocked_by()` or
  `backend/blocked_by.py`, so a Discussion whose Spec is finished-but-blocked reads as
  plainly spawnable there.

Both make the TS readers **fail open** relative to Python. That is contained only because TS
does not gate any real spawn today. Porting `discussion-status.ts` up to the Python parser —
anchored read plus `BLOCKED-BY:` — is a prerequisite for the TS loop ever becoming
authoritative, and must land before parity is signed off on the spawn path.

---

## 7. spawn-agent.ts: no PR-tree provisioning, no unprovisioned-worktree reason

**Status:** Known divergence, TS side is behind. Deferred, not fixed here (D#2222).

`scripts/spawn-agent.sh` (bash, live) provisions a PR-amend worktree via `scripts/lib/pr-tree.sh`
when `--pr` is set, and — as of D#2222 — tags a spawn with `worktree_unprovisioned` /
`worktree_unprovisioned_reason` so `backend/prompt_builder.py` can render one of three distinct
messages: the honest "the Agent tool provisions this" note for a canonical fresh spawn, a hard-fail
for a real `pr_tree_provision` failure, and a hard-fail for a `--pr` head-sha resolution failure
(this last one matters specifically because proceeding in the wrong tree during a PR amend is
silent data corruption, not a loud error).

`ts-backend/src/spawn/spawn-agent.ts` has none of this ported: `assemblePrompt()` sets
`worktree_path` from `args.worktreePath` alone (`--pr` never provisions a tree) and never sets
`worktree_unprovisioned`, so a worktree-isolated TS-lane spawn with no `--worktree-path` renders
no worktree block at all — silently, which is a third failure mode neither the old nor the new
bash behavior has.

Not fixed here because this lane is not live (no real spawn is dispatched through it today —
see caveat 6). If/when it goes live, both `pr-tree.sh` provisioning and the three-way reason
distinction need to be ported alongside it; parity should not be assumed just because the
payload shape (`worktree_path`) matches.
