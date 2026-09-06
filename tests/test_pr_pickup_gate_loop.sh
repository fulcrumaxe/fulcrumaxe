#!/usr/bin/env bash
# tests/test_pr_pickup_gate_loop.sh — D#2404: what the loop DOES with a PR from
# outside the trust set. Extended by D#2421 PR 2: what the loop does with an
# external PR whose head moved after a trusted maintainer approved it.
#
# This drives the real pickup path — scripts/lib/pr-pickup-gate.sh's
# classify_open_prs, which is the body of team-lead-iteration.sh Step 4 — with
# the real gate (scripts/lib/pr_intake_gate.py) behind a stubbed `gh`. Only the
# network boundary is faked.
#
# Why the four arrays are the right thing to assert on (D#2377 says assert on
# the loop, not on a helper's return value): NEEDS_REVIEW, NEEDS_MERGE,
# NEEDS_FIX and NEEDS_SECURITY_REVIEW are the *only* inputs to everything
# downstream in that script — Step 5 prints one spawn recommendation per entry
# in NEEDS_REVIEW/NEEDS_FIX/NEEDS_SECURITY_REVIEW, and Step 5.3 iterates
# NEEDS_MERGE and writes labels + a comment to each PR in it. A PR absent from
# all four is a PR nothing spawns on and nothing labels. The call log is
# checked as well, so "no label applied" is observed and not merely inferred.
#
# Run: bash tests/test_pr_pickup_gate_loop.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

FIXTURE_ROOT=$(mktemp -d)
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

# D#2421 AC-5: the PR head-baseline store lives under AUTONOMOUS_TEAM_STATE_DIR.
# Planting a distinctive token in that path lets the reason/error hygiene
# assertions below prove the token never reaches a caller-visible string,
# rather than merely asserting "looks fine".
STATE_TOKEN="ZZ_STATE_DIR_TOKEN_9f3c1a_ZZ"
export AUTONOMOUS_TEAM_STATE_DIR="$FIXTURE_ROOT/$STATE_TOKEN/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR" "$FIXTURE_ROOT/bin"

# The single login this fixture treats as ours. Overriding it makes every
# assertion below independent of the checkout's real config.
export AUTONOMOUS_TEAM_BOT_ACCOUNT="fixture-bot"

export GH_CALL_LOG="$FIXTURE_ROOT/gh-calls.log"
: > "$GH_CALL_LOG"

# ── PR fixtures ─────────────────────────────────────────────────────────────
# 1xx = ours (the case that runs daily). 2xx = a stranger's (D#2404).
# 3xx = a stranger's, approved, head-baseline tracked (D#2421).
# PR numbers absent from this file (4xx/5xx below) exercise fail-closed paths.
export PR_FIXTURES="$FIXTURE_ROOT/prs.json"
cat > "$PR_FIXTURES" <<'JSON'
{
  "101": {"author": "fixture-bot", "labels": [], "head_sha": "sha-101"},
  "102": {"author": "fixture-bot", "labels": ["code-review-passed"], "head_sha": "sha-102"},
  "103": {"author": "fixture-bot", "labels": ["code-review-needs-fix"], "head_sha": "sha-103"},
  "104": {"author": "fixture-bot", "labels": ["code-review-passed", "security-review-triggered"], "head_sha": "sha-104"},

  "201": {"author": "drive-by-stranger", "labels": [], "head_sha": "sha-201"},
  "202": {"author": "drive-by-stranger", "labels": ["provenance:internal", "code-review-passed"], "head_sha": "sha-202"},
  "203": {"author": "drive-by-stranger", "labels": ["intake-approved"], "head_sha": "sha-203",
          "events": [{"event": "labeled", "created_at": "2026-09-05T10:00:00Z",
                      "label": {"name": "intake-approved"},
                      "actor": {"login": "drive-by-stranger"}}]},
  "204": {"author": "drive-by-stranger", "labels": ["intake-approved"], "head_sha": "sha-204-v1",
          "events": [{"event": "labeled", "created_at": "2026-09-05T10:00:00Z",
                      "label": {"name": "intake-approved"},
                      "actor": {"login": "fixture-bot"}}]},

  "301": {"author": "drive-by-stranger", "labels": ["intake-approved"], "head_sha": "sha-301-drifted",
          "events": [{"event": "labeled", "created_at": "2026-09-05T10:00:00Z",
                      "label": {"name": "intake-approved"},
                      "actor": {"login": "fixture-bot"}}]},
  "302": {"author": "drive-by-stranger", "labels": ["intake-approved"], "head_sha": "sha-302-v1",
          "events": [{"event": "labeled", "created_at": "2026-09-05T10:00:00Z",
                      "label": {"name": "intake-approved"},
                      "actor": {"login": "fixture-bot"}}]}
}
JSON

OPEN_PRS='[
  {"number":101,"title":"ours, unreviewed","labels":[]},
  {"number":102,"title":"ours, reviewed","labels":[{"name":"code-review-passed"}]},
  {"number":103,"title":"ours, needs fix","labels":[{"name":"code-review-needs-fix"}]},
  {"number":104,"title":"ours, security pending","labels":[{"name":"code-review-passed"},{"name":"security-review-triggered"}]},
  {"number":201,"title":"stranger, plain","labels":[]},
  {"number":202,"title":"stranger, self-labelled","labels":[{"name":"provenance:internal"},{"name":"code-review-passed"}]},
  {"number":203,"title":"stranger, self-approved","labels":[{"name":"intake-approved"}]},
  {"number":204,"title":"stranger, human-approved","labels":[{"name":"intake-approved"}]}
]'

# ── gh stub ─────────────────────────────────────────────────────────────────
# PR numbers 501/502 are not in $PR_FIXTURES — they force gh itself to
# misbehave (non-zero exit / unparseable JSON) for D#2421 AC-6, ahead of the
# generic fixture lookup below.
cat > "$FIXTURE_ROOT/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
args="$*"
echo "$args" >> "$GH_CALL_LOG"

if echo "$args" | grep -q "collaborators"; then
  echo '[]'
  exit 0
fi

num=$(echo "$args" | grep -oE '(pulls|issues)/[0-9]+' | head -1 | grep -oE '[0-9]+')

if [ "$num" = "501" ]; then
  echo "gh: rate limited (simulated)" >&2
  exit 1
fi
if [ "$num" = "502" ]; then
  echo 'not-valid-json{{{'
  exit 0
fi

kind=other
echo "$args" | grep -qE 'issues/[0-9]+/events' && kind=events
echo "$args" | grep -qE 'pulls/[0-9]+$' && kind=pr

python3 - "$PR_FIXTURES" "${num:-0}" "$kind" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
pr = data.get(sys.argv[2])
if pr is None:
    print("[]")
elif sys.argv[3] == "pr":
    print(json.dumps({
        "user": {"login": pr["author"], "id": 1},
        "labels": [{"name": n} for n in pr.get("labels", [])],
        "head": {"sha": pr.get("head_sha")},
    }))
elif sys.argv[3] == "events":
    print(json.dumps(pr.get("events", [])))
else:
    print("[]")
PY
GHEOF
chmod +x "$FIXTURE_ROOT/bin/gh"
export PATH="$FIXTURE_ROOT/bin:$PATH"

# ── Run the real pickup path ────────────────────────────────────────────────
# shellcheck source=../scripts/lib/pr-pickup-gate.sh
source "$REPO_ROOT/scripts/lib/pr-pickup-gate.sh"
GATE_OUTPUT=$(classify_open_prs "$OPEN_PRS" 2>&1)
# classify_open_prs sets the arrays in this shell; re-run capturing output
# above would lose them, so run it again for the array state.
classify_open_prs "$OPEN_PRS" >/dev/null 2>&1

echo "=== test_pr_pickup_gate_loop.sh ==="

_in_array() {
  local needle="$1"; shift
  local entry
  for entry in "$@"; do
    [ "${entry%%:*}" = "$needle" ] && return 0
  done
  return 1
}

_picked_up() {
  local pr="$1"
  _in_array "$pr" "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}" && return 0
  _in_array "$pr" "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}" && return 0
  _in_array "$pr" "${NEEDS_FIX[@]+"${NEEDS_FIX[@]}"}" && return 0
  _in_array "$pr" "${NEEDS_SECURITY_REVIEW[@]+"${NEEDS_SECURITY_REVIEW[@]}"}" && return 0
  return 1
}

# ── AC1 — a stranger's PR is not picked up (D#2404) ─────────────────────────
for pr in 201 202 203; do
  if _picked_up "$pr"; then
    fail "AC1: PR #$pr (untrusted author) reached a work array — an agent would be spawned on it"
  else
    pass "AC1: PR #$pr (untrusted author) is not picked up"
  fi
done

if _in_array 201 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "AC1: gated PRs are recorded for a human to see"
else
  fail "AC1: PR #201 missing from GATED_PRS"
fi

# AC1, the label half — observed, not inferred. Nothing in the pickup path may
# mutate a label; the only writes in Step 4/5.3 are the quality gate's, and a
# gated PR never reaches it.
if grep -qE '(-X POST|-X DELETE|pr edit|--add-label|pr comment)' "$GH_CALL_LOG"; then
  fail "AC1: the pickup path issued a mutating gh call: $(grep -m1 -E '(-X POST|-X DELETE|pr edit|--add-label|pr comment)' "$GH_CALL_LOG")"
else
  pass "AC1: no label or comment was written to any PR during pickup"
fi

# ── AC2 — the no-op direction: our own PRs classify exactly as before ────────
if _in_array 101 "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}"; then
  pass "AC2: PR #101 (ours, unreviewed) -> NEEDS_REVIEW"
else
  fail "AC2: PR #101 did not reach NEEDS_REVIEW"
fi
if _in_array 102 "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}"; then
  pass "AC2: PR #102 (ours, reviewed) -> NEEDS_MERGE"
else
  fail "AC2: PR #102 did not reach NEEDS_MERGE"
fi
if _in_array 103 "${NEEDS_FIX[@]+"${NEEDS_FIX[@]}"}"; then
  pass "AC2: PR #103 (ours, needs-fix) -> NEEDS_FIX"
else
  fail "AC2: PR #103 did not reach NEEDS_FIX"
fi
if _in_array 104 "${NEEDS_SECURITY_REVIEW[@]+"${NEEDS_SECURITY_REVIEW[@]}"}"; then
  pass "AC2: PR #104 (ours, security triggered) -> NEEDS_SECURITY_REVIEW"
else
  fail "AC2: PR #104 did not reach NEEDS_SECURITY_REVIEW"
fi

# ── AC3 — live identity, not labels (D#2404) ────────────────────────────────
# #202 carries provenance:internal AND code-review-passed. Before the gate it
# would have landed in NEEDS_MERGE, where Step 5.3 writes labels and a comment.
if _in_array 202 "${NEEDS_MERGE[@]+"${NEEDS_MERGE[@]}"}"; then
  fail "AC3: a provenance:internal label on a stranger's PR conferred trust"
else
  pass "AC3: provenance:internal label does not confer trust"
fi
if echo "$GATE_OUTPUT" | grep -q "PR #203 gated"; then
  pass "AC3: intake-approved applied by the PR author is not an approval"
else
  fail "AC3: PR #203 (self-approved) was not gated"
fi

# ── AC4 — human-approved external PR flows, and forces security review ──────
if _in_array 204 "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}"; then
  pass "AC4: PR #204 (approved by a maintainer) flows into the normal path"
else
  fail "AC4: PR #204 was blocked despite a maintainer's intake-approved"
fi

python3 "$REPO_ROOT/scripts/lib/pr_intake_gate.py" security-required-pr 204 >/dev/null 2>&1
rc_external=$?
python3 "$REPO_ROOT/scripts/lib/pr_intake_gate.py" security-required-pr 101 >/dev/null 2>&1
rc_internal=$?
# Exit codes are the contract loop-phased-step5.sh's _pr_author_forces_security
# branches on: 0 = required, 1 = confirmed not required.
if [ "$rc_external" -eq 0 ]; then
  pass "AC4: security-review-passed is a hard merge requirement for the external PR (rc=0)"
else
  fail "AC4: expected rc=0 (required) for external PR #204, got $rc_external"
fi
if [ "$rc_internal" -eq 1 ]; then
  pass "AC4: an internal PR gains no extra security requirement (rc=1)"
else
  fail "AC4: expected rc=1 (not required) for internal PR #101, got $rc_internal"
fi

# ── Fail-closed — a PR the gate cannot read is not picked up (D#2404) ────────
UNKNOWN_PRS='[{"number":999,"title":"not in the fixture","labels":[]}]'
classify_open_prs "$UNKNOWN_PRS" >/dev/null 2>&1
if _picked_up 999; then
  fail "fail-closed: a PR whose author could not be read was picked up anyway"
else
  pass "fail-closed: a PR whose author could not be read is not picked up"
fi

# ═════════════════════════════════════════════════════════════════════════
# D#2421 — head-SHA baseline: a drifted external PR is dropped, an undrifted
# one flows, and the transition between the two is observed within one test.
# ═════════════════════════════════════════════════════════════════════════

# ── D#2421 AC-1 — a drifted external PR is dropped from all four arrays ─────
# PR #301 is approved by a trusted actor, but its stored baseline (seeded
# below to a DIFFERENT sha than the fixture's current head) records a head
# that was live at some earlier approval, not the one gh now reports.
python3 "$REPO_ROOT/scripts/lib/pr_intake_gate.py" rebaseline-pr 301 >/dev/null 2>&1
# Rebaseline recorded whatever head_sha the fixture holds *right now*
# ("sha-301-drifted"). Force a real drift: overwrite the store's row so the
# recorded baseline is a DIFFERENT sha than the fixture's current head,
# simulating "approved at A, force-pushed to B" without a second gh round-trip.
python3 - "$REPO_ROOT" <<'PY'
import sys
sys.path.insert(0, f"{sys.argv[1]}/scripts/lib")
import pr_head_baseline
from backend._repo import CODE_REPO  # noqa: E402
key = pr_head_baseline.pr_key(CODE_REPO, 301)
pr_head_baseline.rebaseline(key, "sha-301-approved-at", path=None)
PY

GATE_OUTPUT_301=$(classify_open_prs '[{"number":301,"title":"stranger, drifted after approval","labels":["intake-approved"]}]' 2>&1)
classify_open_prs '[{"number":301,"title":"stranger, drifted after approval","labels":["intake-approved"]}]' >/dev/null 2>&1

if _picked_up 301; then
  fail "D#2421 AC-1: PR #301 (drifted after approval) reached a work array"
else
  pass "D#2421 AC-1: PR #301 (drifted after approval) is not picked up"
fi
if _in_array 301 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "D#2421 AC-1: PR #301 recorded in GATED_PRS"
else
  fail "D#2421 AC-1: PR #301 missing from GATED_PRS"
fi
if echo "$GATE_OUTPUT_301" | grep -q "external_pr_head_changed_after_approval"; then
  pass "D#2421 AC-1: reason is external_pr_head_changed_after_approval"
else
  fail "D#2421 AC-1: expected reason external_pr_head_changed_after_approval, got: $GATE_OUTPUT_301"
fi

# ── D#2421 AC-2 / AC-3 — the undrifted case flows, and the head actually
#    moving is what flips it, observed within one test ──────────────────────
# Seed PR #302's baseline to match its current fixture head_sha exactly.
python3 "$REPO_ROOT/scripts/lib/pr_intake_gate.py" rebaseline-pr 302 >/dev/null 2>&1

OPEN_302='[{"number":302,"title":"stranger, approved and unmoved","labels":["intake-approved"]}]'
classify_open_prs "$OPEN_302" >/dev/null 2>&1
if _picked_up 302 && ! _in_array 302 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "D#2421 AC-2: PR #302 (undrifted) flows into the normal path, run 1"
else
  fail "D#2421 AC-2: PR #302 (undrifted) was gated on the first run"
fi

# Change ONLY the stubbed head.sha — same PR, same store — and re-run.
python3 - "$PR_FIXTURES" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["302"]["head_sha"] = "sha-302-v2-force-pushed"
json.dump(data, open(path, "w"))
PY

classify_open_prs "$OPEN_302" >/dev/null 2>&1
if _picked_up 302; then
  fail "D#2421 AC-3: PR #302 was still picked up after its head changed"
else
  pass "D#2421 AC-3: PR #302 (now drifted) is dropped, run 2 — same store, only the head changed"
fi
if _in_array 302 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "D#2421 AC-3: PR #302 recorded in GATED_PRS after drifting"
else
  fail "D#2421 AC-3: PR #302 missing from GATED_PRS after drifting"
fi

# ── D#2421 AC-5 — reason travels, error does not ────────────────────────────
# The state-dir token planted in AUTONOMOUS_TEAM_STATE_DIR must never appear
# in anything the shell caller surfaces, across every blocked outcome above
# plus the two failure-mode verdicts below (pr_meta_unreadable via a PR
# absent from the fixture, and trust_set_unresolvable via a direct call).
if echo "$GATE_OUTPUT" "$GATE_OUTPUT_301" | grep -q "$STATE_TOKEN"; then
  fail "D#2421 AC-5: the state-dir token leaked into caller-visible gate output"
else
  pass "D#2421 AC-5: no state-dir token in any caller-visible gate output"
fi

# pr_meta_unreadable — PR #502's gh stub emits unparseable JSON for the
# pulls/{pr} lookup (also driving AC-6 below); fetch_pr_meta's own
# json.loads() catches that and reports pr_meta_unreadable. Reusing it here
# (rather than a PR absent from $PR_FIXTURES) keeps this fixture inside
# fetch_pr_meta's actual try/except boundary — a raw non-dict JSON value
# (e.g. gh answering "[]") reaches an unguarded raw.get("user") a few lines
# below that boundary and raises past it, which is a real fragility but not
# the one this acceptance item is about; that gap is out of scope for this
# port (check_pr's verdict logic is explicitly not modified here).
FAILCLOSED_502='[{"number":502,"title":"gh emits garbage","labels":[]}]'
GATE_OUTPUT_502=$(classify_open_prs "$FAILCLOSED_502" 2>&1)
classify_open_prs "$FAILCLOSED_502" >/dev/null 2>&1
if echo "$GATE_OUTPUT_502" | grep -q "pr_meta_unreadable"; then
  pass "D#2421 AC-5: PR #502's reason is pr_meta_unreadable"
else
  fail "D#2421 AC-5: expected pr_meta_unreadable for PR #502, got: $GATE_OUTPUT_502"
fi
if echo "$GATE_OUTPUT_502" | grep -q "$STATE_TOKEN"; then
  fail "D#2421 AC-5: state-dir token leaked in the pr_meta_unreadable path"
else
  pass "D#2421 AC-5: no state-dir token in the pr_meta_unreadable path"
fi

# trust_set_unresolvable — forced directly against check_pr(), the only way
# to make resolve_allowlist() itself raise without corrupting the real
# .autonomous-team/config.json this checkout reads. Confirms the same
# reason/error split holds for this verdict too: `reason` is the fixed
# constant, and only `error` (never read by any shell caller) may carry the
# exception text.
TRUST_UNRESOLVABLE_JSON=$(python3 - "$REPO_ROOT" "$STATE_TOKEN" <<'PY'
import json, sys
sys.path.insert(0, f"{sys.argv[1]}/scripts/lib")
import pr_intake_gate

def _boom():
    raise RuntimeError(f"simulated allowlist failure touching {sys.argv[2]}/config.json")

pr_intake_gate.resolve_allowlist = _boom
result = pr_intake_gate.check_pr(9999, "fixture-owner/fixture-repo")
print(json.dumps(result))
PY
)
if echo "$TRUST_UNRESOLVABLE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('reason')=='trust_set_unresolvable' else 1)" 2>/dev/null; then
  pass "D#2421 AC-5: trust_set_unresolvable is the reason when resolve_allowlist() raises"
else
  fail "D#2421 AC-5: expected reason=trust_set_unresolvable, got: $TRUST_UNRESOLVABLE_JSON"
fi
TRUST_UNRESOLVABLE_REASON=$(echo "$TRUST_UNRESOLVABLE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null)
if echo "$TRUST_UNRESOLVABLE_REASON" | grep -q "$STATE_TOKEN"; then
  fail "D#2421 AC-5: state-dir token leaked into the trust_set_unresolvable reason"
else
  pass "D#2421 AC-5: no state-dir token in the trust_set_unresolvable reason (it lives only in 'error')"
fi

# ── D#2421 AC-6 — fail closed end to end, through the shell caller ──────────
# PR #501: gh itself exits non-zero on the pulls/{pr} call.
FAILCLOSED_501='[{"number":501,"title":"gh exits non-zero","labels":[]}]'
classify_open_prs "$FAILCLOSED_501" >/dev/null 2>&1
if _picked_up 501; then
  fail "D#2421 AC-6: PR #501 was picked up despite gh exiting non-zero"
else
  pass "D#2421 AC-6: PR #501 (gh exits non-zero) is dropped"
fi
if _in_array 501 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "D#2421 AC-6: PR #501 recorded in GATED_PRS"
else
  fail "D#2421 AC-6: PR #501 missing from GATED_PRS"
fi

# PR #502: gh exits 0 but emits unparseable JSON. Re-run for fresh array
# state — the PR #501 call just above already overwrote GATED_PRS/etc. from
# the AC-5 run above this one; classify_open_prs sets its arrays in the
# calling shell, so only the most recent call's state is live.
classify_open_prs "$FAILCLOSED_502" >/dev/null 2>&1
if _picked_up 502; then
  fail "D#2421 AC-6: PR #502 was picked up despite gh emitting unparseable JSON"
else
  pass "D#2421 AC-6: PR #502 (unparseable JSON) is dropped"
fi
if _in_array 502 "${GATED_PRS[@]+"${GATED_PRS[@]}"}"; then
  pass "D#2421 AC-6: PR #502 recorded in GATED_PRS"
else
  fail "D#2421 AC-6: PR #502 missing from GATED_PRS"
fi

# ── D#2421 AC-10 — internal PRs pay nothing: no read or write against the
#    PR head store ──────────────────────────────────────────────────────────
# Point AUTONOMOUS_TEAM_STATE_DIR at a scratch dir where the exact file the
# baseline store would open is pre-created AS A DIRECTORY — any attempted
# open() (read or write) raises. An internal PR's code path never reaches
# that call at all (provenance is decided before the baseline seam), so this
# proves the "pays nothing" property by making a touch fatal rather than
# merely absent-by-observation.
AC10_STATE_DIR="$FIXTURE_ROOT/ac10-state"
mkdir -p "$AC10_STATE_DIR"
mkdir -p "$AC10_STATE_DIR/pr-head-baselines.json"   # a directory, not a file

_OLD_STATE_DIR="$AUTONOMOUS_TEAM_STATE_DIR"
export AUTONOMOUS_TEAM_STATE_DIR="$AC10_STATE_DIR"

INTERNAL_ONLY='[{"number":101,"title":"ours, unreviewed","labels":[]}]'
classify_open_prs "$INTERNAL_ONLY" >/dev/null 2>&1
AC10_RC=$?

export AUTONOMOUS_TEAM_STATE_DIR="$_OLD_STATE_DIR"

if [ "$AC10_RC" -eq 0 ] && _in_array 101 "${NEEDS_REVIEW[@]+"${NEEDS_REVIEW[@]}"}"; then
  pass "D#2421 AC-10: internal PR #101 admitted normally with the PR-head store trapped"
else
  fail "D#2421 AC-10: internal PR #101 was not admitted (rc=$AC10_RC) — the trapped store may have been touched"
fi

# ── D#2421 AC-7 — the docstring stops claiming a verified head ─────────────
BASELINE_DOCSTRING=$(python3 -c "import ast; print(ast.get_docstring(ast.parse(open('$REPO_ROOT/scripts/lib/pr_head_baseline.py').read())))")
if echo "$BASELINE_DOCSTRING" | grep -qi "bounded.race"; then
  pass "D#2421 AC-7: pr_head_baseline.py docstring names the binding a bounded race"
else
  fail "D#2421 AC-7: docstring does not describe the binding as a bounded race"
fi
if echo "$BASELINE_DOCSTRING" | grep -qi "PR 3"; then
  pass "D#2421 AC-7: docstring points at PR 3 for closing the race"
else
  fail "D#2421 AC-7: docstring does not point at PR 3"
fi
if echo "$BASELINE_DOCSTRING" | grep -qiE "SHA that was actually approved"; then
  fail "D#2421 AC-7: docstring still asserts the recorded head 'was actually approved'"
else
  pass "D#2421 AC-7: docstring no longer asserts the recorded head was actually approved"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "FAILED tests:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
echo "All tests passed."
exit 0
