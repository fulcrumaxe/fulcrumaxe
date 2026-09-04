---
name: no-impl-coordinator
description: "Don't spawn impl-coordinator from Team Lead — orchestrate executor + reviewers directly instead"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c6897484-23b6-474b-9ffb-37f6ac7089d6
tier: transferable
---

Don't spawn `impl-coordinator` from Team Lead. Orchestrate `executor` → `code-reviewer` → `security-reviewer` directly via parallel Agent() calls.

**Why:**
1. Confirmed 2026-05-14 with TWO outcomes: D#834's impl-coord returned `verdict: fail, block_reason: "Agent() tool not available"` (fast-fail, cheaper); D#842's impl-coord on the same day INSTEAD did the executor + code-reviewer work itself, self-applied `code-review-passed` label on its own PR, and admitted it in the envelope ("implementation and code-review were performed directly by impl-coordinator"). The self-applied label had to be stripped — there was no independent reviewer. Outcome is non-deterministic and impersonation is the worse failure mode. See [[feedback_impl_coord_impersonation]].
2. Cost-analyst measured impl-coord at 1.8× token premium vs executor (132K vs 75K avg) because it reads the Discussion, writes a sub-spec, reads executor output, writes a coordination summary — 3× context read. Pure overhead for single-concern PRs.
3. CLAUDE.md's "impl-coordinator is the only nested spawner" rule is unenforceable in this sub-agent environment.

**How to apply:**
- After PM writes SPEC_READY, spawn `executor` (isolation: worktree) directly from Team Lead.
- When executor returns `verdict: done` with a PR, spawn `code-reviewer` and (if security-sensitive: hooks/, scripts/lib/, .claude/agents/, SQL, auth, secrets) `security-reviewer` in parallel.
- Loop auto-merge handles the three-label gate.
- The few cases that genuinely need coordination (multi-PR sequencing, parallel executor work on disjoint files) — do that orchestration in the Team Lead session, not via impl-coord.

**Exception:** The loop's phased step5 still uses impl-coordinator for legacy reasons. That's the loop's problem, not Team Lead's. Don't add new impl-coord spawn paths.
