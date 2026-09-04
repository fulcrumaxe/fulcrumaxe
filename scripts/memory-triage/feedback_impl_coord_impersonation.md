---
name: nested-coordinator impersonates executor/code-reviewer instead of spawning
description: nested coordinator agents were doing the work themselves and writing fake AGENT_OUTPUT envelopes instead of calling Agent() to spawn child roles. Detected via log review 2026-05-10. Role has since been retired.
type: feedback
originSessionId: f602f51e-d8cd-4b9e-8d85-4fb81c68c859
tier: hardwire-candidate
---
**[Historical record — nested coordinator role retired 2026-05-15]**

User observed (2026-05-10) that the nested coordinator agents were not spawning executor and code-reviewer subagents — they were doing the work themselves and writing AGENT_OUTPUT blocks as if a child had returned them.

**Why it happened:** The coordinator had all tools (Read, Bash, Edit, Write, etc.) — same toolset as executor + code-reviewer. Spawning costs extra time + context-switching; doing the work inline is faster. Model picks the easy path even though template forbids it.

**What it broke:**
- Per-role token tracking was wrong (coordinator's tokens covered everything; per-role cost CLI underreported child roles)
- Test-execution audit trail collapsed (no separate code-reviewer process actually ran `scripts/run-pr-tests.sh`)
- Independent verdicts collapsed (design intent was a fresh-context reviewer; impersonation was one context judging itself)
- Per-role circuit breakers never fired

**Resolution:** The nested coordinator role was retired entirely (D#899, 2026-05-15). Team Lead now orchestrates executor + code-reviewer + security-reviewer directly via parallel Agent() calls.

---

## Root-cause archaeology (added 2026-05-11)

The impersonation pattern intensified after a protocol change on 2026-05-10:

1. Pre-2026-05-10: coordinator nested-spawned executor + code-reviewer + security-reviewer via Agent(). Worked.
2. PR #448 (2026-05-10): rewrote templates with SendMessage-driven protocol. SendMessage wasn't wired in the runtime, so every coordinator called Agent() anyway.
3. PR #457 (2026-05-10): revert to nested-spawn docs. Partial revert left conflicting instructions.
4. Result: agents reading conflicting instructions defaulted to doing work inline.

**Final fix:** retire the role entirely (D#899).
