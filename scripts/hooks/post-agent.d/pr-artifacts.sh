#!/usr/bin/env bash
# scripts/hooks/post-agent.d/pr-artifacts.sh — PR test-artifact producer.
#
# Sourced by post-agent-hook.sh during the pr_artifacts step.
# Expects the following variables to be set by the caller:
#   REPO_ROOT   — absolute path to repo root
#   PR          — PR number (may be empty; script no-ops when absent)
#   ROLE        — agent role (e.g. "code-reviewer", "acceptance-tester")
#   CONTENT     — raw AGENT_OUTPUT envelope JSON (may be empty)
#
# Writes one JSONL line per tests_run entry to:
#   .autonomous-team/pr-artifacts/<pr>/<sha>.jsonl
#
# The sha is the HEAD commit of the PR branch, obtained via gh api.
# If PR is unset, sha lookup fails, or tests_run is absent/empty → no-op.

_PA_ARTIFACTS_DIR="${REPO_ROOT}/.autonomous-team/pr-artifacts"

# Only proceed when a PR number is known
if [[ -z "${PR:-}" ]]; then
  return 0
fi

# Only proceed when CONTENT (envelope) is non-empty
if [[ -z "${CONTENT:-}" ]]; then
  return 0
fi

# Extract tests_run from the envelope (JSON array or absent)
_PA_TESTS_RUN=$(python3 - "${CONTENT}" <<'_PYEOF'
import sys, json, re

raw = sys.argv[1]

# Try to parse the outer envelope JSON directly
try:
    d = json.loads(raw)
    tr = d.get("tests_run")
    if tr and isinstance(tr, list) and len(tr) > 0:
        print(json.dumps(tr))
        sys.exit(0)
except Exception:
    pass

# Fallback: extract the JSON block from <!-- AGENT_OUTPUT --> ... <!-- /AGENT_OUTPUT -->
m = re.search(r'<!--\s*AGENT_OUTPUT\s*-->(.*?)<!--\s*/AGENT_OUTPUT\s*-->', raw, re.DOTALL)
if m:
    inner = m.group(1).strip()
    # Strip markdown code fence if present
    inner = re.sub(r'^```[a-z]*\n?', '', inner, flags=re.MULTILINE)
    inner = inner.strip().strip('`')
    try:
        d = json.loads(inner)
        tr = d.get("tests_run")
        if tr and isinstance(tr, list) and len(tr) > 0:
            print(json.dumps(tr))
            sys.exit(0)
    except Exception:
        pass

# No tests_run found
sys.exit(0)
_PYEOF
2>/dev/null || true)

if [[ -z "$_PA_TESTS_RUN" ]]; then
  return 0
fi

# Fetch HEAD sha of the PR branch via gh api
_PA_SHA=$(gh api "repos/${_REPO}/pulls/${PR}" --jq '.head.sha' 2>/dev/null | head -c 8 || true)
if [[ -z "$_PA_SHA" ]]; then
  echo "[pr-artifacts] WARN: could not fetch PR #${PR} head sha — skipping artifact write" >&2
  return 0
fi

# Ensure the target directory exists
_PA_OUT_DIR="${_PA_ARTIFACTS_DIR}/${PR}"
mkdir -p "$_PA_OUT_DIR" 2>/dev/null || {
  echo "[pr-artifacts] WARN: could not create ${_PA_OUT_DIR} — skipping" >&2
  return 0
}

_PA_OUT_FILE="${_PA_OUT_DIR}/${_PA_SHA}.jsonl"
_PA_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Write one JSONL line per tests_run entry
python3 - "$_PA_TESTS_RUN" "$_PA_TS" "${ROLE:-unknown}" "$_PA_OUT_FILE" <<'_PYEOF' 2>/dev/null || true
import sys, json

tests_run_json, ts, agent, out_file = sys.argv[1:5]

try:
    entries = json.loads(tests_run_json)
except Exception as e:
    print(f"[pr-artifacts] WARN: failed to parse tests_run: {e}", file=sys.stderr)
    sys.exit(0)

written = 0
with open(out_file, "a") as fh:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = {
            "command":          entry.get("command", ""),
            "exit_code":        entry.get("exit_code", -1),
            "duration_seconds": entry.get("duration_seconds", 0),
            "ts":               ts,
            "agent":            agent,
        }
        # Include stdout_tail only when the entry provides it
        if "stdout_tail" in entry:
            record["stdout_tail"] = entry["stdout_tail"]
        fh.write(json.dumps(record) + "\n")
        written += 1

print(f"[pr-artifacts] wrote {written} artifact(s) to {out_file}")
_PYEOF
