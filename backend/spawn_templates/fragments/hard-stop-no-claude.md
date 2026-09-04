## HARD STOP — NO CLAUDE SPAWN

Confirm your actual root before assuming containment: run `pwd`, then `cat .git`.
If `.git` is a directory, you are in the primary shared checkout — nothing you do
locally is contained, and a destructive local operation lands in the tree the whole
team is using. If `.git` is a pointer file (`gitdir: …/worktrees/<id>`), you are
worktree-isolated.

Either way the commands below are FORBIDDEN for you. Worktree-isolated, the
PreToolUse hook blocks them with block_reason "claude_spawn_forbidden". In the
shared checkout it does not — that tier is allowed unconditionally
(hooks/sandbox.py:381-394) — and the rule is yours to keep:

- `claude -p "..."` — direct claude binary invocation
- `/path/to/claude -p "..."` — absolute-path claude invocation
- `bash -c 'claude ...'` or `sh -c 'claude ...'` — shell-wrapped claude invocations
- `exec claude ...` — exec-form claude invocation
- `env FOO=bar claude ...` — env-prefixed claude invocation
- `scripts/spawn-agent.sh` — spawning sub-agents from within a subagent
- `backend/trigger.py` — loop trigger
- `scripts/loop-trigger.sh` — loop trigger
- `scripts/run-loop-iteration.sh` — loop runner
- `backend/_start_loop_run.py` — loop runner

Do NOT attempt any of the above. If you believe you need to spawn a sub-agent or
trigger a loop, STOP and report this as a blocker in your AGENT_OUTPUT envelope.
The Team Lead (not you) is the spawner.
