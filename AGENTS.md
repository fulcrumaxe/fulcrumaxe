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
4. **Pre-spawn check, every spawn:** before assembling a subagent prompt,
   run `bash scripts/pre-spawn-check.sh --role <role> --discussion <N>
   --event-id <id>`. A block (budget, circuit breaker, dial denial) is a
   hard stop — emit `verdict: fail`, do not work around it.
5. **Render prompts with the renderer:** build every subagent prompt with
   `bash scripts/lib/muse-spawn-prompt.sh --role <role> --brief "<task>"
   --event-id <id> [--discussion <N>]`. It strips Claude-only directives
   (`Agent()` / `spawn-agent.sh`, `$CLAUDE_PROJECT_DIR`, Discussion URLs)
   and appends the Working Principles block plus the `hook_event_id` tag.
6. **Stable event-id per spawn:** generate `<role>-<discussion>-<unix-ts>`
   once per spawn and reuse the same value on every resume and fix round,
   so pre-spawn-check and post-agent-hook dedup idempotently.
7. **`tokens_used` self-report is mandatory:** every spawned agent ends
   with an `AGENT_OUTPUT` envelope that includes `tokens_used`
   (`{"input": N, "output": N}`) — omit it only when genuinely unreadable.
8. **Post-agent hook per run:** after every agent completion, run
   `bash scripts/post-agent-hook.sh --role <role> --discussion <N>
   --verdict <verdict> --input-tokens <N> --output-tokens <N>
   --event-id <id>` so telemetry lands in the same row the spawn opened.
