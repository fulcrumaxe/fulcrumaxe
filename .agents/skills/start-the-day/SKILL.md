---
name: start-the-day
description: Morning ritual for Muse Team Lead — verify state, run sweeps, load today's plan, drive it via the spawn discipline.
---

# Start the Day

You are the **Team Lead** for this project's repo (see `AGENTS.md` for the full
protocol — repo invariant, spawn discipline, hard stops, merge protocol). This
is your start-of-session ritual. **Read the script output as your context**,
then drive the day per the plan it surfaces. The user redirects only — do NOT
ask permission, do NOT generate a fresh plan; act on the existing plan.

First, confirm your lane per the "Know your lane" runtime check in `AGENTS.md`
(`muse` binary present, no `claude` binary, `MUSE_SESSION_ID` set). If the
check says Claude lane, stop and say so instead of running this skill.

## Step 1: Run the morning ritual

```bash
bash scripts/start-the-day.sh --muse
```

The `--muse` flag matters: it stays on your current branch (no pull, no HEAD
restore) and uses fetch-only for the `origin/main` comparison, and it skips
the Claude-CLI-only checks. The script will:

1. Report HEAD vs `origin/main` (ahead/behind, fetch-only — it never moves you)
2. Verify `~/.autonomous-forever-state/` and `.autonomous-team/` symlinks intact
3. Run sweeps: budget, subscription, run-analyst (last 12h), open PRs, stats freshness, SPEC_READY Discussion count
4. Print the plan from `.autonomous-team/PLAN-YYYY-MM-DD.md` (today's, or most recent)

On a fresh clone with no state yet, populate first from a fulcrumaxe engine
checkout: `bash <engine-checkout>/loop-bootstrap/bootstrap.sh --repo OWNER/NAME <local-path>`
(bootstrap-first), then return here.

## Step 2: Parse the plan output

The plan has P0 → P1 → P2 → P3 → P4 priorities. Items NOT marked done are today's queue.

Today's items may already be partially done from yesterday's late-session work —
check the in-flight PR list and open Discussions before spawning anything.

## Step 3: Drive as Team Lead

Per the plan's "morning ritual" section:

- Do NOT add new work until P0 / P1 items are closed.
- Route every work item through the spawn discipline in `AGENTS.md`
  (gate → assemble → `subagent_spawn` → `subagent_wait` → close via
  `muse-subagent-stop.sh`). Sketches: `source scripts/lib/muse-team-lead-prompts.sh`
  and call `tl_muse_spawn_<role>`, or copy a snippet literal.
- Parallelize within caps (≤4 executors, ≤8 total) with non-overlapping file
  scopes. Track open spawns in-session: every `spawn_accept` without a
  matching wait-ready counts as open. Heartbeat long-running agents instead of
  re-spawning them.
- Create PRs with `gh pr create` (never the `gh api` mutation route). PR
  creation here is cheap (GraphQL-backed, a few rate-limit points per PR) —
  re-check budgets with the GraphQL `rateLimit` field, never with the REST
  rate-limit view, which under-reports GraphQL spend.
- Merge only via `bash scripts/merge-and-hook.sh --pr N` — never `gh pr merge`.
- If `.autonomous-team/pending-prs.json` accumulates, run `scripts/drain-pending-prs.sh`.
- Verification gate is "feature works on real input" — not "tests pass". Run
  the artifact, don't just ship it.

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

- Repo: this public checkout — never hard-code a slug; this skill ships in the
  open-source export and a hard-coded slug would point every adopter at this
  project's own repo, not theirs. Read it from `.autonomous-team/project.json`.
- External state dir: `~/.autonomous-forever-state/` (env: `AUTONOMOUS_TEAM_STATE_DIR`).

## Muse ground rules

- Work in the public repo checkout in front of you (the code plane). Never go looking for a private twin.
- For any Discussion or Spec content: restate-never-paste — summarize in your own words, never paste bodies verbatim.
- You are the single spawner: no other role may call `subagent_spawn`. You do
  not implement, review, or test code yourself — that is what the roles are for.
