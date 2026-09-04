# loop-bootstrap

Cold-start kit for deploying the autonomous development loop to a new project.

## What's included

| Directory | Contents | Installed to |
|---|---|---|
| `memories/` | Transferable memory files (tier:transferable) — cross-project agent behavior lessons | `$target/.claude/projects/-<name>-projects/memory/` |
| `scripts/` | Canonical orchestration scripts: spawn-agent.sh, pre-spawn-check.sh, post-agent-hook.sh, subagent-stop-hook.sh, lib/* | `$target/scripts/` |
| `agents/` | Role definitions for all 24 agent roles (executor, code-reviewer, etc.) | `$target/.claude/agents/` |
| `templates/` | Spawn prompt templates for each role | `$target/backend/spawn_templates/` |

## Usage

```bash
bash loop-bootstrap/bootstrap.sh /path/to/target-repo

# Dry run (shows what would be installed, no writes)
bash loop-bootstrap/bootstrap.sh --dry-run /path/to/target-repo
```

## Target repo assumptions

- The target must be a git repository (`git rev-parse` must succeed).
- `$target/scripts/` will be created if absent. Existing files are overwritten.
- `$target/.claude/agents/` will be created if absent.
- `$target/backend/spawn_templates/` will be created if absent.
- A minimal `$target/CLAUDE.md` is created only if one doesn't already exist.
- The memory destination path is derived from the target repo's basename:
  `/path/to/my-project` → `$target/.claude/projects/-path-to-my-project/memory/`

## Re-running (idempotent)

Running bootstrap.sh on an already-bootstrapped repo produces no diff. Files are
overwritten with identical content each time.

## Keeping it current

The files in this directory are snapshots. When the source files in `scripts/`,
`.claude/agents/`, `backend/spawn_templates/`, or `scripts/memory-triage/` change,
re-copy them here. There is no automated sync — by design, the kit is a stable
snapshot, not a live mirror.

## Memory tier filter

Only memory files tagged `tier:transferable` are included. These are cross-project
lessons that apply to any autonomous loop deployment (agent isolation, concurrency,
archive protocol, etc.). Project-specific memories (`tier:project`) are excluded.
