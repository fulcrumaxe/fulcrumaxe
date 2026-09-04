---
name: /loop is the canonical 8-step CLAUDE.md flow, not "do work and ScheduleWakeup"
description: Each /loop iteration must run all 8 steps from CLAUDE.md, including pre-flight, Discussion scan, PR scan, post-agent-hook chain, subsystem sweep, now.md update
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
In session 85514482 the user invoked `/loop` with a 6-feature directive. I treated each iteration as "do one chunk of feature work and ScheduleWakeup," which produced shippable code but skipped almost the entire CLAUDE.md /loop protocol. The user called this out twice and asked me to teach future-me the right flow.

**Why:** CLAUDE.md spells out the 8-step iteration explicitly. Skipping steps means: no budget tracking, no audit entries, no training-data flywheel firing, no Discussion lifecycle, no review labels, no auto-merge gates, no wiki sync. The work ships but the team's observability and quality machinery stays inert. When the cron loop is dead AND I'm running a half-loop, nothing in `audit.jsonl` or `loop-metrics.jsonl` gets updated, so `/health/loop` goes red and the dashboard reports a broken team.

**How to apply:** Every wake-up of a Team Lead /loop iteration runs ALL of these steps in order:

```
0.   Parse [Loop pre-flight: ...] header if present (otherwise skip)
0.5. Init metrics counters (T_START, ACTIONS, AGENTS_SPAWNED, PRS_MERGED)
0.6. python3 backend/budget.py status > /dev/null || python3 backend/budget.py init
1.   gh repo view --json nameWithOwner
2.   Ensure team-log issue exists (label: team-log)
3.   Scan GitHub Discussions via GraphQL — route new ones to project-manager
4.   gh pr list --state open --json number,title,labels — find PRs needing review or merge
5.   ACT ON WORK:
       For each PR missing review labels:
         bash scripts/pre-spawn-check.sh --role code-reviewer --discussion <N>
         Agent(subagent_type="code-reviewer", prompt=...)
         Wait for AGENT_OUTPUT envelope; parse verdict
         bash scripts/post-agent-hook.sh --role code-reviewer --verdict <V> --pr <N> ...
       For each SPEC_READY Discussion:
         bash scripts/pre-spawn-check.sh --role impl-coordinator --discussion <N>
         python3 backend/workflow_runner.py resolve implement-discussion --input ...
         Agent(subagent_type="impl-coordinator", ...) OR Agent(subagent_type="executor", isolation="worktree", ...)
       For each DISCUSSING Discussion with no spec:
         Agent(subagent_type="project-manager", ...)
6.   AUTO-MERGE: for each PR with required gate labels (code-review-passed; +security-review-passed if triggered):
       gh pr merge $PR --squash --delete-branch
       bash scripts/post-merge-hook.sh --pr $PR --discussion $DISC
7.   Heartbeat to project-manager if running (skip if none)
7.5. SUBSYSTEM SWEEP — run ALL of these every iteration, log warnings on failures:
       python3 backend/budget.py status
       python3 backend/cost_tracker.py summary
       python3 backend/kpi_engine.py show
       python3 backend/quality_scorer.py stats
       python3 backend/health_monitor.py check
       python3 backend/audit_trail.py stats
       Append loop-metrics.jsonl line with iteration JSON
       Post one-line status to team-log issue
8.   Update .autonomous-team/now.md
9.   ScheduleWakeup with the same /loop prompt prefixed with "/loop "
```

Steps 0-9 are non-negotiable. The dashboard-side LoopRunner button writes to loop-metrics with `trigger=dashboard` so /health/loop stays green between iterations — that's separate from the canonical loop above.

**Key wrinkles:**
- `backend/trigger.py` is the cron-side launcher that writes a loop-iteration request to the TUI's FIFO. The Team Lead in a Claude Code session does NOT call trigger.py (would lose visibility) — instead the Team Lead IS the loop, executing the 8 steps directly.
- The loop driver decision (Claude Code vs cron Kimi vs both) is the user's call. As of session 85514482 the user chose: "keep me as the loop driver" — Claude Opus stays the Team Lead, cron stays dead.
- If the user types `/loop <directive>`, the directive becomes the goal of the iterations but does not replace the 8-step flow. The directive shapes step 5 (what to act on), nothing else.
