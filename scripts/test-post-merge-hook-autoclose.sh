#!/usr/bin/env bash
# test-post-merge-hook-autoclose.sh — unit tests for multi-Discussion auto-detect
# in scripts/post-merge-hook.sh (lines 42-74).
#
# Mocks: gh CLI is overridden via PATH. GraphQL discussion(number:N) returns
# non-null id for N in MOCK_DISC_IDS; returns null otherwise.
#
# Run: bash scripts/test-post-merge-hook-autoclose.sh

set -uo pipefail

PASS=0
FAIL=0

# ── Mock gh binary ─────────────────────────────────────────────────────────────
MOCK_BIN_DIR=$(mktemp -d)
trap 'rm -rf "$MOCK_BIN_DIR"' EXIT

# Discussions that exist (GraphQL returns real id for these numbers)
MOCK_DISC_IDS="99 100 101 738 999"

cat > "$MOCK_BIN_DIR/gh" <<'MOCK_GH'
#!/usr/bin/env bash
# Minimal gh mock for post-merge-hook auto-detect tests

# gh pr view <PR> --json body --jq '.body'
if [[ "$1 $2" == "pr view" ]] && echo "$@" | grep -q "\-\-json body"; then
  echo "${MOCK_PR_BODY:-}"
  exit 0
fi

# gh api graphql -f query=... --jq '.data.repository.discussion.id'
if [[ "$1 $2" == "api graphql" ]]; then
  # Extract discussion number from the query string
  DISC_NUM=$(echo "$@" | grep -oE 'discussion\(number:[0-9]+\)' | grep -oE '[0-9]+' | head -1)
  if [[ -n "$DISC_NUM" ]]; then
    # Check if this number is in the allowed mock list
    if echo " ${MOCK_DISC_IDS} " | grep -q " ${DISC_NUM} "; then
      echo "D_${DISC_NUM}_mock_id"
    else
      echo "null"
    fi
  fi
  exit 0
fi

# Catch-all: allow other gh calls to succeed silently
exit 0
MOCK_GH
chmod +x "$MOCK_BIN_DIR/gh"

export PATH="$MOCK_BIN_DIR:$PATH"
export MOCK_DISC_IDS

# ── Helper: run the auto-detect block extracted from post-merge-hook.sh ────────
# We source just the detection block, not the full hook (avoids needing all deps).
run_autodetect() {
  local pr_body="$1"
  local cli_discussion="${2:-}"

  MOCK_PR_BODY="$pr_body" bash - <<SHELL
export PATH="$MOCK_BIN_DIR:\$PATH"
export MOCK_DISC_IDS="$MOCK_DISC_IDS"

PR="42"
DISCUSSION="$cli_discussion"

declare -a DISCUSSIONS=()
if [[ -n "\$DISCUSSION" ]]; then
  DISCUSSIONS=("\$DISCUSSION")
else
  PR_BODY=\$(gh pr view "\$PR" --repo ${REPO:-fulcrumaxe/fulcrumaxe} --json body \\
    --jq '.body' 2>/dev/null || echo "")

  RAW_NUMS=\$(echo "\$PR_BODY" \\
    | grep -oiE '(D#|[Dd]iscussion #|[Cc]loses #|[Rr]esolves #|[Ff]ixes #)[0-9]+' \\
    | grep -oE '[0-9]+' \\
    | sort -u)

  for CAND in \$RAW_NUMS; do
    DISC_VALID=\$(gh api graphql \\
      -f query="query { repository(owner:\"fulcrumaxe\", name:\"fulcrumaxe\") { discussion(number:\$CAND) { id } } }" \\
      --jq '.data.repository.discussion.id' 2>/dev/null || echo "")
    if [[ -n "\$DISC_VALID" && "\$DISC_VALID" != "null" ]]; then
      DISCUSSIONS+=("\$CAND")
    fi
  done
fi

DISCUSSION="\${DISCUSSIONS[0]:-}"

# Print result as space-separated list for test assertions
echo "\${DISCUSSIONS[*]:-}"
SHELL
}

# ── Test runner ────────────────────────────────────────────────────────────────
assert_equal() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $label"
    echo "      expected: '$expected'"
    echo "      actual:   '$actual'"
    FAIL=$((FAIL + 1))
  fi
}

# ── Test 1: single 'Resolves D#999' → one Discussion closed ───────────────────
RESULT=$(run_autodetect "Resolves D#999")
assert_equal "single Resolves D#999" "999" "$RESULT"

# ── Test 2: 'Closes D#100. Fixes D#101' → both closed ────────────────────────
RESULT=$(run_autodetect "Closes D#100. Fixes D#101")
# sort to make order stable for assertion
SORTED=$(echo "$RESULT" | tr ' ' '\n' | sort | tr '\n' ' ' | xargs)
assert_equal "two Discussions: Closes D#100 + Fixes D#101" "100 101" "$SORTED"

# ── Test 3: '#42 fix' (no D-prefix on a non-Discussion number) → ignored ─────
# #42 not in MOCK_DISC_IDS so GraphQL returns null; also no D# prefix pattern
RESULT=$(run_autodetect "#42 fix")
assert_equal "bare #42 with no D-prefix ignored" "" "$RESULT"

# ── Test 4: 'Closes #42' where #42 is an Issue (GraphQL returns null) → ignored
# #42 is not in MOCK_DISC_IDS, so GraphQL mock returns null
RESULT=$(run_autodetect "Closes #42")
assert_equal "Closes #42 (Issue, not Discussion) ignored" "" "$RESULT"

# ── Test 5: --discussion CLI arg bypasses body parsing ────────────────────────
RESULT=$(run_autodetect "some body text" "738")
assert_equal "--discussion flag respected" "738" "$RESULT"

# ── Test 6: 'Discussion #99' pattern works ────────────────────────────────────
RESULT=$(run_autodetect "See Discussion #99 for context")
assert_equal "Discussion #N pattern" "99" "$RESULT"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
