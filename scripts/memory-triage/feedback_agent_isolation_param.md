---
name: agent-isolation-param
description: "Agent() tool's isolation:\"worktree\" param is what actually sandboxes a subagent — spawn-agent.sh --isolation worktree only assembles the PROMPT"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 85693ade-3566-4f36-8526-315122a0361d
tier: transferable
---

When spawning an executor (or any code-writing subagent) from the Team Lead session, you MUST pass `isolation: "worktree"` to the Agent tool call itself. The `--isolation worktree` flag passed to `scripts/spawn-agent.sh` ONLY tells the assembled prompt to mention a worktree — it does NOT create one and does NOT sandbox the child process.

**Why:** Without `Agent(isolation:"worktree")`, the subagent runs in the Team Lead's working directory. Three executors in 2026-05-14 all stomped on `<repo-root>` simultaneously: branches got switched, commits landed on the wrong branch (D#738 work ended up on the auto-plan-751 branch and was nearly lost in a rebase), uncommitted changes mixed across three different scopes.

**How to apply:**
- Every `Agent({subagent_type:"executor", ...})` call → add `isolation: "worktree"`
- Same for code-modifying roles: fix re-spawn executor, security-reviewer when re-running tests
- Read-only roles (code-reviewer doing PR diff review, mission-analyst auditing, project-manager writing specs to Discussion body, run-analyst reading JSON) do NOT need worktree isolation — they don't write to the working tree

**Detection:** if `git worktree list` shows only the main repo + `.claude/worktrees/` entries left over from earlier sessions (stale subagent worktrees), and `git status -sb` in main shows uncommitted changes from a spawned executor, you forgot the isolation param.

Cost when forgotten: kill the executors mid-flight, snapshot uncommitted work to `archive/in-progress-snapshots-<ts>/`, reset main, re-spawn correctly. ~3-5 min of cleanup + lost executor tokens.
