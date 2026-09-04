---
name: Don't ask permission to act on SPEC_READY work
description: When Discussions are SPEC_READY, spawn executors immediately — never ask "want me to?"
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
When there are SPEC_READY Discussions, act on them. Don't ask "want me to run a loop iteration?" or "should I spawn executors?" — just do it.

**Why:** The user has repeatedly said the loop MUST produce work. Asking for permission to do what CLAUDE.md already prescribes is wasting time.

**How to apply:** If SPEC_READY Discussions exist and pre-spawn checks pass, spawn executors immediately. The only time to pause is when budget is exceeded or circuit breaker is tripped.
