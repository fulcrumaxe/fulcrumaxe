# ts-backend Parity Caveats

Known, intentional, or unavoidable divergences from the Python backend.
Future reviewers: do not re-chase these — they are documented here on purpose.

Every entry now carries a **Reachability** line (D#2540 item 10): whether a
dashboard user can actually observe the divergence through the live backend
toggle (`dashboard/src/components/BackendTargetIndicator.tsx`), or whether it
is internal/orchestration-only. A cosmetic difference and one that answers
about the wrong project read identically as "a line in this file" without
that distinction — which is exactly how the stats.dora project-scoping gap
(the former caveat #8, closed by D#2540) sat here for a cycle. All seven
entries below were also re-verified against current code-plane `main` for
this pass (item 11); each is still accurate except #7, corrected below.

---

## 1. Auth: bare `Authorization: Bearer ` header (empty token)

**Status:** Unavoidable framework-level divergence. Both implementations DENY.

**Reachability:** Every RPC/REST request goes through this auth path, but
this specific edge case — an empty `Bearer` value — requires a malformed
client. The dashboard's own client always sends a well-formed token; nothing
in the dashboard UI (backend toggle included) can trigger this branch.

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

**Reachability:** Reachable. `generated_at`/`checked_at` render directly on
dashboard stat tiles, on both backends the toggle can select.

TS handlers normalize all generated timestamps (e.g. `generated_at`, `checked_at`) to
second-granularity ISO-8601 UTC: `2026-05-23T14:00:00Z`.

Python's `datetime.now(UTC).isoformat()` preserves microseconds: `2026-05-23T14:00:00.123456+00:00`.

The TS form is the agreed canonical output. Dashboard consumers parse ISO-8601 and do not rely
on sub-second precision; normalizing is strictly cleaner.

---

## 3. /events event-type coverage

**Status:** Known limitation. Deferred to P5b (externalize the event bus).

**Reachability:** Reachable. `dashboard/src/hooks/useWebSocket.ts` consumes
`/events`; a dashboard user on the TS backend sees fewer event types than on
Python for the same activity.

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

**Reachability:** Internal/orchestration-only. No `dashboard/src` file calls
`budget-init`/`session_ceiling`; it is reached by scripts and orchestration
code, not by anything the backend toggle changes what a dashboard user sees.
Moot either way now that it is resolved.

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

**Reachability:** Reachable — this is the same handler the former caveat #8
(stats.dora project-scoping, closed by D#2540) was about. Two caveats on one
user-reachable handler was itself worth stating explicitly, which is why
this note exists: this caveat is about `gh` CLI availability/auth in the
serving environment, orthogonal to project scoping (now correctly resolved
per request). Both can be true of the same response at once — a
project-scoped call can still degrade to `-1.0`/`"n/a"` if `gh` itself is
unavailable, exactly as an unscoped call already could.

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

**Reachability:** Internal/orchestration-only. This is spawn/loop-gating
logic, not a dashboard-rendered value — the backend toggle does not change
what a dashboard user sees here because nothing here reaches the dashboard.

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

**Reachability:** Internal/orchestration-only. Spawn plumbing, no dashboard
surface — the backend toggle is irrelevant here.

**Corrected 2026-09-12 (D#2540 item 11) — the bash side moved, this caveat had not.**
The previous text of this caveat said `scripts/spawn-agent.sh` provisions a PR-amend
worktree via `scripts/lib/pr-tree.sh` main-flow whenever `--pr` is set. That stopped being
true when D#2542 landed (PR #177, merged 2026-09-11T12:00:55Z): measured on current
code-plane `main`, `pr_tree_provision` now appears exactly **once** in
`scripts/spawn-agent.sh`, at `:242`, inside the `--dry-run-env-dump` block only — never on
the main spawn path. A `--pr` worktree spawn is no longer auto-provisioned a tree to
describe; instead it renders a "resolve your own root" prompt block tagged with one of
three reasons — `pr_amend` (`:1008`, `:1012`), `pr_resolution_failed` (the `--pr` head-sha
lookup itself failed), or `agent_tool_provisions` (`:1034`, the canonical
`--isolation worktree` fresh-spawn case with no `--pr` at all) — so
`backend/prompt_builder.py` can explain how to reach the PR's head content from
whatever tree the Agent tool's own `isolation="worktree"` actually provisions. Bash's
reasoning: nothing ever ran in the auto-provisioned tree (the CC lane never `cd`s there),
so provisioning one purely to describe it was waste. `scripts/lib/pr-tree.sh` itself is
unchanged — it still backs `scripts/lib/pr-dependents.sh` and
`backend/spawn_templates/docs-writer.tmpl`.

The substance of this caveat is unchanged even though its bash-side description was
stale: `ts-backend/src/spawn/spawn-agent.ts`'s `assemblePrompt()` still sets
`worktree_path` from `args.worktreePath` alone and never sets `worktree_unprovisioned` /
`worktree_unprovisioned_reason`, so a worktree-isolated TS-lane spawn with no
`--worktree-path` still renders no worktree block at all — silently. That gap is real on
both the old and the corrected bash behavior; only the bash side's own shape (what it
does instead of provisioning) had drifted out from under this file's description.

Not fixed here because this lane is not live (no real spawn is dispatched through it today —
see caveat 6). If/when it goes live, both the current bash reason-classification (`pr_amend` /
`pr_resolution_failed` / `agent_tool_provisions`) and the self-resolve prompt block need to be
ported alongside it; parity should not be assumed just because the payload shape
(`worktree_path`) matches, and this description needs re-checking against the bash side again
before that happens — this is the second time it has drifted.
