# AGENTS.md — fulcrumaxe (Muse Code entrypoint)

This repo is **fulcrumaxe**: a self-evolving autonomous development team
that builds and improves itself. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how changes land (fork, branch off `main`, PR, CI must pass).

## Project rules

**Read [CLAUDE.md](CLAUDE.md) for the Team Lead operating protocol**
(identity and boundaries, single-spawner invariant, hard stops,
human-voice standard, per-role protocols under `.claude/agents/`).

Role definitions live in `agents/`; session commands in `commands/`.
Cold-start kit for new adopters: `loop-bootstrap/`.
Commit messages explain *why*, not just *what*.

## Muse deltas (Muse Code sessions only)

1. **Morning ritual:** run `bash scripts/start-the-day.sh --muse`.
   `--muse` stays on your current branch and is fetch-only (no HEAD
   restore, no pull, Claude-CLI checks skipped). On a fresh clone,
   run `bash loop-bootstrap/bootstrap.sh --repo OWNER/NAME <path>`
   first, then the ritual.
2. **No Claude-native spawning here:** do not use `Agent()` /
   `scripts/spawn-agent.sh` and do not shell out to the `claude` CLI
   in Muse sessions. Implement, review, and test directly.
3. **Code plane is this repo:** the public repo is the only remote you
   act on. Never reference the private Discussion-plane repo, and never
   paste internal Discussion prose outward into PR bodies or comments —
   restate findings in your own words against the code.
