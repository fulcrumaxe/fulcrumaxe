#!/usr/bin/env bash
# scripts/spawn-agent.sh — Assemble a fully-wired spawn prompt for Agent() calls.
#
# Usage:
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role executor \
#     --discussion 543 \
#     --task-prompt "Implement Discussion #543 ..." \
#     [--isolation worktree] \
#     [--worktree-path /absolute/path/to/worktree] \
#     [--security-trigger])
#   Agent(subagent_type="executor", prompt="$PROMPT")
#
# --worktree-path requires --pr (D#2222): it names the pre-provisioned PR-head
# tree scripts/lib/pr-tree.sh checks out for a --pr spawn, which is a path
# this script controls. A fresh (no --pr) --isolation worktree spawn has no
# such tree for this script to describe — the real one is provisioned by the
# Agent tool's own isolation="worktree" param on the Agent() call below, which
# this script cannot see or influence. Passing --worktree-path without --pr
# is rejected: the Agent() call always provisions its own tree regardless,
# so the flag would only produce a registry entry describing one nothing runs
# in. For the canonical fresh-spawn shape, pass --isolation worktree alone.
#
# The script:
#   1. Generates a stable event-id (<role>-<discussion>-<unix-ts>)
#   2. Runs pre-spawn-check.sh and captures JSON output
#   3. Verifies exit code 0 (budget OK, circuit breaker OK) — exits non-zero if blocked
#   4. Passes PSC JSON + task metadata to backend.prompt_builder via env var
#   5. Prints assembled prompt to stdout
#
# Team Lead then passes stdout into Agent():
#   Agent(subagent_type=ROLE, prompt="$(bash scripts/spawn-agent.sh ...)")
#
# Output contract:
#   - Exit 0 + assembled prompt on stdout  → spawn is allowed, prompt is ready
#   - Exit 1 + error on stderr             → spawn blocked (budget/circuit-breaker)
#
# Environment overrides:
#   SPAWN_AGENT_SKIP_EXIT_TRAP=1  — suppress the EXIT trap (fallback post-agent-hook call).
#     Set this when the SubagentStop hook in .claude/settings.local.json will fire instead,
#     to prevent a double-fire with verdict=unknown before real telemetry arrives.
#     The SubagentStop hook fires with real envelope data; the EXIT trap fires immediately
#     with verdict=unknown (before the subagent even runs).
#     Idempotency in post-agent-hook.sh makes a double-fire safe, but skipping is cleaner.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# Three consumers, all PR-side: the open-PR file-scope conflict scan, and the
# two `repos/<slug>/pulls/<n>` head-sha lookups that call the resolver inline
# rather than reading this variable. Nothing here reads a Discussion through
# either spelling — grep for `_resolve_repo` as well as `_REPO` before
# concluding a call site does not exist.
_CODE_REPO="$(_resolve_code_repo)"

# ── GH_TOKEN: prefer installation token (15k/hr) over user PAT (5k/hr) ───────
# shellcheck source=scripts/lib/gh-token.sh
source "$SCRIPT_DIR/lib/gh-token.sh" || true

# ── AUTONOMOUS_TEAM_STATE_DIR: read from project.json so telemetry lands in ──
# the correct database for forked projects (Bug 4a — forked-project handoff).
# shellcheck source=scripts/lib/state-dir.sh
source "$SCRIPT_DIR/lib/state-dir.sh" || true

ROLE=""
DISCUSSION=""
TASK_PROMPT=""
ISOLATION=""
WORKTREE_PATH_ARG=""
SECURITY_TRIGGER=""
TOUCHPOINTS=""
DRY_RUN_ENV_DUMP=""
NO_REGISTER=""
PR_ARG=""
OPERATION_CLASS=""
# SDK_LANE: set to 1 by --sdk-lane flag or SDK_LANE=1 env var.
# When set, "sdk_eligible":true is added to the SpawnSpec JSON sent to dispatch.py.
# Only takes effect when ROUTE_VIA_DISPATCHER=1; zero behavioral change otherwise.
# Must only be used with low-stakes background roles (docs-writer, run-analyst,
# quality-sweep, feedback-scanner, mission-analyst).  dispatch.py enforces this
# via offload_policy.is_offload_eligible() — the flag alone does not route to SDK.
SDK_LANE="${SDK_LANE:-0}"
# Note: OVERRIDE_CAP intentionally NOT pre-set here
# so that environment variables of the same name are honoured by the ${VAR:-0} expansion below.

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)              ROLE="$2";              shift 2 ;;
    --discussion)        DISCUSSION="$2";        shift 2 ;;
    --task-prompt)       TASK_PROMPT="$2";       shift 2 ;;
    --isolation)         ISOLATION="$2";         shift 2 ;;
    --worktree-path)     WORKTREE_PATH_ARG="$2"; shift 2 ;;
    --security-trigger)  SECURITY_TRIGGER=1;     shift   ;;
    --touchpoints)       TOUCHPOINTS="$2";       shift 2 ;;
    --override-cap)      OVERRIDE_CAP=1;         shift   ;;
    --dry-run-env-dump)  DRY_RUN_ENV_DUMP=1;     shift   ;;
    --no-register)       NO_REGISTER=1;          shift   ;;
    --pr)                PR_ARG="$2";            shift 2 ;;
    --operation-class)   OPERATION_CLASS="$2";   shift 2 ;;
    --sdk-lane)          SDK_LANE=1;             shift   ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 --role <role> --discussion <N> --task-prompt <text> [--isolation worktree] [--worktree-path <path>] [--security-trigger] [--touchpoints <comma-separated-paths>] [--override-cap] [--dry-run-env-dump] [--no-register] [--pr <N>] [--operation-class <class>] [--sdk-lane]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$ROLE" ]]; then
  echo "Error: --role is required" >&2
  exit 1
fi

if [[ -z "$TASK_PROMPT" && -z "$DRY_RUN_ENV_DUMP" ]]; then
  echo "Error: --task-prompt is required" >&2
  exit 1
fi

# D#2222: --worktree-path only makes sense paired with --pr (the pre-provision
# lane below checks out the PR's own branch/sha at a path spawn-agent.sh
# controls). Without --pr there is no tree for this flag to describe — the
# canonical fresh-spawn shape is `--isolation worktree` alone, and the tree
# is provisioned by the Agent tool's own isolation param on the actual
# Agent() call, not by this script. Accepting --worktree-path here anyway
# used to silently produce a registry entry describing a tree the Agent tool
# never uses (it provisions its own regardless) -- see D#2222. Reject loudly
# instead of accepting-and-ignoring.
if [[ -n "$WORKTREE_PATH_ARG" && -z "$PR_ARG" ]]; then
  echo "Error: --worktree-path requires --pr. Without --pr there is no PR branch/sha for" >&2
  echo "this script to check the path out to, and the Agent() tool provisions its own" >&2
  echo "worktree regardless of this flag -- passing it alone produces a registry entry" >&2
  echo "for a tree nothing ever uses (D#2222). For a fresh spawn, drop --worktree-path" >&2
  echo "and pass isolation:\"worktree\" on the Agent() call itself; see" >&2
  echo "scripts/lib/team-lead-prompts.sh for the canonical shape." >&2
  exit 1
fi

# ── env-scrub: build unset list (D#886, narrowed D#1956) ─────────────────────
# Compute the set of env vars that must never reach a subagent's shell, by
# NAME SHAPE alone — this step has no way to tell a credential from a
# same-shaped non-credential, so anything matching one of these patterns is
# swept unless it is explicitly allow-listed below:
#   - ANTHROPIC_API_KEY (direct API auth)
#   - CLAUDE_* (harness config, may carry tokens/paths) — allow-list below
#   - *_API_KEY  (any service key)
#   - *_TOKEN    (any service token)
#
# Allow-list: vars that match one of the patterns above but are NOT
# credentials — they are the agent-to-Team-Lead messaging transport or
# harness session identity, and unsetting them severs the agent's ability to
# report back rather than removing a secret. Add entries only via Discussion.
#   - CLAUDE_CODE_SSE_PORT                          dashboard event-stream port
#   - CLAUDECODE                                    harness marker
#   - CLAUDE_CODE_MESSAGING_SOCKET / _TOKEN         agent-to-Team-Lead transport
#   - CLAUDE_CODE_BRIDGE_SESSION_ID                 harness session identity
#   - CLAUDE_CODE_CHILD_SESSION                     harness session identity
#   - CLAUDE_CODE_SESSION_ID                        harness session identity
#   - CLAUDE_CODE_ENTRYPOINT / CLAUDE_CODE_EXECPATH harness process identity
#   - CLAUDE_PID / CLAUDE_EFFORT                    harness process identity
#
# D#1956: this used to run in two places — this process-level unset (below,
# unconditional) AND a prompt-injected instruction telling the subagent to
# unset the same list as its first Bash step. The prompt-injection lane was
# removed: it was denied by the permission classifier on every observed
# spawn, and even a permitted `unset` would only have protected the single
# Bash call it ran in — each Bash call gets a fresh shell, so there was
# nothing durable for it to enforce. This process-level unset is the only
# lane now, which is why the allowlist above matters: with a bare CLAUDE_*
# glob and no allowlist entry, it would silently strip the messaging
# transport by name shape, not because it is a credential.

_ENV_SCRUB_ALLOWLIST=" CLAUDE_CODE_SSE_PORT  CLAUDECODE  CLAUDE_CODE_MESSAGING_SOCKET  CLAUDE_CODE_MESSAGING_TOKEN  CLAUDE_CODE_BRIDGE_SESSION_ID  CLAUDE_CODE_CHILD_SESSION  CLAUDE_CODE_SESSION_ID  CLAUDE_CODE_ENTRYPOINT  CLAUDE_CODE_EXECPATH  CLAUDE_PID  CLAUDE_EFFORT "

_ENV_SCRUB_VARS=()
while IFS= read -r _var; do
  # Match patterns: ANTHROPIC_API_KEY | CLAUDE_* | *_API_KEY | *_TOKEN
  if [[ "$_var" == "ANTHROPIC_API_KEY" ]] \
     || [[ "$_var" == CLAUDE_* ]] \
     || [[ "$_var" == *_API_KEY ]] \
     || [[ "$_var" == *_TOKEN ]]; then
    # Skip allow-listed vars (e.g. CLAUDE_CODE_SSE_PORT)
    if [[ "$_ENV_SCRUB_ALLOWLIST" == *" $_var "* ]]; then
      continue
    fi
    _ENV_SCRUB_VARS+=("$_var")
  fi
done < <(compgen -v)

_ENV_SCRUB_COUNT="${#_ENV_SCRUB_VARS[@]}"

# Step 1: Unset in this process (for --dry-run-env-dump verification)
for _var in "${_ENV_SCRUB_VARS[@]}"; do
  unset "$_var"
done
unset _var

# Build the unset shell snippet (injected into subagent prompt via Python builder)
_ENV_SCRUB_SNIPPET=""
if [[ "$_ENV_SCRUB_COUNT" -gt 0 ]]; then
  _ENV_SCRUB_SNIPPET="unset"
  for _v in "${_ENV_SCRUB_VARS[@]}"; do
    _ENV_SCRUB_SNIPPET="$_ENV_SCRUB_SNIPPET $_v"
  done
fi
unset _v

# ── --dry-run-env-dump: print scrubbed env and exit (for test verification) ──
# D#2014: when this dry-run is scoped to a PR-amend worktree spawn (--pr +
# --isolation worktree), provision the real pr-tree first and export its
# path as `worktree_path` — this is what lets the acceptance check assert
# the emitted path's HEAD against the PR's real head sha without a full
# spawn. Any other flag combination is byte-identical to before.
if [[ -n "$DRY_RUN_ENV_DUMP" ]]; then
  if [[ -n "$PR_ARG" && "$ISOLATION" == "worktree" ]]; then
    _DRP_INFO=$(gh api "repos/$(_resolve_code_repo)/pulls/${PR_ARG}" --jq '[.head.sha, .head.ref] | @tsv' 2>/dev/null || true)
    _DRP_SHA=$(printf '%s' "$_DRP_INFO" | cut -f1)
    if [[ -n "$_DRP_SHA" ]]; then
      # shellcheck source=scripts/lib/pr-tree.sh
      source "$SCRIPT_DIR/lib/pr-tree.sh"
      _DRP_EVENT_ID="${ROLE:-agent}-${DISCUSSION:-nod}-$(date +%s)-dryrun"
      _DRP_DEST="$REPO_ROOT/.claude/worktrees/pr-${PR_ARG}-${ROLE:-agent}-${_DRP_EVENT_ID}"
      if worktree_path=$(pr_tree_provision "$PR_ARG" "$_DRP_SHA" "$_DRP_DEST" 2>&1); then
        export worktree_path
      else
        echo "WARN: dry-run pr-tree provisioning failed: $worktree_path" >&2
        unset worktree_path
      fi
      unset _DRP_EVENT_ID _DRP_DEST
    else
      echo "WARN: dry-run could not resolve PR #${PR_ARG} head sha for pr-tree provisioning" >&2
    fi
    unset _DRP_INFO _DRP_SHA
  fi
  echo "[env-scrub] scrubbed ${_ENV_SCRUB_COUNT} var(s) matching secret patterns" >&2
  env
  exit 0
fi

# ── 0a. Concurrency cap enforcement ──────────────────────────────────────────
# Refuse to spawn if active executor or total agent count exceeds configured caps.
# Caps are read from control_plane policies; defaults: 4 executors, 8 total.
# Override with --override-cap flag (documented above) for emergency hot-path fixes.
if [[ "${OVERRIDE_CAP:-0}" != "1" ]]; then
  _CAP_EXECUTORS=$(python3 "$REPO_ROOT/backend/control_plane.py" get policies.team_lead.concurrency_cap_executors 2>/dev/null | tr -d '"' || echo "4")
  _CAP_TOTAL=$(python3 "$REPO_ROOT/backend/control_plane.py" get policies.team_lead.concurrency_cap_total 2>/dev/null | tr -d '"' || echo "8")
  # Defaults when policy is not set
  [[ "$_CAP_EXECUTORS" =~ ^[0-9]+$ ]] || _CAP_EXECUTORS=4
  [[ "$_CAP_TOTAL" =~ ^[0-9]+$ ]]     || _CAP_TOTAL=8

  # Reconcile ghost open runs before counting — background Agent() spawns
  # never get end_ts written, so completed agents linger for up to 2h and
  # silently eat cap slots.  Gather live agent IDs from the worktree registry
  # as the positive-liveness set, then close any open rows that are both
  # absent from that set AND older than the grace window (default 30 min).
  _LIVE_IDS=""
  if [[ -f "$REPO_ROOT/scripts/lib/worktree-registry.sh" ]]; then
    _LIVE_IDS=$(bash "$REPO_ROOT/scripts/lib/worktree-registry.sh" list --status active --json 2>/dev/null \
      | python3 -c "
import json, sys
data = json.load(sys.stdin)
# Use event_id when available; fall back to worktree_id
ids = []
for e in data:
    eid = e.get('event_id') or e.get('worktree_id') or ''
    if eid:
        ids.append(eid)
print(' '.join(ids))
" 2>/dev/null || echo "")
  fi
  # Call reconcile — non-fatal, errors are swallowed inside the Python call.
  # D#1655: pass an explicit --stale-after-min instead of relying on the
  # 30-min loop default — interactive sessions never get end_ts written
  # (no SubagentStop hook wired), so the default grace window let completed
  # agents linger and eat cap slots. reconcile-grace.sh detects the hook and
  # returns 5 min (interactive) or 30 min (loop, unchanged) accordingly.
  if [[ -f "$REPO_ROOT/backend/agent_run_tracker.py" ]]; then
    if [[ -f "$REPO_ROOT/scripts/lib/reconcile-grace.sh" ]]; then
      source "$REPO_ROOT/scripts/lib/reconcile-grace.sh"
      _GRACE=$(reconcile_grace_window "$REPO_ROOT/.claude/settings.local.json")
    else
      _GRACE=30
    fi
    python3 "$REPO_ROOT/backend/agent_run_tracker.py" reconcile ${_LIVE_IDS:+--live-ids $_LIVE_IDS} \
      --stale-after-min "$_GRACE" \
      >/dev/null 2>&1 || true
    unset _GRACE
  fi

  # D#2089: supersede any still-open agent_run row for the SAME (role,
  # discussion) pair before counting. A prompt that is built and then
  # discarded (rebuild after a flag correction, an aborted plan) leaves an
  # open row that the reconcile step above cannot touch — the phantoms this
  # fixes are 1-3 minutes old, inside the grace window that protects
  # genuinely-just-started agents, so age cannot tell them apart. Identity
  # can: a second build for the same pair means the first was superseded.
  # Match on the role/discussion COLUMNS (populated by start_run), not the
  # agent_id string, so "executor-2059-" can't also match "executor-20591-".
  # Guarded by -n "$DISCUSSION" (SR-E: no discussion, no matchable key) and
  # -z "$NO_REGISTER" (SR-F: a build-only smoke invocation supersedes
  # nothing). No schema change — complete_run is already an idempotent
  # UPSERT that takes --verdict.
  if [[ -n "$DISCUSSION" && -z "$NO_REGISTER" && -f "$REPO_ROOT/backend/agent_run_tracker.py" ]]; then
    _SUPERSEDE_IDS=$(python3 - "$REPO_ROOT" "$ROLE" "$DISCUSSION" <<'_PYEOF_SUPERSEDE'
import sys
repo_root, role, discussion = sys.argv[1:4]
sys.path.insert(0, repo_root)
try:
    from backend.agent_run_tracker import _db_path
    import duckdb
    db_path = _db_path()
    if not db_path.exists():
        sys.exit(0)
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(
        "SELECT agent_id FROM agent_run WHERE role = ? AND discussion = ? AND end_ts IS NULL",
        [role, int(discussion)],
    ).fetchall()
    conn.close()
    for r in rows:
        print(r[0])
except Exception:
    # Fail-open: a broken/unwritable tracker must never block the spawn.
    pass
_PYEOF_SUPERSEDE
)
    if [[ -n "$_SUPERSEDE_IDS" ]]; then
      while IFS= read -r _old_id; do
        [[ -z "$_old_id" ]] && continue
        echo "Superseding open agent_run row $_old_id — a newer spawn for role=$ROLE discussion=$DISCUSSION replaces its draft (verdict=superseded)." >&2
        python3 "$REPO_ROOT/backend/agent_run_tracker.py" complete --agent-id "$_old_id" --verdict superseded >/dev/null 2>&1 || true
      done <<< "$_SUPERSEDE_IDS"
    fi
    unset _SUPERSEDE_IDS
  fi

  # Count active agents from DuckDB (no end_ts = still running).
  # Agents running > 2 hours are treated as stale and excluded.
  _COUNTS=$(python3 - "$REPO_ROOT" "$_CAP_EXECUTORS" "$_CAP_TOTAL" "$ROLE" <<'_PYEOF_CAPS'
import sys, json
repo_root, cap_exec_s, cap_total_s, role = sys.argv[1:5]
cap_exec = int(cap_exec_s)
cap_total = int(cap_total_s)

try:
    sys.path.insert(0, repo_root)
    from backend.agent_run_tracker import _db_path
    import duckdb
    from datetime import datetime, timezone
    # D#2089: use the same STATS_DB_PATH-aware resolver as start_run/complete_run/
    # reconcile (all go through agent_run_tracker._db_path()) instead of a
    # repo-relative path that ignored the override — the two must agree on
    # which file counts, or a superseded row and the count that sees it could
    # silently point at different databases.
    db_path = _db_path()
    if not db_path.exists():
        print(json.dumps({"active_exec": 0, "active_total": 0, "blocked": False, "reason": "", "rows_rendered": ""}))
        sys.exit(0)
    con = duckdb.connect(str(db_path), read_only=True)
    role_rows = con.execute("""
        SELECT role, COUNT(*) as cnt
        FROM agent_run
        WHERE end_ts IS NULL
          AND start_ts > NOW() - INTERVAL '2 hours'
        GROUP BY role
    """).fetchall()
    role_counts = {r[0]: r[1] for r in role_rows}
    active_exec = role_counts.get("executor", 0)
    active_total = sum(role_counts.values())
    blocked = False
    reason = ""
    counted_rows = []
    if role == "executor" and active_exec >= cap_exec:
        blocked = True
        reason = f"executor cap reached ({active_exec}/{cap_exec} active executors)"
        counted_rows = con.execute("""
            SELECT agent_id, role, start_ts
            FROM agent_run
            WHERE end_ts IS NULL
              AND start_ts > NOW() - INTERVAL '2 hours'
              AND role = 'executor'
            ORDER BY start_ts
        """).fetchall()
    elif active_total >= cap_total:
        blocked = True
        reason = f"total agent cap reached ({active_total}/{cap_total} active agents)"
        counted_rows = con.execute("""
            SELECT agent_id, role, start_ts
            FROM agent_run
            WHERE end_ts IS NULL
              AND start_ts > NOW() - INTERVAL '2 hours'
            ORDER BY start_ts
        """).fetchall()
    con.close()
    # Render the counted-rows block here (not in bash) — agent_id, role,
    # start_ts, age. Nothing else: this must never leak prompt text.
    lines = []
    now = datetime.now(timezone.utc)
    for agent_id, r_role, start_ts in counted_rows:
        st = start_ts
        if hasattr(st, "tzinfo") and st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        age_min = int((now - st).total_seconds() // 60) if hasattr(st, "tzinfo") else None
        age_str = f"{age_min}m ago" if age_min is not None else "unknown age"
        lines.append(f"  {agent_id}  {r_role}  {st}  ({age_str})")
    print(json.dumps({
        "active_exec": active_exec,
        "active_total": active_total,
        "blocked": blocked,
        "reason": reason,
        "rows_rendered": "\n".join(lines),
    }))
except Exception as e:
    # DuckDB unavailable or schema mismatch — fail-open (don't block spawning)
    print(json.dumps({"active_exec": 0, "active_total": 0, "blocked": False, "reason": "", "rows_rendered": ""}))
_PYEOF_CAPS
  )
  _CAP_BLOCKED=$(PSC_JSON_INPUT="$_COUNTS" python3 -c "import sys,json,os; d=json.loads(os.environ['PSC_JSON_INPUT']); print(str(d.get('blocked',False)).lower())" 2>/dev/null || echo "false")
  _CAP_REASON=$(PSC_JSON_INPUT="$_COUNTS" python3 -c "import sys,json,os; d=json.loads(os.environ['PSC_JSON_INPUT']); print(d.get('reason',''))" 2>/dev/null || echo "")
  if [[ "$_CAP_BLOCKED" == "true" ]]; then
    # D#2089: name every counted row — id, start_ts, age — plus the literal
    # recovery command. "4/4 active executors" alone sends the reader looking
    # for four running agents; printing what was actually counted makes the
    # diagnosis a one-read job instead of a database query.
    _CAP_ROWS=$(PSC_JSON_INPUT="$_COUNTS" python3 -c "import sys,json,os; d=json.loads(os.environ['PSC_JSON_INPUT']); print(d.get('rows_rendered',''))" 2>/dev/null || echo "")
    {
      echo "Spawn blocked: concurrency cap — $_CAP_REASON."
      echo "Counted (open agent_run rows in the last 2h):"
      [[ -n "$_CAP_ROWS" ]] && echo "$_CAP_ROWS"
      echo "If none of these is a running agent, clear them:"
      echo "  python3 backend/agent_run_tracker.py reconcile --live-ids --stale-after-min 1"
      echo "Use --override-cap to bypass for emergency spawns."
    } >&2
    exit 1
  fi
fi

# ── 0. PM-gate enforcement ────────────────────────────────────────────────────
# Implementation roles (executor) can only spawn against
# Discussions that have a PM-written Spec (STATUS:SPEC_READY). Prevents Team
# Lead from bypassing PM by writing the per-PR spec inline in the spawn
# prompt — the failure mode observed 2026-05-12 (5 of 6 executors ran on
# bootstrapped-from-plan prompts instead of a PM Spec).
#
# Override: SPAWN_AGENT_ALLOW_NO_SPEC=1 in environment. Caller must document
# the parent umbrella Discussion in the spawn context.
case "$ROLE" in
  executor)
    if [[ -n "$DISCUSSION" && "${SPAWN_AGENT_ALLOW_NO_SPEC:-0}" != "1" ]]; then
      # --fresh bypasses the 300s TTL: a PM's just-written STATUS must be visible
      # to the very next spawn, not read from a stale cache row (D#1778). Exit
      # code 3 means the live fetch failed and this is a STALE fallback — do not
      # trust it for the SPEC_READY check below.
      DISC_BODY=$(python3 "$REPO_ROOT/backend/discussion_cache.py" get-body "$DISCUSSION" --fresh 2>/dev/null)
      DISC_BODY_RC=$?
      if [[ $DISC_BODY_RC -eq 3 ]]; then
        echo "Spawn blocked: could not get a live read of Discussion #$DISCUSSION (GraphQL fetch failed) — only a stale cached body is available, so SPEC_READY status cannot be confirmed. Retry the spawn, or check GitHub API connectivity. Set SPAWN_AGENT_ALLOW_NO_SPEC=1 to override." >&2
        exit 1
      fi
      if [[ -z "$DISC_BODY" ]]; then
        echo "Spawn blocked: cannot read Discussion #$DISCUSSION body — refusing to spawn $ROLE without spec verification. Set SPAWN_AGENT_ALLOW_NO_SPEC=1 to override." >&2
        exit 1
      fi
      # shellcheck source=scripts/lib/spec-ready-gate.sh
      source "$REPO_ROOT/scripts/lib/spec-ready-gate.sh"
      if ! _GATE_REASON=$(spec_ready_gate_check "$DISC_BODY" "$DISCUSSION" 2>&1); then
        echo "$_GATE_REASON" >&2
        exit 1
      fi
      unset _GATE_REASON

      # planned_prs gate (D#2272): the Spec is SPEC_READY, but the role card's
      # "required" wording for planned_prs licensed its own omission and 11 of
      # 16 Discussions shipped without the field anyway. This is the
      # mechanical enforcement that replaces relying on the card being
      # followed. Fetches body+comments itself (does not reuse $DISC_BODY
      # above, which is body-only — PMs post Specs as comments too, D#2064).
      # shellcheck source=scripts/lib/planned-prs-gate.sh
      source "$REPO_ROOT/scripts/lib/planned-prs-gate.sh"
      if ! _GATE_REASON=$(planned_prs_gate_check "$DISCUSSION" 2>&1); then
        echo "$_GATE_REASON" >&2
        exit 1
      elif [[ -n "$_GATE_REASON" ]]; then
        echo "$_GATE_REASON" >&2
      fi
      unset _GATE_REASON

      # Soft warning: check for missing three-section template headers (non-blocking).
      _MISSING_SECTIONS=$(python3 "$REPO_ROOT/backend/discussion_status.py" missing-sections "$DISCUSSION" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(', '.join(data)) if data else print('')
" 2>/dev/null || echo "")
      if [[ -n "$_MISSING_SECTIONS" ]]; then
        echo "WARN: discussion #$DISCUSSION body missing section: $_MISSING_SECTIONS" >&2
      fi
      unset _MISSING_SECTIONS
    fi
    ;;
esac

# ── 0b. external_docs marker gate (Stage 3) ──────────────────────────────────
# Refuses spawn of executor when the source Discussion carries a
#   <!-- MISSING_EXTERNAL_DOCS: a, b, c -->
# marker injected by the PM render gate.
#
# Override: ALLOW_MISSING_EXTERNAL_DOCS=1 in environment.
# Caller MUST also set ALLOW_MISSING_EXTERNAL_DOCS_REASON.
case "$ROLE" in
  executor)
    if [[ -n "$DISCUSSION" ]]; then
      # --fresh: same reasoning as the PM-gate read above — a marker the PM just
      # cleared must not still appear present from a stale cache row (D#1778).
      _ED_DISC_BODY=$(python3 "$REPO_ROOT/backend/discussion_cache.py" get-body "$DISCUSSION" --fresh 2>/dev/null)
      _ED_DISC_BODY_RC=$?
      if [[ $_ED_DISC_BODY_RC -eq 3 ]]; then
        # NOTE (D#1799): this branch does not read ALLOW_MISSING_EXTERNAL_DOCS —
        # that override only applies below, gated on an actual MISSING_EXTERNAL_DOCS
        # marker being present in a body we could read. Here we couldn't get a live
        # body at all, so there's nothing to override: exit code 3 means currency
        # could not be proven, and retrying the spawn is the only real remedy.
        echo "Spawn blocked: could not get a live read of Discussion #$DISCUSSION for the external-docs marker check (GraphQL fetch failed) — only a stale cached body is available, so a cleared MISSING_EXTERNAL_DOCS marker cannot be trusted. Retry the spawn once GitHub API connectivity is restored." >&2
        exit 1
      fi
      if [[ -n "$_ED_DISC_BODY" ]]; then
        _ED_MISSING=$(python3 -c "
import re, sys
body = sys.argv[1]
m = re.search(r'<!--\s*MISSING_EXTERNAL_DOCS:\s*([^-]+?)-->', body)
print(m.group(1).strip() if m else '')
" "$_ED_DISC_BODY" 2>/dev/null || echo "")
        if [[ -n "$_ED_MISSING" ]]; then
          if [[ "${ALLOW_MISSING_EXTERNAL_DOCS:-0}" == "1" ]]; then
            _ED_REASON="${ALLOW_MISSING_EXTERNAL_DOCS_REASON:-no reason given}"
            echo "WARN: ALLOW_MISSING_EXTERNAL_DOCS override for Discussion #$DISCUSSION — missing: $_ED_MISSING — reason: $_ED_REASON" >&2
            python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
from backend.spec_external_docs import write_override_audit
write_override_audit(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5].split(', '))
" "$REPO_ROOT" "$ROLE" "$DISCUSSION" "$_ED_REASON" "$_ED_MISSING"
          else
            echo "Spawn blocked: Discussion #$DISCUSSION has <!-- MISSING_EXTERNAL_DOCS: $_ED_MISSING -->." >&2
            echo "  The PM spec is missing external_docs URLs for: $_ED_MISSING" >&2
            echo "  Fix: add a '### external_docs' block to the Spec with one URL per module." >&2
            exit 1
          fi
        fi
      fi
      unset _ED_DISC_BODY _ED_DISC_BODY_RC _ED_MISSING _ED_REASON
    fi
    ;;
esac

# ── 0c. File-scope claim gate ─────────────────────────────────────────────────
# Refuses spawn when --touchpoints overlap files already claimed by an open PR
# or a live worktree's uncommitted changes.  Opt-in: legacy spawns without
# --touchpoints proceed with a warning (no breaking change).
# D#2153: a read-only role (declared on its role card, see
# scripts/lib/role-capabilities.sh) never writes, so an overlap is reported
# as NOTE: instead of CONFLICT: and does not block the spawn.
# shellcheck source=scripts/lib/role-capabilities.sh
source "$SCRIPT_DIR/lib/role-capabilities.sh"
if [[ -z "$TOUCHPOINTS" ]]; then
  echo "WARN: --touchpoints not set for role=$ROLE discussion=${DISCUSSION:-} — file-scope conflict detection skipped" >&2
else
  _PR_FILES=$(gh pr list --repo "$_CODE_REPO" \
    --state open --json number,files \
    --jq '.[] | {n: .number, f: [.files[].path]} | .f[] | . + " PR#\(.n)"' \
    2>/dev/null || echo "")

  # D#1819: worktree claim classification (MERGED/ABANDONED/STALE/ACTIVE) lives in
  # scripts/lib/worktree-claims.sh — the same module sweep-stale-worktrees.sh
  # consumes, so the gate and the sweep share one definition of "stale"
  # instead of drifting (the old inline commits-behind-only walk here never
  # asked whether a worktree's branch had already landed, so a squash-merged
  # branch looked like live in-flight work forever).
  # shellcheck source=scripts/lib/worktree-claims.sh
  source "$SCRIPT_DIR/lib/worktree-claims.sh"
  # D#2158: a read-only role never writes, so the per-worktree local git work
  # (wtc_cmd_list — the O(N) part of this gate) buys it nothing; skip it and
  # leave the open-PR half above untouched. D#2153 Decision 2 requires the
  # overlap still be REPORTED, not silently dropped — the empty _WT_FILES
  # below makes the worktree half of the matching loop inert on its own, so
  # the loop itself is left in place rather than special-cased.
  if role_is_read_only "$ROLE"; then
    echo "INFO: role=$ROLE is read-only — skipping the worktree half of the file-scope claim gate (cannot write to touchpoints); open-PR overlap check still runs" >&2
    _WT_FILES=""
  else
    _WT_FILES="$(wtc_cmd_list)"
  fi

  _CONFLICT=0
  IFS=',' read -ra _TP_ARRAY <<< "$TOUCHPOINTS"
  for _tp in "${_TP_ARRAY[@]}"; do
    _tp="${_tp# }"; _tp="${_tp% }"
    [[ -z "$_tp" ]] && continue
    _pr_hit=$(printf '%s\n' "$_PR_FILES" | wtc_match_claim "$_tp" || true)
    if [[ -n "$_pr_hit" ]]; then
      _pr_ref=$(echo "$_pr_hit" | grep -oE 'PR#[0-9]+' | head -1 || echo "unknown PR")
      rc_report_claim "$ROLE" "$_tp" "$_pr_ref" && _CONFLICT=1
    fi
    _wt_hit=$(printf '%s\n' "$_WT_FILES" | wtc_match_claim "$_tp" || true)
    if [[ -n "$_wt_hit" ]]; then
      _wt_ref=$(echo "$_wt_hit" | grep -oE 'WT:[^ ]+' | head -1 || echo "unknown worktree")
      rc_report_claim "$ROLE" "$_tp" "$_wt_ref" && _CONFLICT=1
    fi
  done
  unset _PR_FILES _WT_FILES _TP_ARRAY _tp _pr_hit _pr_ref _wt_hit _wt_ref

  if [[ "$_CONFLICT" -eq 1 ]]; then
    exit 1
  fi
  unset _CONFLICT
fi

# ── 0d. Ensure team substrate exists ─────────────────────────────────────────
python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}')
from backend.agent_teams_substrate import ensure_team_exists
try:
    ensure_team_exists()
except Exception as e:
    print(f'[spawn-agent] WARN: ensure_team_exists failed: {e}', file=sys.stderr)
" 2>/dev/null || true

# ── 1. Generate stable event-id ───────────────────────────────────────────────
EVENT_ID="${ROLE}-${DISCUSSION:-nod}-$(date +%s)"

# ── 1a. Resolve model from role agent card ────────────────────────────────────
# Read the 'model:' frontmatter field from .claude/agents/<role>.md.
# This is the authoritative source of which model tier a role uses.
# We capture it here (spawn time) so agent_run rows have non-null model
# without depending on post-completion telemetry.
# Non-fatal: if the card is missing or has no model: field, ROLE_MODEL is empty
# and start_run omits the --model flag (column stays NULL for this row).
ROLE_MODEL=""
_ROLE_CARD="${REPO_ROOT}/.claude/agents/${ROLE}.md"
if [[ -f "$_ROLE_CARD" ]]; then
  ROLE_MODEL=$(python3 -c "
import re, sys
try:
    with open(sys.argv[1]) as f:
        text = f.read(512)   # frontmatter is always in the first 512 bytes
    m = re.search(r'^model:\s*(\S+)', text, re.MULTILINE)
    print(m.group(1).strip() if m else '')
except Exception:
    print('')
" "$_ROLE_CARD" 2>/dev/null || echo "")
fi
unset _ROLE_CARD

# ── 2. Build pre-spawn-check args ─────────────────────────────────────────────
PSC_ARGS=(--role "$ROLE" --event-id "$EVENT_ID")
[[ -n "$DISCUSSION" ]]       && PSC_ARGS+=(--discussion "$DISCUSSION")
[[ -n "$ISOLATION" ]]        && PSC_ARGS+=(--isolation "$ISOLATION")
[[ -n "$OPERATION_CLASS" ]]  && PSC_ARGS+=(--operation-class "$OPERATION_CLASS")
# --touchpoints already exists on this script (file-scope claim gate, 0c above)
# but stopped there — it was never forwarded on to pre-spawn-check.sh, so the
# dial-class derivation there could never see what a spawn actually touches
# (D#1805). One line of plumbing; reuses the flag rather than adding a
# parallel one.
[[ -n "$TOUCHPOINTS" ]]      && PSC_ARGS+=(--touchpoints "$TOUCHPOINTS")
# --no-register: pass --dry-run to skip fleet slot registration in smoke invocations
[[ -n "$NO_REGISTER" ]] && PSC_ARGS+=(--dry-run)

# ── 3. Run pre-spawn-check, capture JSON ──────────────────────────────────────
PSC_RAW=$("$SCRIPT_DIR/pre-spawn-check.sh" "${PSC_ARGS[@]}" 2>/dev/null)
PSC_EXIT=$?

if [[ $PSC_EXIT -ne 0 ]]; then
  echo "Spawn blocked: pre-spawn-check exited $PSC_EXIT for role=$ROLE discussion=${DISCUSSION:-}" >&2
  exit 1
fi

# Extract JSON block via env var (avoids heredoc/pipe stdin-collision — D#977).
PSC_JSON=$(PSC_RAW_INPUT="$PSC_RAW" python3 -c "
import sys, json, os
lines = os.environ['PSC_RAW_INPUT'].splitlines(keepends=True)
start = None; end = None
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('{') and start is None: start = i
    if stripped == '}': end = i
if start is None or end is None: sys.exit(1)
raw = ''.join(lines[start:end+1])
d = json.loads(raw)
print(json.dumps(d))
" 2>/dev/null)

if [[ -z "$PSC_JSON" ]]; then
  echo "Spawn blocked: failed to parse pre-spawn-check JSON for role=$ROLE" >&2
  exit 1
fi

# Double-check the allowed flag (belt + suspenders)
ALLOWED=$(PSC_JSON_INPUT="$PSC_JSON" python3 -c "
import sys, json, os
d = json.loads(os.environ['PSC_JSON_INPUT'])
print(str(d.get('allowed', True)).lower())
" 2>/dev/null || echo "true")
if [[ "$ALLOWED" == "false" ]]; then
  echo "Spawn blocked: pre-spawn-check returned allowed=false for role=$ROLE" >&2
  exit 1
fi

# ── 3a. Spawn notification + dial state snapshot (AC9) ───────────────────────
# Emit a structured notification line to stderr and capture dial state for
# injection into the assembled prompt (PR footer via executor).
_VERB_LABELS='{"1":"ask","2":"propose-confirm","3":"propose-timeout","4":"announce","5":"act"}'
_DIAL_STATE_AT_SPAWN=$(python3 -c "
import sys, json, os
sys.path.insert(0, '${REPO_ROOT}')
verb_labels = {1: 'ask', 2: 'propose-confirm', 3: 'propose-timeout', 4: 'announce', 5: 'act'}
role = '${ROLE}'
disc = '${DISCUSSION:-}'
try:
    from backend.dial_registry import list_directives, _ROLE_TO_DIAL_CLASS
    from datetime import datetime, timezone
    directives = list_directives()
    # Determine dial class for this role
    dial_class = _ROLE_TO_DIAL_CLASS.get(role, 'agent.spawn')
    # Find matching directive
    entry = next((d for d in directives if d['class'] == dial_class), None)
    if entry:
        lvl = entry['level']
        ceil = entry['ceiling']
        verb = verb_labels.get(lvl, str(lvl))
        # Find active TTL
        ttl_str = 'none'
        for directive in entry.get('directives', []):
            ttl = directive.get('ttl_until')
            if ttl:
                try:
                    exp = datetime.fromisoformat(ttl)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < exp:
                        ttl_str = exp.strftime('%Y-%m-%dT%H:%MZ')
                        break
                except Exception:
                    pass
        disc_part = f' for D#{disc}' if disc else ''
        notify_line = f'spawning {role}{disc_part} ({dial_class}: {verb} level {lvl}/{ceil}, TTL {ttl_str})'
        # Build compact dial state string for all classes (for PR footer)
        state_parts = []
        for d in directives:
            cls = d['class']
            lv = d['level']
            vb = verb_labels.get(lv, str(lv))
            state_parts.append(f'{cls}={vb}')
        dial_state_str = ', '.join(state_parts)
        print(json.dumps({'notify': notify_line, 'dial_state': dial_state_str}))
    else:
        disc_part = f' for D#{disc}' if disc else ''
        print(json.dumps({'notify': f'spawning {role}{disc_part} (dial: unknown)', 'dial_state': ''}))
except Exception as e:
    disc_part = f' for D#{disc}' if disc else ''
    print(json.dumps({'notify': f'spawning {role}{disc_part} (dial registry unavailable)', 'dial_state': ''}))
" 2>/dev/null || echo '{"notify":"","dial_state":""}')

_SPAWN_NOTIFY=$(python3 -c "import json,os,sys; d=json.loads(sys.argv[1]); print(d.get('notify',''))" "$_DIAL_STATE_AT_SPAWN" 2>/dev/null || echo "")
_DIAL_STATE_LINE=$(python3 -c "import json,os,sys; d=json.loads(sys.argv[1]); print(d.get('dial_state',''))" "$_DIAL_STATE_AT_SPAWN" 2>/dev/null || echo "")

if [[ -n "$_SPAWN_NOTIFY" ]]; then
  echo "$_SPAWN_NOTIFY" >&2
fi

# Also append to agent-feed.jsonl for dashboard visibility
if [[ -n "$_SPAWN_NOTIFY" ]]; then
  _FEED_FILE="${REPO_ROOT}/.autonomous-team/agent-feed.jsonl"
  printf '{"ts":"%s","event":"spawn_notify","role":"%s","discussion":"%s","message":%s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ROLE" "${DISCUSSION:-}" \
    "$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$_SPAWN_NOTIFY" 2>/dev/null || echo '""')" \
    >> "$_FEED_FILE" 2>/dev/null || true
fi

# ── 3b. Emit start_run event to agent_run_tracker ────────────────────────────
# Skip when --no-register is set (smoke invocation — no Agent() will follow).
if [[ -z "$NO_REGISTER" ]]; then
  python3 "$REPO_ROOT/backend/agent_run_tracker.py" start \
    --agent-id "$EVENT_ID" \
    --role "$ROLE" \
    ${DISCUSSION:+--discussion "$DISCUSSION"} \
    ${PR_ARG:+--pr "$PR_ARG"} \
    --event-id "$EVENT_ID" \
    ${ROLE_MODEL:+--model "$ROLE_MODEL"} \
    2>/dev/null || {
      bash "$SCRIPT_DIR/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] spawn-agent: WARN — start_run failed for $EVENT_ID (non-fatal)" \
        2>/dev/null || true
    }

  python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}')
from backend.agent_teams_substrate import append_team_member
try:
    append_team_member('${EVENT_ID}', '${ROLE}', discussion='${DISCUSSION:-}' or None)
except Exception as e:
    print(f'[spawn-agent] WARN: append_team_member failed: {e}', file=sys.stderr)
" 2>/dev/null || true

  python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}')
from backend.agent_teams_substrate import write_task
try:
    write_task('${EVENT_ID}', {
        'owner': '${ROLE}',
        'status': 'pending',
        'discussion': '${DISCUSSION:-}' or None,
    })
except Exception as e:
    print(f'[spawn-agent] WARN: write_task failed: {e}', file=sys.stderr)
" 2>/dev/null || true
fi

# ── 3c. EXIT trap — suppressed unconditionally ────────────────────────────────
# The EXIT trap previously fired with --verdict "unknown" for any abnormal exit
# before the subagent completed.  "unknown" is never a valid verdict and
# pollutes role_success_rate_24h().  The trap is suppressed either way; this
# detection only decides which log line below is printed.
#
# D#2131: the "present" line no longer claims the hook delivers "real
# telemetry" — that conflated "registered" with "actually closes the row",
# which measurement showed are different claims. It now says only what's
# known here (registered); scripts/lib/reconcile-grace.sh's window choice is
# what checks delivery, from recent agent_run history.
#
# D#2107: reuse reconcile-grace.sh's detection instead of carrying a third
# inlined copy of it — that inlined copy only ever checked settings.local.json,
# which is where this repo's SubagentStop hook is NOT registered (it lives in
# settings.json), so this log line always said "not detected" regardless of
# reality.
if [[ -z "${SPAWN_AGENT_SKIP_EXIT_TRAP:-}" && -z "$NO_REGISTER" ]]; then
  if [[ -f "$REPO_ROOT/scripts/lib/reconcile-grace.sh" ]]; then
    source "$REPO_ROOT/scripts/lib/reconcile-grace.sh"
  fi
  if declare -f reconcile_grace_hook_registered >/dev/null 2>&1 && \
     reconcile_grace_hook_registered "${REPO_ROOT}/.claude/settings.local.json"; then
    echo "[spawn-agent] SubagentStop hook registered — EXIT trap suppressed (unknown verdict is not useful)" >&2
  else
    echo "[spawn-agent] SubagentStop hook not detected — EXIT trap suppressed (unknown verdict is not useful)" >&2
  fi
  # Always suppress: a trap firing with verdict=unknown is never actionable.
  SPAWN_AGENT_SKIP_EXIT_TRAP=1
fi

# EXIT trap is intentionally not registered: SPAWN_AGENT_SKIP_EXIT_TRAP is always
# set above so this block never executes in production paths.
# Kept as documentation of what the old behaviour was.
# if [[ "${SPAWN_AGENT_SKIP_EXIT_TRAP:-0}" != "1" && -z "$NO_REGISTER" ]]; then
#   trap 'bash "$SCRIPT_DIR/post-agent-hook.sh" --role "$ROLE" --verdict "trap_only" ...' EXIT
# fi

# ── 4. Collect prior test-run artifact block + PR head branch (--pr flag) ────
# D#1788: the --jq query below is widened to also pull .head.ref (PR_BRANCH)
# for the {{pr_branch}} template slot — same call site as before, no second
# network round trip added.
#
# D#2014: this section now runs BEFORE worktree-path resolution (section 5,
# below) — it used to run after, which meant the PR head sha was never in
# scope at the point the worktree path was decided. Pure reorder: no logic
# inside this block changed, except that it now also captures the full
# (untruncated) head sha in _PA_SHA_FULL for section 5 to provision a tree
# at. _PA_SHA stays 8 chars, unchanged, for the pr-artifacts.sh lookup below.
PRIOR_TEST_RUNS_BLOCK=""
PR_BRANCH=""
_PA_SHA_FULL=""
if [[ -n "$PR_ARG" ]]; then
  _PA_ERR=$(mktemp)
  _PA_INFO=$(gh api "repos/$(_resolve_code_repo)/pulls/${PR_ARG}" --jq '[.head.sha, .head.ref] | @tsv' 2>"$_PA_ERR" || true)
  _PA_API_FAILED=""
  if [[ -z "$_PA_INFO" ]]; then
    _PA_API_FAILED=1
    echo "WARN: gh api failed to resolve head branch for PR #${PR_ARG}: $(cat "$_PA_ERR")" >&2
  fi
  rm -f "$_PA_ERR"
  # -s: suppress lines with no delimiter instead of printing them whole —
  # without it, an empty/malformed $_PA_INFO makes `cut -f2` echo the SHA
  # (field 1, the only field) into PR_BRANCH.
  _PA_SHA=$(printf '%s' "$_PA_INFO" | cut -f1 | head -c 8)
  _PA_SHA_FULL=$(printf '%s' "$_PA_INFO" | cut -f1)
  PR_BRANCH=$(printf '%s' "$_PA_INFO" | cut -s -f2)

  # D#1788 round 3: pr_number/pr_url are hard-required with no network
  # involved (pure string formatting from --pr). pr_branch is different —
  # it's a best-effort `gh api` lookup that can fail on a rate limit or
  # network blip — but three role templates (docs-writer,
  # accessibility-reviewer, runbook-writer) reference {{pr_branch}} and
  # would otherwise render it as a silent empty string (the exact class of
  # bug this file exists to fix), or a downstream contract error that names
  # "pr_branch" without ever saying the real cause was an API failure.
  # Only those three roles need to hard-fail here — checking the template
  # directly (not a hand-maintained role list) keeps this from drifting the
  # way REQUIRED_VARS did.
  if [[ -n "$_PA_API_FAILED" ]]; then
    _PB_TMPL="$REPO_ROOT/backend/spawn_templates/${ROLE}.tmpl"
    if [[ -f "$_PB_TMPL" ]] && grep -q '{{pr_branch}}' "$_PB_TMPL"; then
      echo "Spawn blocked: role=$ROLE requires {{pr_branch}}, but gh api failed to resolve the head branch for PR #${PR_ARG} (see WARN above). Retry, or check gh auth/rate limits." >&2
      unset _PA_LIB _PA_SHA _PA_INFO _PA_ERR _PA_API_FAILED _PB_TMPL
      exit 1
    fi
    unset _PB_TMPL
  fi

  _PA_LIB="$SCRIPT_DIR/lib/pr-artifacts.sh"
  if [[ -f "$_PA_LIB" && -n "$_PA_SHA" ]]; then
    PRIOR_TEST_RUNS_BLOCK=$(
      SCRIPT_DIR="$SCRIPT_DIR" REPO_ROOT="$REPO_ROOT"
      source "$_PA_LIB"
      inject_for_pr "$PR_ARG" "$_PA_SHA"
    ) 2>/dev/null || true
  fi
  unset _PA_LIB _PA_SHA _PA_INFO _PA_ERR _PA_API_FAILED
fi

# ── 5. Resolve worktree path for prompt injection ────────────────────────────
# D#2014: _WORKTREE_PATH_JSON is now either a real, provisioned path or
# "null" — never a claim about one. The old fallback asserted a worktree at
# the literal, unexpanded string "$(pwd)  # your worktree root — verify
# with: pwd", which was self-contradictory whenever the caller's cwd was not
# actually a worktree (the case this whole Discussion is about). When
# --pr is set and no --worktree-path was supplied, this provisions a tree at
# the PR's head sha via scripts/lib/pr-tree.sh — deliberately NOT
# scripts/lib/verify-tree.sh, which write-protects every tracked file and
# clones with origin pointed at the local checkout, both fatal for a tree
# that needs to be edited and pushed. See pr-tree.sh's header for the full
# split rationale. On any failure to resolve a real path, _WT_UNPROVISIONED
# is set so prompt_builder emits the honest "no tree" block instead.
_WORKTREE_PATH_JSON="null"
_WT_UNPROVISIONED=""
# D#2222: WHY no path was resolved — see backend/prompt_builder.py's
# _build_unprovisioned_worktree_block for how this changes the emitted block.
_WT_UNPROVISIONED_REASON=""
if [[ "$ISOLATION" == "worktree" ]]; then
  bash "$SCRIPT_DIR/setup-state-dir.sh" >/dev/null 2>&1 || true
  if [[ -n "$WORKTREE_PATH_ARG" ]]; then
    (cd "$WORKTREE_PATH_ARG" && bash "$SCRIPT_DIR/setup-state-dir.sh" >/dev/null 2>&1) || true
    for _nm_dir in tui dashboard; do
      _nm_src="$REPO_ROOT/$_nm_dir/node_modules"
      _nm_dst="$WORKTREE_PATH_ARG/$_nm_dir/node_modules"
      if [ -d "$_nm_src" ] && [ ! -e "$_nm_dst" ]; then
        ln -s "$_nm_src" "$_nm_dst"
      fi
    done
    unset _nm_dir _nm_src _nm_dst
    _WORKTREE_PATH_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$WORKTREE_PATH_ARG")
  elif [[ -n "$PR_ARG" ]]; then
    if [[ -n "$_PA_SHA_FULL" ]]; then
      # shellcheck source=scripts/lib/pr-tree.sh
      source "$SCRIPT_DIR/lib/pr-tree.sh"
      _PT_DEST="$REPO_ROOT/.claude/worktrees/pr-${PR_ARG}-${ROLE}-${EVENT_ID}"
      if _PT_PATH=$(pr_tree_provision "$PR_ARG" "$_PA_SHA_FULL" "$_PT_DEST" 2>&1); then
        (cd "$_PT_PATH" && bash "$SCRIPT_DIR/setup-state-dir.sh" >/dev/null 2>&1) || true
        for _nm_dir in tui dashboard; do
          _nm_src="$REPO_ROOT/$_nm_dir/node_modules"
          _nm_dst="$_PT_PATH/$_nm_dir/node_modules"
          if [ -d "$_nm_src" ] && [ ! -e "$_nm_dst" ]; then
            ln -s "$_nm_src" "$_nm_dst"
          fi
        done
        unset _nm_dir _nm_src _nm_dst
        _WORKTREE_PATH_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$_PT_PATH")
      else
        echo "WARN: pr_tree_provision failed for PR #${PR_ARG}: $_PT_PATH" >&2
        _WORKTREE_PATH_JSON="null"
        _WT_UNPROVISIONED=1
        _WT_UNPROVISIONED_REASON="pr_tree_failed"
      fi
      unset _PT_DEST _PT_PATH
    else
      # PR review finding on D#2222's original fix: --pr was given but the
      # head sha could not be resolved earlier (gh api failure — see the WARN
      # above), so pr_tree_provision was never even attempted. This must NOT
      # collapse into the "agent_tool_provisions" case below: for a --pr
      # amend, whatever tree the Agent tool's own isolation happens to hand
      # the agent is NOT the PR's branch, so proceeding there would silently
      # amend the wrong tree. Tag this distinctly and hard-fail.
      echo "WARN: cannot provision PR-amend worktree for PR #${PR_ARG} — head sha resolution failed (see WARN above)" >&2
      _WORKTREE_PATH_JSON="null"
      _WT_UNPROVISIONED=1
      _WT_UNPROVISIONED_REASON="pr_resolution_failed"
    fi
  else
    # D#2222: no --worktree-path, no --pr — this is the canonical fresh-spawn
    # shape (`--isolation worktree` alone; see scripts/lib/team-lead-prompts.sh).
    # spawn-agent.sh has nothing to provision here: the real tree comes from
    # the Agent tool's own isolation="worktree" param on the Agent() call
    # this prompt is handed to. This is NOT a failure — tag it so the
    # rendered block says so instead of telling the agent to hard-fail.
    _WT_UNPROVISIONED=1
    _WT_UNPROVISIONED_REASON="agent_tool_provisions"
  fi
fi
unset _PA_SHA_FULL

# ── 6. Assemble prompt via Python builder (D#977) ────────────────────────────
# All data passed via SPAWN_PROMPT_JSON env var — no heredoc, no eval.
# backend.prompt_builder reads SPAWN_PROMPT_JSON and writes the assembled
# prompt to stdout.
if [[ -n "$_ENV_SCRUB_SNIPPET" ]]; then
  echo "[spawn-agent] env-scrub: injecting unset for ${_ENV_SCRUB_COUNT} secret-pattern var(s)" >&2
fi
unset _ENV_SCRUB_VARS _ENV_SCRUB_COUNT _ENV_SCRUB_ALLOWLIST

# D#1788: the payload used to be built by an inline `python3 -c` heredoc here,
# wrapped in `2>/dev/null` — that combination is why a missing `pr` key went
# unreviewed for as long as it did. It's now backend/spawn_payload.py: an
# importable, testable module. Stderr is captured (not discarded) so a
# genuine failure reaches the operator instead of just producing an empty
# SPAWN_PROMPT_JSON.
_PAYLOAD_ERR="$(mktemp)"
SPAWN_PROMPT_JSON=$(
  PSC_JSON_INPUT="$PSC_JSON" \
  _ROLE="$ROLE" \
  _DISC="${DISCUSSION:-}" \
  _TASK="$TASK_PROMPT" \
  _WT_PATH="$_WORKTREE_PATH_JSON" \
  _WT_UNPROVISIONED="${_WT_UNPROVISIONED:-}" \
  _WT_UNPROVISIONED_REASON="${_WT_UNPROVISIONED_REASON:-}" \
  _SEC="${SECURITY_TRIGGER:-}" \
  _EVENT_ID="$EVENT_ID" \
  _ENV_SCRUB="${_ENV_SCRUB_SNIPPET:-}" \
  _PRIOR_RUNS="${PRIOR_TEST_RUNS_BLOCK:-}" \
  _DIAL_STATE="${_DIAL_STATE_LINE:-}" \
  _PR="${PR_ARG:-}" \
  _PR_BRANCH="${PR_BRANCH:-}" \
  PYTHONPATH="$REPO_ROOT" python3 -m backend.spawn_payload 2>"$_PAYLOAD_ERR"
)
_PAYLOAD_EXIT=$?
unset _ENV_SCRUB_SNIPPET

if [[ $_PAYLOAD_EXIT -ne 0 || -z "$SPAWN_PROMPT_JSON" ]]; then
  echo "Spawn blocked: failed to build prompt JSON for role=$ROLE: $(cat "$_PAYLOAD_ERR")" >&2
  rm -f "$_PAYLOAD_ERR"
  exit 1
fi
rm -f "$_PAYLOAD_ERR"
unset _PAYLOAD_ERR _PAYLOAD_EXIT

_BUILDER_ERR="$(mktemp)"
ASSEMBLED=$(SPAWN_PROMPT_JSON="$SPAWN_PROMPT_JSON" PYTHONPATH="$REPO_ROOT" python3 -m backend.prompt_builder render 2>"$_BUILDER_ERR")
BUILDER_EXIT=$?

if [[ $BUILDER_EXIT -ne 0 || -z "$ASSEMBLED" ]]; then
  echo "Spawn blocked: prompt_builder failed for role=$ROLE (exit $BUILDER_EXIT): $(cat "$_BUILDER_ERR")" >&2
  rm -f "$_BUILDER_ERR"
  exit 1
fi
rm -f "$_BUILDER_ERR"
unset _BUILDER_ERR

# ── 6.5. Spawn injection audit — verify ## Voice block is present ─────────────
# Non-blocking: warn + log but do not abort the spawn.
if ! echo "$ASSEMBLED" | grep -q "## Voice"; then
  echo "WARN: assembled prompt for role=${ROLE} discussion=${DISCUSSION:-} is missing ## Voice block — persona drift guard absent" >&2
  _INJECT_LOG="${REPO_ROOT}/.autonomous-team/hook-events/spawn-injection-$(date +%Y-%m-%d).jsonl"
  printf '{"ts":"%s","role":"%s","discussion":"%s","event_id":"%s","missing":"voice_block"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ROLE" "${DISCUSSION:-}" "$EVENT_ID" \
    >> "$_INJECT_LOG" 2>/dev/null || true
  unset _INJECT_LOG
fi

# ── 6.6. Soft-warning: code-reviewer pytest discipline ────────────────────────
# If assembling a code-reviewer prompt that doesn't mention pytest, warn to stderr.
# SOFT warning only — does NOT block or fail the spawn.
if [[ "$ROLE" == "code-reviewer" ]]; then
  if ! echo "$ASSEMBLED" | grep -qi "pytest"; then
    echo "WARN: code-reviewer prompt for discussion=${DISCUSSION:-} does not include pytest invocation — code-reviewer.pytest_invoked drift risk" >&2
  fi
fi

# ── 6.7. Dispatcher gate (ROUTE_VIA_DISPATCHER=1) ────────────────────────────
# When ROUTE_VIA_DISPATCHER is unset or 0, this entire block is skipped and
# behavior is byte-identical to today (zero change in the default path).
#
# When set to 1:
#   1. Build a SpawnSpec JSON from already-resolved inputs.
#   2. Pipe it to python3 -m backend.orchestrator.dispatch.
#   3. Parse the JSON result envelope:
#      - route=="sdk"  → the SDK already handled the run; emit telemetry + exit 0.
#      - route=="cc"   → continue to step 7 (the existing Agent()/spawn path).
#      - route=="blocked" → abort spawn with exit 1 (credit exhausted, no fallback).
#      - dispatcher crash (unparseable output) → fail-safe: continue on CC path.
#
# SHADOW_MODE (default "alternate") controls sub-routing inside dispatch.py.
# Set SHADOW_MODE=cc to force all calls through the CC path (safe for testing,
# no real SDK calls made).
#
# Routing decision logic (wrapper-only approach):
#   1. Run dispatcher, capture stdout (_DISPATCH_RESULT) and exit code.
#   2. Attempt to parse _DISPATCH_RESULT as JSON and extract the 'route' field.
#   3. If JSON parses cleanly AND has a valid 'route' field:
#      - honor it: sdk→exit 0, blocked→exit 1, cc/both→continue.
#   4. ONLY if JSON is missing or unparseable (a real dispatcher crash):
#      - fail-safe to CC path, regardless of exit code.
#   This separates "dispatcher returned a valid decision (honor it)" from
#   "dispatcher crashed (fail-safe continue)".
if [[ "${ROUTE_VIA_DISPATCHER:-0}" == "1" ]]; then
  # Build SpawnSpec JSON.  role_card_path is derived from the canonical agents dir.
  _ROLE_CARD_PATH="${REPO_ROOT}/.claude/agents/${ROLE}.md"
  [[ -f "$_ROLE_CARD_PATH" ]] || _ROLE_CARD_PATH=""

  # tool_whitelist is intentionally omitted so _dict_to_spec() applies its default
  # (["Read","Bash"]).  Sending an explicit empty list would give SDK agents zero tools.
  #
  # sdk_eligible: set to true when --sdk-lane flag or SDK_LANE=1 env is present.
  # This is the explicit opt-in for the SDK offload lane (D#1322).  dispatch.py
  # still enforces the role allowlist — this flag alone is not sufficient.
  _DISPATCH_SPEC=$(python3 -c "
import json, os, sys
spec = {
    'role':         os.environ.get('_D_ROLE', ''),
    'task_prompt':  os.environ.get('_D_TASK', ''),
    'role_card_path': os.environ.get('_D_ROLE_CARD', ''),
    'isolation':    os.environ.get('_D_ISO', 'worktree'),
    'worktree_path': os.environ.get('_D_WT', ''),
    'env_allowlist': [],
    'discussion':   int(os.environ['_D_DISC']) if os.environ.get('_D_DISC') else None,
    'pr':           int(os.environ['_D_PR']) if os.environ.get('_D_PR') else None,
    'sdk_eligible': os.environ.get('_D_SDK_ELIGIBLE', '0') == '1',
}
print(json.dumps(spec))
" 2>/dev/null) \
    _D_ROLE="$ROLE" \
    _D_TASK="$TASK_PROMPT" \
    _D_ROLE_CARD="${_ROLE_CARD_PATH:-}" \
    _D_ISO="${ISOLATION:-worktree}" \
    _D_WT="${WORKTREE_PATH_ARG:-}" \
    _D_DISC="${DISCUSSION:-}" \
    _D_PR="${PR_ARG:-}" \
    _D_SDK_ELIGIBLE="${SDK_LANE:-0}"

  if [[ -z "$_DISPATCH_SPEC" ]]; then
    echo "[spawn-agent] dispatcher: failed to build SpawnSpec JSON — falling back to CC path" >&2
    _DISPATCH_ROUTE="cc"
  else
    _DISPATCH_RESULT=$(printf '%s' "$_DISPATCH_SPEC" | PYTHONPATH="$REPO_ROOT" python3 -m backend.orchestrator.dispatch 2>/dev/null)
    _DISPATCH_EXIT=$?

    # Parse route from JSON FIRST.  If the JSON is valid and carries a 'route'
    # field, that is the authoritative decision — honor it even when exit != 0
    # (e.g. route=="blocked" exits 1 by design).
    # Only treat a missing or unparseable result as a dispatcher crash and
    # fall back to CC.
    if [[ -n "$_DISPATCH_RESULT" ]]; then
      _DISPATCH_ROUTE=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    r = d.get('route', '')
    # Validate: only known routes are honored; anything else is a crash signal
    if r in ('sdk', 'cc', 'both', 'blocked'):
        print(r)
    else:
        print('__invalid__')
except Exception:
    print('__invalid__')
" "$_DISPATCH_RESULT" 2>/dev/null || echo "__invalid__")
    else
      _DISPATCH_ROUTE="__invalid__"
    fi

    if [[ "$_DISPATCH_ROUTE" == "__invalid__" ]]; then
      # Dispatcher crashed (unparseable output) — fail-safe to CC path.
      echo "[spawn-agent] dispatcher: exited $_DISPATCH_EXIT with unparseable output — falling back to CC path" >&2
      _DISPATCH_ROUTE="cc"
    fi

    _DISPATCH_RUN_ID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('run_id','') or '')" "$_DISPATCH_RESULT" 2>/dev/null || echo "")
    _DISPATCH_ERROR=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('error','') or '')" "$_DISPATCH_RESULT" 2>/dev/null || echo "")
  fi

  if [[ "$_DISPATCH_ROUTE" == "sdk" ]]; then
    # SDK runner handled the spawn — shell wrapper is done.
    echo "routed_via=sdk run_id=${_DISPATCH_RUN_ID:-} role=$ROLE discussion=${DISCUSSION:-}" >&2
    printf '{"routed_via":"sdk","run_id":"%s","role":"%s","discussion":"%s"}\n' \
      "${_DISPATCH_RUN_ID:-}" "$ROLE" "${DISCUSSION:-}"
    exit 0
  elif [[ "$_DISPATCH_ROUTE" == "blocked" ]]; then
    echo "[spawn-agent] dispatcher: spawn blocked — ${_DISPATCH_ERROR:-credit exhausted}" >&2
    exit 1
  else
    # route=="cc" or route=="both" — continue to step 7 with the assembled prompt.
    echo "routed_via=cc run_id=${_DISPATCH_RUN_ID:-} role=$ROLE discussion=${DISCUSSION:-}" >&2
  fi

  unset _ROLE_CARD_PATH _DISPATCH_SPEC _DISPATCH_RESULT _DISPATCH_EXIT _DISPATCH_ROUTE _DISPATCH_RUN_ID _DISPATCH_ERROR
fi

# ── 7. Print assembled prompt to stdout ───────────────────────────────────────
printf '%s' "$ASSEMBLED"
