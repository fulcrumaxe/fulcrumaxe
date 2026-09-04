#!/usr/bin/env bash
# scripts/lib/ci-status-check.sh — CI-status merge gate (D#1614).
#
# GitHub-native required-status-checks is unavailable on this repo tier (403 —
# needs GitHub Pro). This lib re-implements that gate in our own scripts so a
# red GitHub Actions run is a hard merge block instead of decorative.
#
# Public contract:
#   check_ci_status <pr> <repo> [--wait]
#     0 = every required check-run is present, completed, and green
#     1 = otherwise (fail-closed on any ambiguity)
#     2 = CI is intentionally OFF — the repo variable CI_DISABLED reads
#         exactly "true", so no check-run can ever appear for this head.
#         This is a STAND-DOWN, not a pass: callers must proceed loudly and
#         record it (ci_gate_stood_down), never treat it as a green signal.
#     Sets on return: CI_STATUS_STATE (pass|fail|pending|disabled),
#                      CI_STATUS_FAIL_REASON, CI_STATUS_FAILING_CHECKS,
#                      CI_STATUS_RUN_URL, CI_STATUS_HEAD_SHA (head SHA evaluated)
#
#   check_ci_provenance_gate <pr> <repo> <discussion>
#     0 = CI result may be honored
#     1 = PR traces to a provenance:external Discussion, touches
#         .github/workflows/**, and the D#1588 intake-approved human gate has
#         not cleared — CI's own self-reported green is not trustworthy here.
#     Delegates to scripts/lib/external_intake_gate.py — does not reinvent it.
#
#   ci_merge_sha_pinned <pr> <repo> <sha> [delete_branch_mode]
#     0 = merged (SHA-pinned via `gh pr merge --match-head-commit`)
#     9 = aborted — head moved since <sha> was evaluated (TOCTOU re-gate needed)
#     1 = merge command failed for any other reason
#     [delete_branch_mode] (D#2020): "delete" (default) or "keep" — pass
#     "keep" to omit --delete-branch, e.g. when scripts/lib/pr-dependents.sh
#     found an open PR based on this branch.
#
#   ci_write_audit <kind> <pr> <head_sha> <failing_checks> <run_url> <reason>
#     Appends one durable JSON line to <state_dir>/audit.jsonl (or
#     $CI_STATUS_TEST_AUDIT_FILE in test mode).
#
#   ci_note_merge_if_unverified <pr> <sha> [audit_already_written]
#     D#2271 PR-a: call once, right after a merge actually succeeds. Writes
#     the CI_GATE_UNVERIFIED_MERGE_KIND fallback row unless CI_STATUS_STATE
#     is "pass" or the caller already wrote its own row for the decline.
#     Feeds backend/gate_streak.py — see the "Gate streak markers" comment
#     below for the full design.
#
# Required check-run name allowlist (D#1608/#1610 incident: a naive per-PR
# subset would have excluded the exact backend job that was red — require the
# whole matrix, every time, by exact name):
CI_REQUIRED_CHECKS=("tui" "dashboard" "ts-backend" "backend (import-smoke)")

# ── Gate streak markers (D#2271 PR-a) ───────────────────────────────────────
# 138 ci_gate_stood_down rows sat in the audit trail for two weeks, each one
# correct and each one identical — a signal that fires every time and blocks
# nothing is indistinguishable from no signal at all. backend/gate_streak.py
# turns the trail into one escalating number instead: merges since the CI
# gate last verified something for real. This lib is where both halves of
# that number get written.
#
# CI_GATE_VERIFIED_KIND: written on the ONE branch below that means a
# required check-run was actually read and found green. Every caller that
# reaches STATUS=pass gets this "for free" — nothing at either call site
# needs to know this kind's name, which is the whole point: the reader
# (backend/gate_streak.py) resets its count on this kind and increments on
# every other kind-bearing row, so a future bypass — named or not — cannot
# evade the count without also faking a verified result, which is a lie
# visible in review rather than a forgotten registration.
CI_GATE_VERIFIED_KIND="ci_gate_verified"
# CI_GATE_UNVERIFIED_MERGE_KIND: the fallback half. Written by
# ci_note_merge_if_unverified (below) only when a merge actually proceeds
# without CI_STATUS_STATE having reached "pass" AND without the caller
# having already written its own row documenting why (ci_gate_stood_down,
# manual_merge_ci_bypass, or anything invented later). A bypass that merges
# and writes nothing of its own still leaves this row behind — there is no
# name to register to avoid it, because this call site does not check names
# at all, only whether a row already exists for this merge.
CI_GATE_UNVERIFIED_MERGE_KIND="ci_gate_unverified_merge"

# gh pr checks --json does not expose app.slug (confirmed: `gh pr checks
# --help` JSON FIELDS list has no app/slug field on gh 2.93). The commits/
# {sha}/check-runs REST endpoint does, so this lib uses that endpoint as its
# single source of truth for both the matrix rule and the trust filter —
# per the Spec's documented fallback choice.
CI_MAX_WAIT_SECONDS="${CI_MAX_WAIT_SECONDS:-1200}"
CI_POLL_INTERVAL="${CI_POLL_INTERVAL:-25}"

CI_STATUS_FAIL_REASON=""
CI_STATUS_FAILING_CHECKS=""
CI_STATUS_RUN_URL=""
CI_STATUS_HEAD_SHA=""
# Outcome of the last check_ci_status call as a token, so callers branch on a
# string instead of an exit code alone: pass | fail | pending | disabled.
# `disabled` is deliberately NOT a value any check-run input can produce — it
# is derived only from the repo variable (see _ci_kill_switch_state).
CI_STATUS_STATE=""
# Coarse classification of the last merge failure, so callers branch on a token
# instead of grepping prose: conflict | head-moved | permissions | api | unknown.
CI_STATUS_FAIL_KIND=""
# Best-effort conflicting-path list (see ci_conflicting_files). When the list
# cannot be computed, CI_CONFLICT_FILES is empty and CI_CONFLICT_FILES_REASON
# says why -- callers MUST print the reason rather than omitting the list.
CI_CONFLICT_FILES=""
CI_CONFLICT_FILES_REASON=""

_CI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CI_REPO_ROOT="$(cd "$_CI_LIB_DIR/../.." && pwd)"

# ── CI kill switch (D#1944) ────────────────────────────────────────────────
# `ci.yml` gates all three jobs on `vars.CI_DISABLED != 'true'`. A job-level
# `if:` is evaluated BEFORE matrix expansion, so when the switch is on the
# matrix job registers one placeholder check-run named for the unexpanded
# `${{ matrix.workspace }}` expression instead of the three per-leg names this
# gate requires — they can never appear, and the gate blocks every merge.
#
# The fix is for this gate to read the same variable `ci.yml` reads and stand
# down explicitly. Three properties this must hold, in order of how easy they
# are to break:
#
#  1. `disabled` is derived from the repo variable and NOTHING else. It is
#     never inferred from absent or skipped check-runs — "no checks present"
#     with CI enabled stays a hard block (the D#1614 property).
#  2. A read failure is neither "on" nor "off". 404 means the variable is
#     authoritatively absent -> run the gate normally. 403/5xx/network/CLI
#     failure means we do not know -> hard block. The idiom
#         [ "$(gh api ... --jq .value 2>/dev/null)" = "true" ]
#     collapses all three of those into "not disabled", because `gh api` exits
#     non-zero on 404 AND on 403 — which is exactly the hole this closes. So
#     we read the status line with `gh api -i` and branch on the HTTP code.
#     Measured 2026-08-19 on this repo: `-i` prints `HTTP/2.0 200 OK` (rc=0)
#     and `HTTP/2.0 404 Not Found` (rc=1) to stdout in both cases.
#  3. The string comparison matches `ci.yml` byte for byte: exactly `true`,
#     case-sensitive, no trimming. `True`, `1`, `yes`, ` true ` are all "not
#     disabled" in the workflow, so they must be here too.
#
# Echoes exactly one of: disabled | enabled | unknown.
#
# CI_KILL_SWITCH_OVERRIDE is a TEST SEAM ONLY and is honoured solely when
# CI_STATUS_TEST_MODE=1. An env var must never be able to produce a
# stand-down in production — that would make this `--force-no-ci` with a
# nicer name (see tests/test_ci_status_check.sh CS-15).
read -r -d '' _CI_KILL_SWITCH_PY <<'PYEOF' || true
import json, re, sys

raw = sys.stdin.read()
first = raw.split("\n", 1)[0]
m = re.search(r"\b([0-9]{3})\b", first)
status = m.group(1) if m else ""

if status == "404":
    # Authoritatively absent. This is the ONE failure-shaped outcome that is
    # not ambiguous: the variable does not exist, so CI is not disabled.
    print("enabled")
elif status == "200":
    parts = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
    value = None
    if len(parts) == 2:
        try:
            obj = json.loads(parts[1].strip())
            if isinstance(obj, dict):
                value = obj.get("value")
        except Exception:  # noqa: BLE001 — unparseable 200 is "we don't know"
            value = None
    if value is None:
        print("unknown")
    else:
        # Byte-exact, same as `vars.CI_DISABLED != 'true'` in ci.yml.
        print("disabled" if value == "true" else "enabled")
else:
    print("unknown")
PYEOF

_ci_kill_switch_state() {
  local repo="$1"

  if [ "${CI_STATUS_TEST_MODE:-}" = "1" ] && [ -n "${CI_KILL_SWITCH_OVERRIDE+set}" ]; then
    case "${CI_KILL_SWITCH_OVERRIDE}" in
      HTTP_404)                       printf 'enabled' ;;
      HTTP_403|HTTP_500|GH_API_ERROR) printf 'unknown' ;;
      true)                           printf 'disabled' ;;
      # Any other value stands in for "HTTP 200 with this value" — and only
      # the byte string `true` above yields disabled.
      *)                              printf 'enabled' ;;
    esac
    return 0
  fi

  local raw
  raw="$(gh api -i "repos/${repo}/actions/variables/CI_DISABLED" 2>&1)" || true
  printf '%s' "$raw" | python3 -c "$_CI_KILL_SWITCH_PY" 2>/dev/null || printf 'unknown'
}

# ── Test-seam master key ─────────────────────────────────────────────────
# CI_STATUS_TEST_MODE=1 arms FOUR behaviours, all in this file:
#   1. CI_KILL_SWITCH_OVERRIDE  — kill-switch stand-down mock (_ci_kill_switch_state)
#   2. CI_STATUS_HEAD_SHA_<pr>  — head-SHA mock (_ci_fetch_head_sha)
#   3. CI_STATUS_OVERRIDE_<pr>  — check-runs mock (_ci_fetch_check_runs_json)
#   4. CI_STATUS_TEST_AUDIT_FILE — audit-log redirect (_ci_audit_path)
# One flag is a master key: leaking CI_STATUS_TEST_MODE=1 into a cron or loop
# environment re-arms all four at once, not just the one a caller intended.

# ── Head SHA + check-runs fetch (test-override aware) ───────────────────────
_ci_fetch_head_sha() {
  local pr="$1" repo="$2"
  local mock_var="CI_STATUS_HEAD_SHA_${pr}"
  if [ -n "${!mock_var:-}" ]; then
    if [ "${CI_STATUS_TEST_MODE:-}" = "1" ]; then
      printf '%s' "${!mock_var}"
      return 0
    fi
    echo "_ci_fetch_head_sha: ignoring ${mock_var} — set CI_STATUS_TEST_MODE=1 to honour it" >&2
  fi
  gh pr view "$pr" --repo "$repo" --json headRefOid --jq .headRefOid 2>/dev/null
}

_ci_fetch_check_runs_json() {
  local pr="$1" repo="$2" sha="$3"
  local mock_var="CI_STATUS_OVERRIDE_${pr}"
  if [ -n "${!mock_var:-}" ]; then
    if [ "${CI_STATUS_TEST_MODE:-}" = "1" ]; then
      local mock_val="${!mock_var}"
      if [ "$mock_val" = "GH_API_ERROR" ]; then
        return 1
      fi
      printf '%s' "$mock_val"
      return 0
    fi
    echo "_ci_fetch_check_runs_json: ignoring ${mock_var} — set CI_STATUS_TEST_MODE=1 to honour it" >&2
  fi
  gh api "repos/${repo}/commits/${sha}/check-runs" --jq '.check_runs' 2>/dev/null
}

# ── Pure evaluation: check_runs JSON (list) -> STATUS/REASON/FAILING/URL ────
# Structured JSON parsing only — never a bare substring text match on the pass token.
#
# NB: the evaluator script is loaded via `python3 -c` (not `python3 - <<EOF`),
# because a heredoc redirect on `python3 -` consumes stdin as the *program
# text* itself, leaving nothing for the script's own sys.stdin.read() call —
# `-c` keeps stdin free for the piped check-runs JSON.
read -r -d '' _CI_EVAL_PY <<'PYEOF' || true
import json, sys

required = sys.argv[1:]

try:
    raw = sys.stdin.read()
    runs = json.loads(raw) if raw.strip() else []
    if not isinstance(runs, list):
        runs = runs.get("check_runs", []) if isinstance(runs, dict) else []
except Exception as e:  # noqa: BLE001 — malformed/garbled response is a hard block
    print("STATUS=fail")
    print(f"REASON=malformed check-runs response: {e}")
    print("FAILING=")
    print("URL=")
    sys.exit(0)

# Trust filter: only check-runs posted by the real github-actions App are
# honored. A fork can install a third-party App and post a spoofed "success"
# check-run under a required name — this filter rejects it (D#1614 AC-5).
trusted = [
    r for r in runs
    if isinstance(r, dict) and (r.get("app") or {}).get("slug") == "github-actions"
]

if not trusted:
    # Fail-closed (AC-6/AC-7): zero registered check-runs is "pending", NEVER "pass".
    print("STATUS=pending")
    print("REASON=no github-actions check-runs registered yet for this head")
    print("FAILING=")
    print("URL=")
    sys.exit(0)

by_name = {}
for r in trusted:
    name = r.get("name")
    if name in required:
        by_name[name] = r  # last occurrence wins (reruns appear later)

missing, pending, failing = [], [], []
for name in required:
    r = by_name.get(name)
    if r is None:
        missing.append(name)
        continue
    if r.get("status") != "completed":
        pending.append(name)
    elif r.get("conclusion") not in ("success", "skipped"):
        failing.append((name, r.get("html_url") or ""))

if missing:
    print("STATUS=fail")
    print("REASON=required check absent: " + ", ".join(missing))
    print("FAILING=" + ",".join(missing))
    print("URL=")
elif failing:
    names = ",".join(n for n, _ in failing)
    url = next((u for _, u in failing if u), "")
    print("STATUS=fail")
    print("REASON=required check(s) failed: " + names)
    print("FAILING=" + names)
    print("URL=" + url)
elif pending:
    print("STATUS=pending")
    print("REASON=required check(s) still pending: " + ",".join(pending))
    print("FAILING=")
    print("URL=")
else:
    print("STATUS=pass")
    print("REASON=")
    print("FAILING=")
    print("URL=")
PYEOF

_ci_evaluate() {
  python3 -c "$_CI_EVAL_PY" "$@"
}

# ── Public: check_ci_status <pr> <repo> [--wait] ────────────────────────────
check_ci_status() {
  local pr="$1" repo="$2" wait_mode="${3:-}"
  CI_STATUS_FAIL_REASON=""
  CI_STATUS_FAILING_CHECKS=""
  CI_STATUS_RUN_URL=""
  CI_STATUS_HEAD_SHA=""
  CI_STATUS_STATE=""

  # Kill-switch read comes FIRST — before the head SHA and check-run fetches.
  # When CI is off there are no check-runs to spend an API call on, and no
  # amount of waiting will make them appear.
  local kill_switch
  kill_switch="$(_ci_kill_switch_state "$repo")"
  case "$kill_switch" in
    disabled)
      CI_STATUS_STATE="disabled"
      CI_STATUS_FAIL_REASON=""
      return 2
      ;;
    unknown)
      CI_STATUS_STATE="fail"
      CI_STATUS_FAIL_REASON="could not determine CI_DISABLED state (repo variable read failed with a non-404 status) — failing closed"
      return 1
      ;;
  esac

  # Bounded by construction — a fixed max iteration count, not an open-ended retry.
  local max_iters=1
  if [ "$wait_mode" = "--wait" ]; then
    max_iters=$(( (CI_MAX_WAIT_SECONDS / CI_POLL_INTERVAL) + 1 ))
    [ "$max_iters" -lt 1 ] && max_iters=1
  fi

  local i
  for (( i = 1; i <= max_iters; i++ )); do
    local sha
    sha="$(_ci_fetch_head_sha "$pr" "$repo")"
    if [ -z "$sha" ]; then
      CI_STATUS_STATE="fail"
      CI_STATUS_FAIL_REASON="could not resolve head SHA for PR #$pr (gh pr view failed)"
      return 1
    fi
    CI_STATUS_HEAD_SHA="$sha"

    local raw rc=0
    raw="$(_ci_fetch_check_runs_json "$pr" "$repo" "$sha")" || rc=$?
    if [ "$rc" -ne 0 ]; then
      CI_STATUS_STATE="fail"
      CI_STATUS_FAIL_REASON="gh api call for check-runs failed (pr=$pr sha=$sha) — failing closed"
      return 1
    fi

    local result status="" reason="" failing="" url=""
    result="$(printf '%s' "$raw" | _ci_evaluate "${CI_REQUIRED_CHECKS[@]}")"
    while IFS='=' read -r k v; do
      case "$k" in
        STATUS)  status="$v" ;;
        REASON)  reason="$v" ;;
        FAILING) failing="$v" ;;
        URL)     url="$v" ;;
      esac
    done <<< "$result"

    case "$status" in
      pass)
        CI_STATUS_STATE="pass"
        # D#2271 PR-a: the positive-signal marker. Written here, once, on
        # the single branch that means the required checks were actually
        # read and found green — see the CI_GATE_VERIFIED_KIND comment
        # near the top of this file for why this is the only place it
        # needs to be written.
        ci_write_audit "$CI_GATE_VERIFIED_KIND" "$pr" "$sha" "" "" "required checks green"
        return 0
        ;;
      fail)
        CI_STATUS_STATE="fail"
        CI_STATUS_FAIL_REASON="$reason"
        CI_STATUS_FAILING_CHECKS="$failing"
        CI_STATUS_RUN_URL="$url"
        return 1
        ;;
      pending)
        CI_STATUS_STATE="pending"
        if [ "$wait_mode" = "--wait" ] && [ "$i" -lt "$max_iters" ]; then
          CI_STATUS_FAIL_REASON="$reason"
          sleep "$CI_POLL_INTERVAL"
          continue
        fi
        if [ "$wait_mode" = "--wait" ]; then
          CI_STATUS_FAIL_REASON="CI wait timed out after ${CI_MAX_WAIT_SECONDS}s — $reason"
        else
          CI_STATUS_FAIL_REASON="$reason"
        fi
        return 1
        ;;
      *)
        CI_STATUS_STATE="fail"
        CI_STATUS_FAIL_REASON="unrecognized CI status evaluator response"
        return 1
        ;;
    esac
  done

  CI_STATUS_STATE="pending"
  CI_STATUS_FAIL_REASON="CI wait timed out after ${CI_MAX_WAIT_SECONDS}s — checks still pending"
  return 1
}

# ── Public: check_ci_provenance_gate <pr> <repo> <discussion> ───────────────
# Provenance ordering (AC-15): sits AFTER the D#1588 provenance:external /
# intake-approved human gate. Delegates the classification to
# external_intake_gate.py rather than re-deriving it.
check_ci_provenance_gate() {
  local pr="$1" repo="$2" disc="$3"
  CI_STATUS_FAIL_REASON=""

  local touches_workflows="false"
  local files_mock="CI_PR_FILES_${pr}"
  if [ -n "${!files_mock:-}" ]; then
    if printf '%s\n' "${!files_mock}" | grep -q '^\.github/workflows/'; then
      touches_workflows="true"
    fi
  else
    if gh pr view "$pr" --repo "$repo" --json files --jq '.files[].path' 2>/dev/null \
        | grep -q '^\.github/workflows/'; then
      touches_workflows="true"
    fi
  fi

  if [ "$touches_workflows" != "true" ]; then
    return 0
  fi

  local gate_mock="CI_PROVENANCE_BLOCKED_${disc}"
  if [ -n "${!gate_mock:-}" ]; then
    if [ "${!gate_mock}" = "yes" ]; then
      CI_STATUS_FAIL_REASON="provenance:external PR modifies .github/workflows/** — CI result not honored until the D#1588 intake-approved gate clears (D#$disc)"
      return 1
    fi
    return 0
  fi

  if ! python3 "$_CI_LIB_DIR/external_intake_gate.py" check-discussion "$disc" >/dev/null 2>&1; then
    CI_STATUS_FAIL_REASON="provenance:external PR modifies .github/workflows/** — CI result not honored until the D#1588 intake-approved gate clears (D#$disc)"
    return 1
  fi
  return 0
}

# ── Public: ci_merge_sha_pinned <pr> <repo> <sha> ───────────────────────────
# TOCTOU fix (AC-8): SHA-pin the merge call itself via `gh pr merge
# --match-head-commit`. GitHub's merge API returns 409 (surfaced by gh as a
# non-zero exit + "Head branch was modified" message) if the head moved since
# <sha> was evaluated. Re-checking status before calling merge is NOT
# sufficient on its own — the pin on the merge call closes the race.
ci_merge_sha_pinned() {
  local pr="$1" repo="$2" sha="$3"
  # D#2020: optional 4th arg selects whether --delete-branch is passed.
  # Default "delete" is today's pre-D#2020 behaviour, so any caller that
  # doesn't know about branch dependents (there is currently none) still
  # gets the old three-arg semantics. Pass "keep" when the caller has
  # determined (via pr-dependents.sh) that an open PR is based on this
  # branch.
  local delete_branch_mode="${4:-delete}"
  local -a _merge_flags=(--squash --match-head-commit "$sha")
  if [ "$delete_branch_mode" != "keep" ]; then
    _merge_flags+=(--delete-branch)
  fi

  if [ "${CI_MERGE_MODE:-}" = "echo" ]; then
    echo "CI_MERGE_ARGS: pr=$pr repo=$repo sha=$sha ${_merge_flags[*]}"
    return 0
  fi
  if [ "${CI_MERGE_MODE:-}" = "conflict" ]; then
    CI_STATUS_FAIL_KIND="head-moved"
    CI_STATUS_FAIL_REASON="merge aborted: head moved since CI check (simulated 409, sha=$sha)"
    return 9
  fi

  local out rc=0
  CI_STATUS_FAIL_KIND=""
  out=$(gh pr merge "${_merge_flags[@]}" "$pr" --repo "$repo" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    if printf '%s' "$out" | grep -qiE 'head branch was modified|does not match|match-head-commit'; then
      CI_STATUS_FAIL_KIND="head-moved"
      CI_STATUS_FAIL_REASON="merge aborted: head moved since CI check (sha=$sha no longer current)"
      return 9
    fi
    # Classify so the caller can print a cause-specific remedy. Order matters:
    # a conflict payload and a permissions payload are both "HTTP 4xx" to gh.
    if printf '%s' "$out" | grep -qiE 'not mergeable|merge conflict|conflicting|conflicts with|cannot be merged|is dirty'; then
      CI_STATUS_FAIL_KIND="conflict"
    elif printf '%s' "$out" | grep -qiE 'permission|forbidden|not authorized|resource not accessible|HTTP 40[13]'; then
      CI_STATUS_FAIL_KIND="permissions"
    elif printf '%s' "$out" | grep -qiE 'HTTP [0-9]{3}|graphql|rate limit|could not resolve|connection refused|timeout'; then
      CI_STATUS_FAIL_KIND="api"
    else
      CI_STATUS_FAIL_KIND="unknown"
    fi
    CI_STATUS_FAIL_REASON="merge command failed: $out"
    return 1
  fi
  # Surface gh's own confirmation output (Visibility over silence) — this was
  # captured above only for error-pattern classification, not to swallow it.
  [ -n "$out" ] && printf '%s\n' "$out"
  return 0
}

# ── Public: ci_conflicting_files <base_ref> <head_ref> ──────────────────────
# Best-effort: names the paths that actually conflict between two refs, by
# asking the LOCAL object store via `git merge-tree --write-tree` (git >= 2.40,
# which is when --name-only landed; --write-tree itself dates to 2.38).
#
# GitHub's merge API does not enumerate conflicting paths, and `gh pr view
# --json files` lists the PR's *changed* files, not its conflicting ones -- so
# there is no way to get this from data the wrapper already holds. It has to be
# computed locally, which means it can legitimately be unavailable (refs not
# fetched, not in a repo, old git).
#
# Contract: this function NEVER fails the caller (always returns 0) and NEVER
# guesses. On success CI_CONFLICT_FILES holds one path per line. Otherwise it
# is empty and CI_CONFLICT_FILES_REASON explains why -- callers must print that
# reason. Silently omitting the list, or printing a fabricated one, is a bug.
ci_conflicting_files() {
  local base="$1" head="$2"
  # Resolve refs against the PROJECT repo, not whatever directory the operator
  # happened to run the wrapper from. Third arg is a test seam.
  local repo_dir="${3:-${CI_CONFLICT_REPO_DIR:-$_CI_REPO_ROOT}}"
  CI_CONFLICT_FILES=""
  CI_CONFLICT_FILES_REASON=""

  if ! command -v git >/dev/null 2>&1; then
    CI_CONFLICT_FILES_REASON="git not available on PATH"
    return 0
  fi
  if ! git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1; then
    CI_CONFLICT_FILES_REASON="$repo_dir is not a git repository"
    return 0
  fi

  local base_sha head_sha
  base_sha=$(git -C "$repo_dir" rev-parse --verify --quiet "${base}^{commit}" 2>/dev/null || true)
  if [ -z "$base_sha" ]; then
    base_sha=$(git -C "$repo_dir" rev-parse --verify --quiet "origin/${base}^{commit}" 2>/dev/null || true)
  fi
  if [ -z "$base_sha" ]; then
    CI_CONFLICT_FILES_REASON="base ref '${base}' not fetched locally"
    return 0
  fi
  head_sha=$(git -C "$repo_dir" rev-parse --verify --quiet "${head}^{commit}" 2>/dev/null || true)
  if [ -z "$head_sha" ]; then
    CI_CONFLICT_FILES_REASON="head ref '${head}' not fetched locally"
    return 0
  fi

  local out rc=0
  out=$(git -C "$repo_dir" merge-tree --write-tree --name-only "$base_sha" "$head_sha" 2>/dev/null) || rc=$?
  if [ "$rc" -eq 0 ]; then
    CI_CONFLICT_FILES_REASON="local merge-tree of ${base} and ${head} reports no conflict (local refs may be stale)"
    return 0
  fi
  if [ "$rc" -ne 1 ]; then
    CI_CONFLICT_FILES_REASON="git merge-tree --write-tree --name-only unavailable or failed (rc=$rc; needs git >= 2.40)"
    return 0
  fi

  # Output shape: <tree oid> NL <conflicted paths...> NL blank NL <messages>.
  local files
  files=$(printf '%s\n' "$out" | tail -n +2 | sed -e '/^$/q' | sed -e '/^$/d')
  if [ -z "$files" ]; then
    CI_CONFLICT_FILES_REASON="git merge-tree reported a conflict but named no paths"
    return 0
  fi
  CI_CONFLICT_FILES="$files"
  return 0
}

# ── Public: ci_write_audit <kind> <pr> <head_sha> <failing> <run_url> <reason>
# Durable signal (AC-13) + the audited-bypass trail (AC-12), same shape as
# the existing manual_merge_two_gate_bypass row in merge-and-hook.sh.
ci_write_audit() {
  local kind="$1" pr="$2" head_sha="$3" failing="$4" run_url="$5" reason="$6"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "")
  local entry
  entry=$(python3 -c "
import json, sys
print(json.dumps({
    'kind': sys.argv[1],
    'pr': int(sys.argv[2]) if sys.argv[2].isdigit() else sys.argv[2],
    'head_sha': sys.argv[3],
    'failing_checks': sys.argv[4],
    'run_url': sys.argv[5],
    'reason': sys.argv[6],
    'ts': sys.argv[7],
}))
" "$kind" "$pr" "$head_sha" "$failing" "$run_url" "$reason" "$ts" 2>/dev/null)
  [ -z "$entry" ] && return 0

  printf '%s\n' "$entry" >> "$(_ci_audit_path)" 2>/dev/null || true
}

# ── Public: ci_note_merge_if_unverified <pr> <sha> [audit_already_written] ──
# D#2271 PR-a fallback half of the streak design. Call this once, right
# after a merge has ACTUALLY succeeded (never on a block/refusal path — those
# already `exit`/return before reaching a merge). It writes
# CI_GATE_UNVERIFIED_MERGE_KIND unless either:
#   1. CI_STATUS_STATE is "pass" — the merge WAS verified, and
#      check_ci_status already wrote the positive marker for it, or
#   2. the caller passes audit_already_written="true" — it already wrote
#      its own row explaining the decline (ci_gate_stood_down,
#      manual_merge_ci_bypass, or any future kind).
# A merge that reaches here satisfying neither condition is exactly what
# "declined to gate" means, regardless of what — if anything — the code
# that let it through calls itself. That is the anti-rot property: nothing
# here checks a name, so nothing needs to be added here when a new bypass
# is invented.
ci_note_merge_if_unverified() {
  local pr="$1" sha="$2" audit_already_written="${3:-false}"
  if [ "$CI_STATUS_STATE" = "pass" ]; then
    return 0
  fi
  if [ "$audit_already_written" = "true" ]; then
    return 0
  fi
  ci_write_audit "$CI_GATE_UNVERIFIED_MERGE_KIND" "$pr" "$sha" "" "" \
    "merge proceeded with CI_STATUS_STATE=${CI_STATUS_STATE:-unset} and no other audit row recorded for it"
}

# ── Public: _ci_audit_path ─────────────────────────────────────────────────
# Where audit rows go. Honours $CI_STATUS_TEST_AUDIT_FILE so a test can point
# the whole trail at a tmpfile. Shared with scripts/set-ci-kill-switch.sh so
# the two writers cannot drift onto different files.
_ci_audit_path() {
  if [ -n "${CI_STATUS_TEST_AUDIT_FILE:-}" ]; then
    if [ "${CI_STATUS_TEST_MODE:-}" = "1" ]; then
      printf '%s' "$CI_STATUS_TEST_AUDIT_FILE"
      return 0
    fi
    echo "_ci_audit_path: ignoring CI_STATUS_TEST_AUDIT_FILE — set CI_STATUS_TEST_MODE=1 to honour it" >&2
  fi
  python3 -c "
import sys
sys.path.insert(0, '$_CI_REPO_ROOT')
try:
    from backend.state_paths import AUDIT_LOG
    print(str(AUDIT_LOG))
except Exception:
    print('$_CI_REPO_ROOT/.autonomous-team/audit.jsonl')
" 2>/dev/null || printf '%s' "$_CI_REPO_ROOT/.autonomous-team/audit.jsonl"
}
