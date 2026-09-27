#!/usr/bin/env bash
# scripts/lib/gate1-receipt-check.sh — Gate 1 authorization (D#2566 PR-2).
#
# PR-1 made a caller-authored receipt exist. This file is what makes
# `Gate 1: PASS` unreadable as a pass without one — the whole decision
# table lives here, sourced into scripts/lib/two-gate-check.sh, which
# calls it as registration only (its own marker regex is untouched).
#
# Exposes gate1_receipt_check <pr> <repo>.
#   Sets GATE1_RECEIPT_CHECK_STATE (machine-readable — see the list below)
#   and GATE1_RECEIPT_CHECK_REASON (human-readable, quotable in a NACK).
#   Returns 0 to authorize, 1 to reject.
#
# States: ok | absent | malformed | sha_mismatch | runner_copy_in_tree |
#         partial | unrouted | na_but_routed
#
# TRUST BOUNDARY — normative, see D#2566's Spec.
#   Authorization reads `caller` ONLY, and — security review, this PR —
#   that means every VALUE under `caller` and `head_reported` is validated
#   for shape too, not just the key sets. A key-set match that lets an
#   unvalidated value through is not a trust boundary, it is documentation:
#   `routing[].suite: ""` or `false`, and `partial: "true"` or `1`, both
#   used to slip past a `is None` / `is True` identity check and flip a
#   rejection into an authorization. Anything with an unexpected shape is
#   `malformed`, never interpreted permissively.
#
#   caller fields actually EVALUATED for authorization today: pr_head_sha
#   (sha binding, item 15), gate1_runner_copy + tree_root (item 16, and
#   both must now be non-empty absolute paths — an empty value used to
#   skip the check instead of failing it), pr + repo (cross-checked against
#   the arguments this function was called with, not just trusted as
#   written). caller fields RECORDED but NOT evaluated: gate1_containment,
#   containment_probes, containment_verdict, env, written_at, receipt_path.
#   That is a deliberate, Spec-stated position, not an oversight: under
#   `gate1_containment=NONE (same-uid)` the receipt is honesty, not
#   tamper-evidence (D#2566's Spec, carried from the security-expert's own
#   panel comment), and this file does not invent a containment-based gate
#   the Spec never asked for. If a future PR wants to gate on
#   containment_verdict, that is a new decision to write down, not a
#   silent side effect of this one. `head_reported` (routing, tests_run,
#   partial) is read for REJECTION reasons and for the unrouted/N-A
#   classification below, and — as of this fix — for malformed-shape
#   detection; never to grant a pass. Every reject branch below is
#   reachable from `head_reported` alone; every branch that can return
#   "ok" depends only on `caller` having already checked out clean.
#
# UNROUTED vs N/A (D#2566 items 14, 17, 18; the security-expert's main
# finding). No routing arm in run-pr-tests.sh claims scripts/*.sh outside
# two hand-listed registries (orphan-triage, post-merge-hook), so a PR
# touching scripts/lib/two-gate-check.sh routes suite:null — "routing names
# no suite" would authorize a free N/A on the gate's own file:
#   - every null-routed file must appear in scripts/ci/gate1-routing-
#     ledger.json with a non-empty reason, OR the whole PR is rejected as
#     `unrouted` (never as N/A, never silently as a pass) — item 17. Checked
#     when EVERY routing entry is null; a PR that also ran a real suite for
#     some other file is not affected by THIS particular check — its
#     unrouted, non-`.py` files simply went untested, which is a
#     pre-existing, out-of-scope property of run-pr-tests.sh's routing.
#   - a `.py` file is different: it is NEVER eligible for that ledger, full
#     stop, and this check is NOT gated on all-null — a null-routed `.py`
#     file is rejected as `unrouted` even when the same PR also touches a
#     file that routes to a real suite. Gating it on all-null would be
#     defeated by adding one `backend/` file to the same diff — exactly the
#     PR #123 / PR #200 shape this Spec exists to close, one layer up
#     (security review, this PR). run-pr-tests.sh's pytest arm is
#     `backend/*|tests/*.py|*_test.py|test_*.py` — a `.py` module outside
#     those patterns (e.g. anything under scripts/lib/*.py) routes null and
#     runs nothing, and those are exactly the modules most likely to BE the
#     trust boundary. Ledgering one would mint a permanent, documented free
#     pass for it; "no suite covers this" is a defect statement, not a
#     justification.
#   - if every null-routed file clears the check above AND the PR body
#     declares Gate 1 N/A, that is accepted (item 18).
#   - if the PR is NOT all-null (some real suite ran) and the body declares
#     N/A anyway, that is rejected regardless of ledger coverage — item 14,
#     the exact D#2571 shape.
#
# Test-mode overrides (mirrors two-gate-check.sh's TWO_GATE_PR_BODY_<PR>):
#   GATE1_RECEIPT_PR_BODY_<PR>    PR body text (literal \n for newlines)
#   GATE1_RECEIPT_HEAD_SHA_<PR>   PR's current head sha, skips `gh pr view`
#   GATE1_RECEIPT_JSON_<PR>       the receipt's raw JSON text, skips both
#                                 the state-dir path computation and the
#                                 filesystem read entirely
#   GATE1_RECEIPT_STATE_DIR       overrides AUTONOMOUS_TEAM_STATE_DIR
#                                 resolution for the real (non-JSON-mock)
#                                 path-lookup case

set -uo pipefail

_G1RC_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./gate1-receipt.sh
source "$_G1RC_SCRIPT_DIR/gate1-receipt.sh"

GATE1_RECEIPT_CHECK_REASON=""
GATE1_RECEIPT_CHECK_STATE=""

_g1rc_state_dir() {
  printf '%s' "${GATE1_RECEIPT_STATE_DIR:-${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}}"
}

_g1rc_ledger_path() {
  printf '%s' "${GATE1_RECEIPT_LEDGER_PATH:-$_G1RC_SCRIPT_DIR/../ci/gate1-routing-ledger.json}"
}

_g1rc_pr_body() {
  local pr="$1" repo="$2" mock="GATE1_RECEIPT_PR_BODY_${pr}"
  if [ -n "${!mock:-}" ]; then
    printf '%b' "${!mock}"
    return 0
  fi
  gh pr view "$pr" --repo "$repo" --json body --jq .body 2>/dev/null || true
}

_g1rc_head_sha() {
  local pr="$1" repo="$2" mock="GATE1_RECEIPT_HEAD_SHA_${pr}"
  if [ -n "${!mock:-}" ]; then
    printf '%s' "${!mock}"
    return 0
  fi
  gh pr view "$pr" --repo "$repo" --json headRefOid --jq .headRefOid 2>/dev/null || true
}

# _g1rc_decide RECEIPT_JSON PR HEAD_SHA LEDGER_PATH PR_BODY REPO — the
# receipt's raw JSON text is a positional argument, not stdin: `python3 -`
# reads its OWN program from stdin (that's what the heredoc below
# supplies), so stdin has nothing left for the receipt once the
# interpreter starts — passing it as argv is what actually gets the data
# to the script.
# Prints STATE on line 1, REASON on line 2 (and beyond, if the reason
# itself needs more than one line — callers must not assume exactly two
# lines back).
_g1rc_decide() {
  python3 - "$@" <<'PYEOF'
import json
import re
import sys

raw, pr, head_sha, ledger_path, pr_body, repo = sys.argv[1:7]


def emit(state, reason):
    print(state)
    print(reason)
    sys.exit(0)


try:
    receipt = json.loads(raw)
except Exception as e:
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr} is malformed or unreadable ({e}) "
        "-- never treated as N/A or a pass",
    )

if not isinstance(receipt, dict) or sorted(receipt.keys()) != ["caller", "head_reported", "schema"]:
    got = sorted(receipt.keys()) if isinstance(receipt, dict) else type(receipt).__name__
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr} has top-level keys {got}, "
        "expected exactly caller/head_reported/schema",
    )

caller = receipt.get("caller")
head_reported = receipt.get("head_reported")
if not isinstance(caller, dict) or not isinstance(head_reported, dict):
    emit("malformed", f"Gate 1 receipt for PR #{pr}: caller/head_reported are not both objects")

REQUIRED_CALLER_KEYS = {
    "pr", "repo", "pr_head_sha", "tree_root", "gate1_runner_copy",
    "gate1_containment", "containment_probes", "containment_verdict",
    "env", "written_at", "receipt_path",
}
if sorted(caller.keys()) != sorted(REQUIRED_CALLER_KEYS):
    extra = sorted(set(caller.keys()) - REQUIRED_CALLER_KEYS)
    missing = sorted(REQUIRED_CALLER_KEYS - set(caller.keys()))
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr}: caller key set does not match exactly "
        f"(extra={extra}, missing={missing}) -- a new caller field must be "
        "added to this allowlist deliberately, not discovered by drift",
    )

REQUIRED_HR_KEYS = {"routing", "tests_run", "partial", "measured_tree"}
if sorted(head_reported.keys()) != sorted(REQUIRED_HR_KEYS):
    extra = sorted(set(head_reported.keys()) - REQUIRED_HR_KEYS)
    missing = sorted(REQUIRED_HR_KEYS - set(head_reported.keys()))
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr}: head_reported key set does not match "
        f"exactly (extra={extra}, missing={missing})",
    )

# --- VALUE shape validation, not just key sets (security review, this PR) ---
# A key-set match that lets `partial: "true"` or `routing[].suite: false`
# through is documentation, not a boundary. Every field consulted below is
# validated for shape FIRST; anything unexpected is `malformed`, never
# interpreted permissively toward a pass.

partial = head_reported.get("partial")
if not isinstance(partial, bool):
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr}: head_reported.partial is "
        f"{partial!r} ({type(partial).__name__}), not a real boolean -- "
        "a non-bool value is never treated as false",
    )

routing = head_reported.get("routing")
if not isinstance(routing, list):
    emit(
        "malformed",
        f"Gate 1 receipt for PR #{pr}: head_reported.routing is "
        f"{type(routing).__name__}, not a list",
    )
for _entry in routing:
    if not isinstance(_entry, dict):
        emit(
            "malformed",
            f"Gate 1 receipt for PR #{pr}: a head_reported.routing entry is "
            f"{type(_entry).__name__}, not an object",
        )
    _file = _entry.get("file")
    if not isinstance(_file, str) or not _file:
        emit(
            "malformed",
            f"Gate 1 receipt for PR #{pr}: a head_reported.routing entry has "
            f"no non-empty string 'file' ({_entry.get('file')!r})",
        )
    _suite = _entry.get("suite")
    if _suite is not None and not (isinstance(_suite, str) and _suite.strip()):
        emit(
            "malformed",
            f"Gate 1 receipt for PR #{pr}: head_reported.routing entry for "
            f"{_file} has 'suite'={_suite!r} -- must be null or a non-empty "
            "string, never an empty string, a boolean, or a number",
        )

# --- sha binding (item 15): caller-authored, authorizing ---
receipt_sha = caller.get("pr_head_sha")
if receipt_sha != head_sha:
    emit(
        "sha_mismatch",
        f"Gate 1 receipt's pr_head_sha ({receipt_sha}) does not match PR "
        f"#{pr}'s current head ({head_sha}) -- a receipt only ever "
        "authorizes the exact tree it measured",
    )

# --- caller.pr / caller.repo cross-checked against the arguments this
# function was actually called with (security review, "should fix" #4).
# In production the receipt's filename already binds pr+sha; this closes
# the same check on the GATE1_RECEIPT_JSON_<PR> mock path, where nothing
# else binds them at all.
_receipt_pr = caller.get("pr")
if str(_receipt_pr) != str(pr):
    emit(
        "malformed",
        f"Gate 1 receipt's caller.pr ({_receipt_pr!r}) does not match the "
        f"PR being checked (#{pr})",
    )
_receipt_repo = caller.get("repo")
if _receipt_repo != repo:
    emit(
        "malformed",
        f"Gate 1 receipt's caller.repo ({_receipt_repo!r}) does not match "
        f"the repo being checked ({repo!r})",
    )

# --- runner copy outside the tree under test (item 16): caller-authored.
# Both fields must be non-empty absolute paths -- an empty value used to
# skip this check (falsy tree_root) or pass it (falsy runner_copy)
# instead of failing it (security review, "should fix" #5).
import os.path as _p

runner_copy = caller.get("gate1_runner_copy")
tree_root = caller.get("tree_root")
if not isinstance(runner_copy, str) or not runner_copy.startswith("/"):
    emit(
        "malformed",
        f"Gate 1 receipt's caller.gate1_runner_copy ({runner_copy!r}) is not "
        "a non-empty absolute path -- item 16's check cannot be skipped by "
        "leaving this blank",
    )
if not isinstance(tree_root, str) or not tree_root.startswith("/"):
    emit(
        "malformed",
        f"Gate 1 receipt's caller.tree_root ({tree_root!r}) is not a "
        "non-empty absolute path -- item 16's check cannot be skipped by "
        "leaving this blank",
    )
rc_resolved = _p.realpath(runner_copy)
tr_resolved = _p.realpath(tree_root)
if rc_resolved == tr_resolved or rc_resolved.startswith(tr_resolved.rstrip("/") + "/"):
    emit(
        "runner_copy_in_tree",
        f"Gate 1 receipt's gate1_runner_copy ({runner_copy}) resolves inside "
        f"tree_root ({tree_root}) -- the runner copy that ran was the "
        "tree's own, not the caller's",
    )

# --- partial run (item 21): head-influenced, but reportable as a rejection.
# Shape already validated above (real bool); this is the value check.
if partial is True:
    emit(
        "partial",
        "Gate 1 receipt's head_reported.partial is true -- the run was "
        "killed before completing and cannot authorize a pass",
    )

# --- .py trust-module rule (D#2577, sharpened by security review on this
# PR): applies to EVERY null-routed .py file, independent of what its
# siblings in the same routing list route to. Gating this on "every file
# in the PR routes null" would be defeated by adding one backend/ file to
# the same diff -- the exact PR #123 / PR #200 shape one layer up. Checked
# BEFORE the all-null ledger check below, so a .py file never needs (and
# never gets) ledger cover regardless of its neighbors.
for _entry in routing:
    if _entry.get("suite") is None and _entry.get("file", "").endswith(".py"):
        emit(
            "unrouted",
            f"Gate 1: {_entry.get('file')} is a .py module routing to no "
            "suite -- never eligible for scripts/ci/gate1-routing-"
            "ledger.json, and an unrelated file in the same PR routing to "
            "a real suite does not change that",
        )

# --- unrouted vs N/A, non-.py files (items 14, 17, 18) ---
try:
    with open(ledger_path) as f:
        ledger = json.load(f).get("ledger", {})
    if not isinstance(ledger, dict):
        ledger = {}
except Exception:
    ledger = {}

null_entries = [r for r in routing if r.get("suite") is None]
real_entries = [r for r in routing if r.get("suite") is not None]
all_null = len(real_entries) == 0

if all_null and null_entries:
    unledgered = []
    for r in null_entries:
        path = r.get("file", "")
        reason = ledger.get(path)
        if not reason or not str(reason).strip():
            unledgered.append(path)
    if unledgered:
        emit(
            "unrouted",
            f"Gate 1: {unledgered[0]} routes to no suite and is not covered "
            "by scripts/ci/gate1-routing-ledger.json -- unrouted status "
            "never authorizes a merge, whatever the PR body claims",
        )

gate1_line = re.search(
    r"Gate[ \t]*1[^:]*:[ \t]*(PASS(ED)?|N/A|✓)", pr_body, re.IGNORECASE
)
declares_na = bool(gate1_line and gate1_line.group(1).upper().startswith("N/A"))

if not all_null and declares_na:
    r0 = real_entries[0]
    emit(
        "na_but_routed",
        f"Gate 1 declared N/A but routing names suite '{r0.get('suite')}' "
        f"for {r0.get('file')} -- N/A requires every changed file to route "
        "to no suite",
    )

emit("ok", "authorized")
PYEOF
}

gate1_receipt_check() {
  GATE1_RECEIPT_CHECK_REASON=""
  GATE1_RECEIPT_CHECK_STATE=""
  local pr="$1" repo="$2"

  local head_sha body
  head_sha="$(_g1rc_head_sha "$pr" "$repo")"
  body="$(_g1rc_pr_body "$pr" "$repo")"

  if [ -z "$head_sha" ]; then
    GATE1_RECEIPT_CHECK_STATE="absent"
    GATE1_RECEIPT_CHECK_REASON="could not resolve PR #$pr's current head sha -- cannot locate a receipt without it"
    return 1
  fi

  local receipt_json="" mock_json_var="GATE1_RECEIPT_JSON_${pr}"
  if [ -n "${!mock_json_var:-}" ]; then
    receipt_json="${!mock_json_var}"
  else
    # Validate the sha BEFORE it becomes a path segment on the read side
    # too (security review, "should fix" #6) — gate1_receipt_path's own
    # docstring says callers must call gate1_receipt_validate_sha first;
    # the write side (gate1-invoke.sh) already does, the read side did
    # not. head_sha comes from `gh pr view --json headRefOid` today, so
    # this is defense-in-depth rather than a live traversal, same as the
    # write side's own comment says.
    if ! gate1_receipt_validate_sha "$head_sha"; then
      GATE1_RECEIPT_CHECK_STATE="absent"
      GATE1_RECEIPT_CHECK_REASON="PR #$pr's head sha ($head_sha) does not match ^[0-9a-f]{40}\$ -- refusing to use it as a path segment"
      return 1
    fi
    local state_dir receipt_path
    state_dir="$(_g1rc_state_dir)"
    receipt_path="$(gate1_receipt_path "$state_dir" "$repo" "$pr" "$head_sha" 2>/dev/null || true)"
    if [ -n "$receipt_path" ] && [ -f "$receipt_path" ]; then
      receipt_json="$(cat "$receipt_path")"
    fi
  fi

  if [ -z "$receipt_json" ]; then
    GATE1_RECEIPT_CHECK_STATE="absent"
    GATE1_RECEIPT_CHECK_REASON="Gate 1 receipt absent for PR #$pr -- run: bash /abs/path/to/operator/checkout/scripts/gate1-invoke.sh --pr $pr --tree /abs/path/to/pr-head/tree"
    return 1
  fi

  local ledger_path result state reason
  ledger_path="$(_g1rc_ledger_path)"
  result="$(_g1rc_decide "$receipt_json" "$pr" "$head_sha" "$ledger_path" "$body" "$repo")"
  state="$(printf '%s\n' "$result" | sed -n '1p')"
  reason="$(printf '%s\n' "$result" | sed -n '2,$p')"

  GATE1_RECEIPT_CHECK_STATE="$state"
  GATE1_RECEIPT_CHECK_REASON="$reason"
  [ "$state" = "ok" ] && return 0
  return 1
}
