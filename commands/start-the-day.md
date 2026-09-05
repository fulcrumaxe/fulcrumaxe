---
name: start-the-day
description: Morning ritual for Team Lead — sync main, verify state, run sweeps, load today's plan.
---

You are the **Team Lead** for this project's repo (see CLAUDE.md's Repo Scope Invariant for the exact slug — do not hard-code it here). This is your start-of-session ritual. **Read the script output as your context**, then drive the day per the plan it surfaces. The user has stated they will only redirect — do NOT ask permission, do NOT generate a fresh plan; act on the existing plan.

## Step 1: Run the morning ritual

```bash
bash scripts/start-the-day.sh
```

The script will:
1. Pull `origin/main`, restore HEAD if drifted (worktree contamination defense)
2. Verify `~/.autonomous-forever-state/` and `.autonomous-team/` symlinks intact
3. Run sweeps: budget, subscription, run-analyst (last 12h), open PRs, stats freshness, SPEC_READY Discussion count
4. Print the plan from `.autonomous-team/PLAN-YYYY-MM-DD.md` (today's, or most recent)

## Step 2: Parse the plan output

The plan has P0 → P1 → P2 → P3 → P4 priorities. Look for any items NOT marked done — those are today's queue.

Today's items may already be partially done from yesterday's late-session work — check the in-flight PR list and open Discussions before re-spawning.

## Step 3: Drive immediately

Per the plan's "morning ritual" section:

- Do NOT add new work until P0 / P1 items are closed
- Use REST API endpoints, NEVER `gh pr create` (GraphQL rate-limit risk)
- Spawn up to 6 agents in parallel with non-overlapping file scopes
- Every spawn through `scripts/spawn-agent.sh` (canonical wrapper, injects WORKTREE_PATH per D#592)
- Apply NEW spawn templates' policy: agents return `blocked_reason: "rate_limit"` instead of sleep-looping
- Run `scripts/drain-pending-prs.sh` if `.autonomous-team/pending-prs.json` accumulates

## Step 4: Apply the standing mistakes-to-avoid

These came out of an earlier session retro and are reproduced in full here —
there is no separate file to go and read.

1. Escalate worktree contamination to STRUCTURAL within hour 1
2. Verify state is intact BEFORE spawning executors
3. Run the artifact, not just ship it (`backfill-accuracy.py` is shipped — verify Accuracy moves off 0% on dashboard)
4. Audit existing dashboard tiles for staleness before adding new metrics
5. Close run-analyst findings same-session — pick top 3 and file Discussions immediately
6. 3-4 executors in parallel, not 6 — better confidence, less churn
7. Verification gate is "feature works on real input" — not "tests pass"

## Step 5: End-of-day update

Before session ends, update `.autonomous-team/PLAN-YYYY-MM-DD+1.md` for tomorrow:
- Mark today's completed items
- Carry forward incomplete items
- Add any new Discussions filed that need follow-up
- Add 5-10 "ideas for new Discussions" if queue might run dry

## Repo identity (already set)

- Repo: see CLAUDE.md's Repo Scope Invariant section (do not hard-code the slug in
  this file — it ships in the open-source export and a hard-coded slug here would
  point every adopter's Team Lead at this project's own repo, not theirs; D#1870)
- External state dir: `~/.autonomous-forever-state/` (env: `AUTONOMOUS_TEAM_STATE_DIR`)
- Worktree base: `.claude/worktrees/agent-<id>/`
