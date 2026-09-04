---
name: Always use the project's real subagent_types — never general-purpose for project work
description: Spawning agents for fulcrumaxe work must use executor / code-reviewer / project-manager / security-reviewer — never general-purpose
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
The Agent tool exposes named subagent_types that match CLAUDE.md's role definitions: `executor`, `code-reviewer`, `security-reviewer`, `project-manager`, `acceptance-tester`. Each comes with role-specific system prompts and is what the post-agent-hook + label flow expects. Picking `general-purpose` because it's the first option visible bypasses all of that.

**Why:** The user explicitly built role-specific agents to enforce different responsibilities. An `executor` writes code but doesn't review it. A `code-reviewer` reviews but doesn't merge. The pipeline (`code-review-passed` label gates auto-merge) depends on the right role doing the right thing. `general-purpose` produces code that looks fine but skips the team's quality machinery — and then the human (or another agent) doesn't know whether it was reviewed or not.

**How to apply:**
- Implementation work (write code, create files, run preflight, open a PR): `subagent_type="executor"`
- Reviewing a PR diff: `subagent_type="code-reviewer"`
- Security review when triggered (server.py, *.env, auth, secret, credential, subprocess, fetch, localStorage): `subagent_type="security-reviewer"`
- Coordinating one Discussion end-to-end (executor → reviewer → merge): Team Lead orchestrates directly via parallel `Agent()` calls — there is no separate coordinator role to spawn (single-spawner invariant)
- Writing a Spec for a Discussion: `subagent_type="project-manager"`
- Verifying acceptance criteria: `subagent_type="acceptance-tester"`
- Open-ended research, codebase exploration with no implementation: `subagent_type="Explore"` (NOT general-purpose)
- `general-purpose` is reserved for tasks unrelated to fulcrumaxe's protocol (e.g. answering a question about an external library)

When two or more agents will touch the same file in parallel, ALWAYS pass `isolation: "worktree"`. The harness creates a fresh git worktree per agent so backend/api.py edits don't collide. Without it, the second agent's commit fails or overwrites the first.

**Worktree isolation is also REQUIRED for any executor that runs `gh pr checkout`, `git checkout <branch>`, `git rebase`, or otherwise touches branch state.** Without isolation, those commands run in the SHARED main repo and switch the branch out from under everyone else. On 2026-05-10 a rebase executor without `isolation: "worktree"` left the main repo on `feature/410-ideas-live-data` for an extended period, causing confusion and risking commits-on-wrong-branch. Default-on rule: if the executor brief mentions any of `gh pr checkout`, `git checkout`, `git rebase`, `git merge`, `git push`, ALWAYS pass `isolation: "worktree"`. No exceptions.

**Even WITH worktree isolation, parent repo's branch can drift if any sub-process leaks `git checkout` or `gh pr create` into the parent's git context.** Multiple times in the origin project's history the parent repo (your project's main checkout, not a worktree) was found on `discussion-XXX-*` branches that should have stayed inside worktrees. Suspected mechanism: when `gh pr create` runs from a worktree, gh push uses a branch ref that may also become the parent's HEAD if the worktree's `.git` file points back. **Mitigation:** the post-merge-hook's auto-pull from #461 catches stale parent state for `main` ahead-only, but not parent-on-wrong-branch. After spawning parallel impl-coords, the Team Lead should periodically run `git -C <your-main-checkout-path> branch --show-current` and switch back to `main` if it drifted. Plan: file a Discussion for a hook that auto-detects and self-heals parent-repo-on-wrong-branch after every spawn batch.

**Executors that run destructive git ops (`git reset --hard`, `git checkout -- .`, `git clean -fd`) MUST verify branch context BEFORE executing.** On 2026-05-10 an executor for #475 ran `git reset --hard origin/main` based on a stale gitStatus snapshot in its session-start context BEFORE checking `git branch --show-current`. It happened to be harmless because the executor was on a fresh worktree branch, but the order was unsafe. Spec rule for executor briefs going forward: "Verify `git branch --show-current` matches your expected worktree branch BEFORE any destructive git operation. Never trust the gitStatus snapshot from session-start context — it reflects the parent repo's state at the time the harness was invoked, not the worktree's current state."
