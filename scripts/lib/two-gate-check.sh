#!/usr/bin/env bash
# scripts/lib/two-gate-check.sh — Two-Gate marker check for PR bodies.
#
# Exposes check_two_gate_markers <pr_number> <repo>.
#   Returns 0 if the PR body contains both Gate 1 and Gate 2 markers.
#   Returns 1 if either marker is absent or the N/A escape hatch is used
#     without a justification.
#
# On failure, sets TWO_GATE_FAIL_REASON to a human-readable string naming
# the first failing condition. Caller uses this in the NACK comment.
#
# Regex rules (case-insensitive grep -iE):
#   Gate 1: line matching Gate[space]*1.*(PASS|PASSED|N/A|✓)
#   Gate 2: line matching Gate[space]*2.*(PASS|PASSED|N/A|✓)
#   Accepted pass-equivalents: PASS, PASSED (case-insensitive), ✓
#   N/A justification: when Gate 2 line contains N/A, the same line must
#     contain non-empty text after N/A (e.g., "N/A — wiki-only") OR the
#     immediately following non-empty line must not start with another Gate
#     token.  Bare "Gate 2 N/A" with nothing after is rejected.
#   Rejected: bare counts like "12 passed", percentages like "86%" — no token.
#
# Test-mode override: TWO_GATE_PR_BODY_<PR> env var (with newlines encoded
# as the literal string \n) supplies the body without a gh API call.
# Example:
#   TWO_GATE_PR_BODY_99999="Gate 1: PASS\nGate 2: PASS"
#   check_two_gate_markers 99999 "owner/repo"

TWO_GATE_FAIL_REASON=""

check_two_gate_markers() {
  local pr="$1"
  local repo="${2:-}"
  TWO_GATE_FAIL_REASON=""

  # Fetch PR body — test-mode override or real gh call.
  local body
  local mock_var="TWO_GATE_PR_BODY_${pr}"
  if [ -n "${!mock_var:-}" ]; then
    # Decode literal \n sequences used in test env vars.
    body=$(printf '%b' "${!mock_var}")
  else
    body=$(gh pr view "$pr" --repo "$repo" --json body --jq .body 2>/dev/null || echo "")
  fi

  if [ -z "$body" ]; then
    TWO_GATE_FAIL_REASON="could not fetch PR body (pr=$pr)"
    return 1
  fi

  # Check Gate 1 presence.
  # Regex requires the recognized token (PASS, PASSED, N/A, ✓) to appear as the
  # first non-space token after the colon — prevents "12 passed" or "86%" from matching.
  if ! echo "$body" | grep -iqE 'Gate[[:space:]]*1[^:]*:[[:space:]]*(PASS(ED)?|N/A|✓)'; then
    TWO_GATE_FAIL_REASON="Gate 1 marker missing — add 'Gate 1: PASS' or 'Gate 1: N/A — <reason>' to the PR body Verification block"
    return 1
  fi

  # Check Gate 2 presence.
  if ! echo "$body" | grep -iqE 'Gate[[:space:]]*2[^:]*:[[:space:]]*(PASS(ED)?|N/A|✓)'; then
    TWO_GATE_FAIL_REASON="Gate 2 marker missing — add 'Gate 2: PASS' or 'Gate 2: N/A — <reason>' to the PR body Verification block"
    return 1
  fi

  # N/A escape-hatch validation for Gate 2.
  # If Gate 2 line uses N/A, require justification.
  local gate2_line
  gate2_line=$(echo "$body" | grep -iE 'Gate[[:space:]]*2.*N/A' | head -1)
  if [ -n "$gate2_line" ]; then
    # Check whether the text after "N/A" on the same line is non-empty.
    # Strip "Gate 2" prefix and "N/A" itself; anything left (trimmed) is justification.
    local after_na
    after_na=$(echo "$gate2_line" | sed -E 's/.*[Nn]\/[Aa][[:space:]]*//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
    if [ -z "$after_na" ]; then
      # Same-line justification absent — check the immediately following non-empty line.
      local found_justification=false
      local in_gate2=false
      while IFS= read -r line; do
        if echo "$line" | grep -iqE 'Gate[[:space:]]*2.*N/A'; then
          in_gate2=true
          continue
        fi
        if [ "$in_gate2" = "true" ]; then
          # Skip blank lines between Gate 2 and the justification.
          local stripped
          stripped=$(echo "$line" | sed 's/^[[:space:]]*//')
          if [ -z "$stripped" ]; then
            continue
          fi
          # If next non-empty line starts with a Gate token, no justification.
          if echo "$stripped" | grep -iqE '^Gate[[:space:]]*[0-9]'; then
            break
          fi
          # Otherwise, this line is the justification.
          found_justification=true
          break
        fi
      done <<< "$body"

      if [ "$found_justification" = "false" ]; then
        TWO_GATE_FAIL_REASON="Gate 2 N/A requires a justification — use 'Gate 2: N/A — <reason>' or add a justification line immediately after"
        return 1
      fi
    fi
  fi

  # N/A escape-hatch validation for Gate 1 (same rule).
  local gate1_line
  gate1_line=$(echo "$body" | grep -iE 'Gate[[:space:]]*1.*N/A' | head -1)
  if [ -n "$gate1_line" ]; then
    local after_na1
    after_na1=$(echo "$gate1_line" | sed -E 's/.*[Nn]\/[Aa][[:space:]]*//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
    if [ -z "$after_na1" ]; then
      local found_justification1=false
      local in_gate1=false
      while IFS= read -r line; do
        if echo "$line" | grep -iqE 'Gate[[:space:]]*1.*N/A'; then
          in_gate1=true
          continue
        fi
        if [ "$in_gate1" = "true" ]; then
          local stripped1
          stripped1=$(echo "$line" | sed 's/^[[:space:]]*//')
          if [ -z "$stripped1" ]; then
            continue
          fi
          if echo "$stripped1" | grep -iqE '^Gate[[:space:]]*[0-9]'; then
            break
          fi
          found_justification1=true
          break
        fi
      done <<< "$body"

      if [ "$found_justification1" = "false" ]; then
        TWO_GATE_FAIL_REASON="Gate 1 N/A requires a justification — use 'Gate 1: N/A — <reason>' or add a justification line immediately after"
        return 1
      fi
    fi
  fi

  return 0
}
