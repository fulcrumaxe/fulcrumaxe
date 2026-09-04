# Spawn/Loop Orchestration → TypeScript Port Program

**Goal (ian, 2026-06-01):** faithfully port the Python spawn + loop orchestration to
TypeScript (1:1 parity, additive — Python untouched), so the full fulcrumaxe
loop can then run on opencode/Qwen as the agent runtime, and later integrate cleanly into
the sandboxed.sh bridge.

**Order chosen:** TS port FIRST → then loop-on-opencode → then sandboxed.sh bridge. This is
a multi-week program; each module is a faithful, parity-tested port, not a reimplementation.

**Conventions (match existing ts-backend):**
- 1:1 parity; header docstring names the exact Python source + handlers mirrored.
- `bun:sqlite` for `state.db`, `duckdb-helpers.ts` for `stats.duckdb`.
- Env-var path resolution matching Python (`AF_REPO_ROOT`, state-dir env vars).
- Parity verified via the golden/parity harness (`golden-capture`/`golden-assert`/`parity-sweep`).
- Additive: Python runtime is never modified; TS is shadow-validated against it.

## Module sequence (dependency order)

| # | Python source | LOC | TS target | Notes |
|---|---|---|---|---|
| 1 | `agent_run_tracker.py` (+ `agent_run.py` reader) | 891+437 | `src/spawn/agent-run-tracker.ts` | FOUNDATION: state.db agent_run writer (start/complete/backfill/reconcile). Parity-testable via DB state diff. |
| 2 | `control_plane.py` | 868 | `src/spawn/control-plane.ts` | policies/caps/gates (config-backed) |
| 3 | `cost_tracker.py`, `claude_spawn_tracker.py`, `loop_runs.py` | ~1830 | `src/spawn/cost-tracker.ts` etc. | metrics writers |
| 4 | `discussion_cache.py`, `discussion_status.py` | ~590 | `src/spawn/discussion-*.ts` | spec readiness |
| 5 | `pre-spawn-check.sh` | 819 | `src/spawn/pre-spawn-check.ts` | caps + spec + touchpoint conflict |
| 6 | `spawn-agent.sh` | 856 | `src/spawn/spawn-agent.ts` | pre-flight + prompt assembly + registration + the runtime invocation SEAM |
| 7 | `post-agent-hook.sh` | 560 | `src/spawn/post-agent-hook.ts` | stats, team-log, bookkeeping |
| 8 | `post-merge-hook.sh` | 1167 | `src/spawn/post-merge-hook.ts` | |
| 9 | `loop-phased-step5.sh` + loop steps | ~1141+ | `src/loop/*.ts` | the loop orchestration |
| 10 | `backend/orchestrator/*` (sdk_runner, dispatch, auto_route, hook_runner) | ~5200 | `src/orchestrator/*.ts` | agent INVOCATION — **the opencode/Qwen adapter slots in here** |

After #10: full loop runs in TS with opencode as the runtime → then the sandboxed.sh bridge
(AF_TEAM mode already drafted in `src/bridge/`) consumes it.

## Parity strategy per module
1. Port faithfully, header docstring naming the Python source.
2. Golden test: run Python + TS on identical inputs (fixtures / a scratch state.db), assert
   identical DB state / stdout / exit code.
3. Gate: no merge until parity green. Python stays the source of truth until the whole
   chain is parity-clean.

## Status
- Module 1 (agent-run-tracker): IN PROGRESS.
- The earlier `src/bridge/team-loop.ts` (opencode role cycle) was a SHAPE prototype — it does
  NOT integrate the backend; the real integration is this port. Keep it as reference only.
