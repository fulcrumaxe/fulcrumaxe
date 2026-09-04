---
name: Use all built subsystems — don't skip protocol steps
description: Team Lead must run every coordination subsystem prescribed in CLAUDE.md, not just the bare minimum scan-and-spawn
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
The Team Lead consistently skips most of the infrastructure built for the loop protocol. This wastes the work put into building circuit breakers, workflow resolution, agent memory, cost tracking, wiki sync, etc.

**Why:** The user built these subsystems specifically to make the autonomous team observable and controllable. Skipping them means no budget tracking, no circuit breaking, no memory injection, no cost visibility — the team runs blind.

**How to apply:** On every loop iteration, use the helper scripts:
- Before EACH spawn: `bash scripts/pre-spawn-check.sh --role <role> --discussion <N>` — gets context, checks circuit breaker + budget
- After EACH agent completion: `bash scripts/post-agent-hook.sh --role <role> --discussion <N> --verdict <V> --input-tokens <N> --output-tokens <N>`
- After EACH PR merge: `bash scripts/post-merge-hook.sh --pr <N> --discussion <N>`
- Start of each iteration: `bash scripts/loop-preflight.sh` — gates, budget, registry sync

Never skip these. If a subsystem is broken, log a warning — don't silently ignore it.
