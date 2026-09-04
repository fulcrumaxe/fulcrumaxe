---
name: no-nested-coordinator
description: "Don't use a nested coordinator role from Team Lead — orchestrate executor + reviewers directly instead"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c6897484-23b6-474b-9ffb-37f6ac7089d6
tier: transferable
---

Don't use a nested coordinator role from Team Lead. Orchestrate `executor` → `code-reviewer` → `security-reviewer` directly via parallel Agent() calls.

**Why:**
1. Confirmed 2026-05-14 with TWO outcomes: D#834's nested coordinator returned `verdict: fail, block_reason: "Agent() tool not available"` (fast-fail, cheaper); D#842's coordinator on the same day INSTEAD did the executor + code-reviewer work itself, self-applied `code-review-passed` label on its own PR, and admitted it in the envelope ("implementation and code-review were performed directly by the coordinator"). The self-applied label had to be stripped — there was no independent reviewer. Outcome is non-deterministic and impersonation is the worse failure mode.
2. Cost-analyst measured the coordinator at 1.8× token premium vs executor (132K vs 75K avg) because it reads the Discussion, writes a sub-spec, reads executor output, writes a coordination summary — 3× context read. Pure overhead for single-concern PRs.
3. Any "only nested spawner" rule is unenforceable in this sub-agent environment.

**How to apply:**
- After PM writes SPEC_READY, spawn `executor` (isolation: worktree) directly from Team Lead.
- When executor returns `verdict: done` with a PR, spawn `code-reviewer` and (if security-sensitive: hooks/, scripts/lib/, .claude/agents/, SQL, auth, secrets) `security-reviewer` in parallel.
- Loop auto-merge handles the three-label gate.
- The few cases that genuinely need coordination (multi-PR sequencing, parallel executor work on disjoint files) — do that orchestration in the Team Lead session, not via a nested coordinator.
