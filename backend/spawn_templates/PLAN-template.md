# Plan — {{date}}

Project: {{project_name}}

---

## Today's wins (carried from yesterday)

_List up to 3 things that shipped or made meaningful progress._

- (none yet)

## Morning ritual

- [ ] `bash scripts/start-the-day.sh` — pull fresh, check state dir, print this plan
- [ ] Confirm no stale D#NNN references below
- [ ] Check open PRs for any that need review labels before spawning new work

---

## P0 — Must ship today

_Critical blockers. If these don't land, the loop stalls._

<!-- Add P0 items here -->

---

## P1 — High value, start today

_SPEC_READY discussions ready for an executor. Each entry = one spawn._

<!-- Format: D#NNN — short title [SPEC_READY] -->

---

## P2 — Queue for this sprint

_Spec is being written or needs a panel review. Not spawn-ready yet._

<!-- Format: D#NNN — short title [DISCUSSING/SPEC_WRITING] -->

---

## P3 — Backlog

_Acknowledged, not prioritized. Review weekly._

<!-- Format: D#NNN — short title -->

---

## Mistakes to avoid

- Check merged PRs before spawning — plans go stale within hours of merges
- Run `pre-spawn-check.sh` before every executor spawn — budget + circuit-breaker
- Never skip post-merge-hook — audit trail breaks without it
- Never use `git rm` — `git mv` to `archive/<name>-<YYYY-MM-DD>/` instead

---

## Ideas for new Discussions

_Capture improvement ideas here so they don't get lost._

- (add ideas here)
