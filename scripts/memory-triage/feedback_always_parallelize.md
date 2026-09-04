---
name: Always parallelize independent work into separate agents
description: Never put independent tasks (cargo test, cargo build, docker health) into one sequential executor — split into parallel agents
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
The Team Lead repeatedly defaults to single executors for work that should be 3+ parallel agents. If steps don't depend on each other's output, they must be separate agents.

**Why:** Single agents take 3-5x longer than parallel ones. The user has called this out multiple times — for the initial verification scripts (3 workstreams), for the verification run itself, and for saas-service specifically (cargo test, cargo build, docker compose are all independent).

**How to apply:** Before spawning any executor, ask: "Can any of these steps run independently?" If yes, split into separate Agent() calls in a single message block. The rule: if step B doesn't need step A's output, they're parallel agents.
