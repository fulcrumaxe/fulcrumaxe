## Bash discipline

### No-sleep rate-limit policy
If a tool/CLI call returns a rate-limit, quota, or transient API error:
- DO NOT wrap the call in `until cmd; do sleep N; done` or any sleep-based retry loop.
- DO return early with AGENT_OUTPUT `verdict: "fail"` and `blocked_reason: "rate_limit"` (or the appropriate reason string).
- Team Lead will respawn you when the rate limit clears. Sleeping holds the agent slot and burns budget for zero progress.

### Check exit codes before consuming output
Every subprocess call returns an exit code. ALWAYS check it before acting on stdout:
- `gh`, `git`, `npm`, `pytest`, `python3`, `curl` all exit non-zero on failure.
- A non-zero exit means stdout is unreliable — do not grep it for success markers.
- On non-zero exit: return AGENT_OUTPUT `verdict: "fail"` with stderr captured in `issues`.
- Pattern: `OUT=$(cmd 2>&1); RC=$?; [ $RC -ne 0 ] && { echo "$OUT"; exit 1; }`

These two rules eliminate the two most common run-analyst findings (`bash_retry_cosmetic_variants`, `tool_output_ignored`).

### When Bash exits non-zero — STOP and read stderr

If your Bash call exits with non-zero status:
1. Read stderr COMPLETELY before doing anything else
2. Identify the root cause (permission? missing file? wrong path? upstream rate limit?)
3. Fix the root cause OR escalate. Do NOT retry with cosmetic variations.

The system actively classifies the following as failure modes and may flag your run:
- Same command with different quoting
- Same command with `2>&1` added/removed
- Same command run from a different cwd via `cd && ...`
- Same command with cosmetic flag reordering

Each retry without root-cause-understanding burns ~1500 tokens. Retries without diagnostic value are a hard anti-pattern.
