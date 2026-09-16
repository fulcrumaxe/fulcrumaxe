---
name: start-the-day
description: Morning ritual for Muse — verify state, run sweeps, and drive today's existing plan.
---

# Start the Day

Ported from `.claude/commands/start-the-day.md` — that file remains canonical for Claude Code; this skill is the Muse adaptation. The Claude commands keep working exactly as now.

You are the operator for this project's repo. This is your start-of-session ritual. **Read the script output as your context**, then drive the day per the plan it surfaces. The user redirects only — do NOT ask permission, do NOT generate a fresh plan; act on the existing plan.

## Muse ground rules

- Work in the public repo checkout in front of you (the code plane). Never go looking for a private twin.
- There is no Claude CLI here and no spawn wrapper: implement every step directly yourself, inline, in this session. Do not fan work out to subagents — you are the only executor.
- On a fresh clone with no state yet, populate first: `loop-bootstrap/bootstrap.sh --repo OWNER/NAME <local-path>` (bootstrap-first), then return here.
- For any Discussion or Spec content: restate-never-paste — summarize in your own words, never paste bodies verbatim.

## Step 1: Run the morning ritual

```bash
bash scripts/start-the-day.sh --muse
```

The `--muse` flag matters: it stays on your current branch (no pull, no HEAD restore) and uses fetch-only for the `origin/main` comparison, and it skips the Claude-CLI-only checks. The script will:

1. Report HEAD vs `origin/main` (ahead/behind, fetch-only — it never moves you)
2. Verify `~/.autonomous-forever-state/` and `.autonomous-team/` symlinks intact
3. Run sweeps: budget, subscription, run-analyst (last 12h), open PRs, stats freshness, SPEC_READY Discussion count
4. Print the plan from `.autonomous-team/PLAN-YYYY-MM-DD.md` (today's, or most recent)

## Step 2: Parse the plan output

The plan has P0 → P1 → P2 → P3 → P4 priorities. Items NOT marked done are today's queue.

Today's items may already be partially done from yesterday's late-session work — check the in-flight PR list and open Discussions before starting anything.

## Step 3: Drive immediately

Per the plan's "morning ritual" section:

- Do NOT add new work until P0 / P1 items are closed.
- Create PRs with `gh pr create` (never the `gh api` mutation route). PR creation here is cheap (GraphQL-backed, a few rate-limit points per PR) — re-check budgets with the GraphQL `rateLimit` field, never with the REST rate-limit view, which under-reports GraphQL spend.
- Work items sequentially yourself, smallest scope first. If `.autonomous-team/pending-prs.json` accumulates, run `scripts/drain-pending-prs.sh`.
- Verification gate is "feature works on real input" — not "tests pass". Run the artifact, don't just ship it.

## Step 4: Standing mistakes to avoid

From an earlier session retro, reproduced in full — there is no separate file to read:

1. Escalate worktree contamination to STRUCTURAL within hour 1.
2. Verify state is intact BEFORE starting executor work.
3. Audit existing dashboard tiles for staleness before adding new metrics.
4. Close run-analyst findings same-session — pick top 3 and file Discussions immediately.
5. Verification gate is "feature works on real input" — not "tests pass".

## Step 5: End-of-day update

Before session ends, update `.autonomous-team/PLAN-YYYY-MM-DD+1.md` for tomorrow:

- Mark today's completed items.
- Carry forward incomplete items.
- Add any new Discussions filed that need follow-up.
- Add 5-10 "ideas for new Discussions" if the queue might run dry.

## Repo identity (already set)

- Repo: this public checkout — never hard-code a slug; this skill ships in the open-source export and a hard-coded slug would point every adopter at this project's own repo, not theirs.
- External state dir: `~/.autonomous-forever-state/` (env: `AUTONOMOUS_TEAM_STATE_DIR`).
