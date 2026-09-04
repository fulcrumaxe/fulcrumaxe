---
name: concurrency-caps
description: "Team Lead concurrency caps — max 6-8 total agents, max 4 executors at any time"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c6897484-23b6-474b-9ffb-37f6ac7089d6
tier: transferable
---

**Hard concurrency caps for Team Lead spawning:**

- **Max 4 executors** in flight at any time (executors hold worktrees, run preflight, hit state.db hardest)
- **Max 4 other agents** (reviewers, PMs, panel specialists, debaters, etc.) in flight at any time
- **= 8 total** ceiling

User clarified the cap on 2026-05-14: "no more than 4 executors and 4 other agents at any given time."

**Why:**
- Set 2026-05-14 after an aggressive parallel batch (7 executors + 4 impl-coords + 10 panel specialists ≈ 21 concurrent agents) caused systemic stall:
  - 11 processes deadlocked on `state.db` SQLite locks
  - 29 hung `preflight.sh` instances accumulated
  - GraphQL rate limit (5000/hr) exhausted
  - Multiple zombie shells in `team_status` / `stuck-pr-detect` recovery loops
- Updates [[feedback_higher_agent_concurrency]] which said "4-6+ parallel subagents" — that was the floor for "we're not parallelizing enough"; THIS rule is the ceiling for "we've overwhelmed the box."

**How to apply:**
- Before spawning, count active executors + active others. If at cap, queue the spawn — don't fire.
- Panel specialists are cheap (Haiku, ≤30K tokens, ≤60s each) but still count toward total — fire them in waves of 5, not 5×N panels in parallel.
- Code-reviewers + security-reviewers also count — when 4 executors are running, hold reviews until at least 2 executors finish.
- The cap applies even when subscription quota has headroom. The bottleneck is local concurrency (state.db, GH API, preflight), not Anthropic spend.

**Exception:** A single hot-path hotfix (e.g. spawn-gate broken) overrides the cap — that's an emergency, the rest of the pipeline doesn't matter until it's fixed.
