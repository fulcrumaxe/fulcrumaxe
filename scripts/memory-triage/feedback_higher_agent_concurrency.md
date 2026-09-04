---
name: Run high agent concurrency when user is monitoring
description: User wants 4-6+ concurrent subagents at a time, not the conservative 1-2 default
type: feedback
originSessionId: a267c7bf-7678-4f93-a4d3-5490a697ebbc
tier: transferable
---
When the user is actively monitoring a session (signaled by "I'm watching", "let's go faster", "increase productivity", short wake intervals like 5min), default to spawning **all SPEC_READY work in parallel** and **all DISCUSSING work in parallel** as PMs — not one at a time.

Rough budget: 4-6 concurrent subagents is fine; the only hard constraints are:
- token budget (5M session ceiling — currently using <1% per merge)
- file conflict (use `isolation: "worktree"` whenever two agents will touch the same file, especially CLAUDE.md or shared scripts)
- pre-spawn-check passing for each

**Why:** Sequential or 1-2-at-a-time is way slower than the budget allows. User said 2026-05-09: "we can increase productivity level we are running to slow we can increase agent count".

**How to apply:**
- At step 5 of /loop, sweep ALL SPEC_READY discussions and spawn impl-coord for each in parallel (worktree-isolated).
- Sweep ALL DISCUSSING refocus-themed discussions and spawn PMs in parallel.
- Don't wait for one PR to merge before spawning the next impl-coord — spawn them all at once.
- Don't artificially cap to "one per iteration" — that wastes time the user is actively spending watching.
- Conflict resolution happens at PR merge time via rebase, not by serializing spawns.
