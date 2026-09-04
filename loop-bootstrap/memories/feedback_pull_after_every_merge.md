---
name: Pull main after every merge
description: After every merge, run git pull on the local main repo so executors and rebases see the actual current main, not stale state
type: feedback
originSessionId: f602f51e-d8cd-4b9e-8d85-4fb81c68c859
tier: transferable
---
After every PR merges (whether via auto-merge or `gh pr merge`), the LOCAL repo (your project's main checkout — not a worktree) is one commit behind origin/main. If multiple PRs merge in a session, local main drifts further behind. This causes:

1. **Rebase races** — when a rebase executor reads "main" from the local repo, it rebases onto stale main, then push fails because real origin/main has moved further. Repeat in a hopeless cycle (PR #438 spent hours in this loop on 2026-05-10).
2. **Conflict-noise** — `git status` shows phantom "modifications" because tests + new files exist on origin/main but weren't pulled locally.
3. **Stale dashboards / scripts** — running CLIs from the local checkout uses outdated code.

**The rule:** After every successful merge (`gh pr merge` or auto-merge confirmed via `gh pr view ... mergedAt`), immediately:

```bash
git pull --ff-only
```

If the pull aborts on untracked-file conflicts (typically because a worktree-based executor created files that origin/main now also has tracked), remove the local untracked copies and retry — they're already on main:

```bash
# Identify blockers from the abort message:
git pull --ff-only 2>&1 | grep -A 20 "would be overwritten"
# Remove them (safe — they exist on origin/main):
rm -f <those-files>
git pull --ff-only
```

If the pull aborts on modified-file conflicts (session noise like `dashboard/e2e/run.mjs` getting touched by branch checkouts), stash them: `git stash push -m "pre-pull session noise" -- <files>` then pull.

**Why:** Executors that work in worktrees share `.git/` with the parent. When they pull origin/main into their worktree, they're fetching commits that the parent's local `main` ref doesn't have yet. The parent's stale main breaks any subsequent operation that reads it.

**How to apply:**
- Bake `git pull --ff-only` into the post-merge hook routine after every PR merge in this conversation
- Before spawning any rebase executor, pull first so the executor's worktree starts from current main
- If a `gh pr merge` returns "not mergeable" with conflicts, the FIRST thing to check is whether local main is behind origin — pull, then assess whether real conflicts exist
- The Team Lead session itself often runs INSIDE a worktree (CWD = `.claude/worktrees/agent-XXX`). Git operations from CWD only affect that worktree's HEAD. To pull the actual repo's main, `cd` (or `git -C`) into **your project's own main checkout path** — not this worktree, and not a hardcoded path copied from another project — then run `pull --ff-only`.
- If local `main` has DIVERGED from `origin/main` (one branch has commits the other doesn't), check what's there: `git log --oneline origin/main..main`. If those commits are stale artifacts from an earlier non-worktree-isolated executor that ran `gh pr checkout` on the parent repo, they're equivalent to a squash-merge of the same PR on origin and `git reset --hard origin/main` is safe. Always `git log` both directions before hard-reset; never reset blind.
