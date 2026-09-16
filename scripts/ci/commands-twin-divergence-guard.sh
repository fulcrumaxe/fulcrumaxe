#!/usr/bin/env bash
# scripts/ci/commands-twin-divergence-guard.sh — every file that legitimately
# exists twice in this tree has an up-to-date twin (D#2486, extended D#2598).
#
# Why this exists
# ----------------
# .claude/commands/ and top-level commands/ carry the SAME three files today,
# duplicated by whatever seeds the public export (see
# open-source/MANIFEST.md's GENERATED_PATHS block, engine-side: commands/ is
# export.sh's own generated mirror of .claude/commands/, not an independently
# maintained copy). Nothing enforced that the two ever agreed, so a PR that
# edited only .claude/commands/start-the-day.md left a stale, since-corrected
# instruction alive in commands/start-the-day.md with nothing marking it
# stale. This guard is the check that would have caught that PR.
#
# D#2598 found three more such pairs drifting silently for the same reason —
# nothing compared them: agents/ vs .claude/agents/ (17 of 26 files diverged,
# some by 100+ lines — a stale top-level mirror of the actual role cards),
# scripts/X vs loop-bootstrap/scripts/X (the adopter's start-the-day.sh was
# 301 lines against the live 807, missing self-heal and HEAD-restore
# entirely), and scripts/memory-triage/X vs loop-bootstrap/memories/X (9
# memory files had drifted, one file existed only on the adopter side with
# no counterpart at all). Rather than writing a second guard for each new
# pair — the D#2339 failure mode this guard's own header used to warn about,
# for a different reason — this one file now checks all four families.
# Adding a fifth twin family later means extending the FAMILIES below, not
# writing scripts/ci/some-other-twin-guard.sh.
#
# What it checks — four families
# --------------------------------
#   commands   .claude/commands/*.md  <->  commands/<same-basename>
#   agents     .claude/agents/*.md    <->  agents/<same-basename>
#   scripts    loop-bootstrap/scripts/*  <->  scripts/<same-basename>
#   memories   loop-bootstrap/memories/*.md  <->  scripts/memory-triage/<same-basename>
#
# For "commands" and "agents", pairing is DRIVEN by .claude/<family>/ (the
# canonical, actually-loaded copy) exactly like the original commands-only
# guard:
#
#   Both exist and are byte-identical           -> PASS
#   Both exist and differ, pair not allowlisted -> FAIL, names the pair
#   Both exist and differ, pair allowlisted     -> PASS, names the allowlist reason
#   .claude/<family>/<name> has no top-level twin  -> FAIL, names it
#   top-level <family>/<name> has no .claude twin  -> NOTE, never fails
#     (see the original D#2486 reasoning: whether the top-level copy is
#     purely a generated mirror, an independent adopter-facing surface, or
#     both is a structural question this guard is not positioned to answer)
#
# For "scripts" and "memories", pairing is DRIVEN by the loop-bootstrap/ side
# on purpose — that side is deliberately small (one hand-maintained variant
# for "scripts" today; whatever the tier rule ships for "memories") and the
# live side (scripts/, scripts/memory-triage/) is deliberately large and NOT
# expected to have a loop-bootstrap twin for every file (residue scripts like
# generate-initial-plan.py have no live counterpart at all by design; most of
# scripts/memory-triage/ is tier:hardwire-candidate or otherwise excluded
# from shipping). So here the reverse direction — a live file with no
# loop-bootstrap twin — is NEVER checked, not even as a NOTE: asserting every
# one of ~270 scripts/ files needs a loop-bootstrap/scripts/ counterpart
# would be absurd, unlike the commands/agents case where both sides are
# small, symmetric mirrors of each other.
#
#   loop-bootstrap side file, live twin exists, identical      -> PASS
#   loop-bootstrap side file, live twin exists, differs,
#     pair not allowlisted                                      -> FAIL
#   loop-bootstrap side file, live twin exists, differs,
#     pair allowlisted                                          -> PASS (reason named)
#   loop-bootstrap side file, NO live twin at all               -> NOTE, never fails
#     (a legitimate bootstrap-only residue script, or similar)
#
# If loop-bootstrap/memories/ does not exist at all (the normal state after
# D#2598 — memories are derived from scripts/memory-triage/ by tier, not
# hand-copied), the "memories" family has zero pairs to check and reports 0
# divergent, 0 matched, with a note explaining why.
#
# The allowlist
# -------------
# scripts/ci/twin-divergence-allowlist.json holds ONLY deliberate variants —
# never known drift, never "pending reconciliation". Every entry requires:
#   pair    "<family>:<basename>"   e.g. "scripts:start-dashboard.sh"
#   date    ISO 8601 (YYYY-MM-DD)
#   reason  non-empty, states what is deliberately different
# An entry missing date or reason, or naming "pending"/"follow-up"/
# "reconcile later" in its reason, makes the guard FAIL — an allowlist is not
# a place to park something for later.
#
# Usage
# -----
#   bash scripts/ci/commands-twin-divergence-guard.sh
#
# Exit 0: every pair in every family is either identical or an allowlisted
#         deliberate variant, and the allowlist itself is well-formed.
# Exit 1: a pair diverged unlisted, a required twin is missing, or the
#         allowlist itself is malformed (missing date/reason, or a
#         "pending"-shaped reason).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

ALLOWLIST="$REPO_ROOT/scripts/ci/twin-divergence-allowlist.json"

# D#2598 fix-round 2 item 2: agents/ is no longer a byte-identical mirror of
# .claude/agents/ — it's generated from it (scripts/lib/agents-plugin-mirror.sh
# strips this project's Discussion-plane repo down to a runtime resolver call,
# the same shape .claude/agents/ already uses for the code plane, so a plugin
# user never gets this project's own repo baked into a namespaced role).
# shellcheck source=scripts/lib/agents-plugin-mirror.sh
source "$REPO_ROOT/scripts/lib/agents-plugin-mirror.sh"

FAILED=0
MATCHED=0
ALLOWED=0

# ── allowlist validation + lookup ───────────────────────────────────────────
# One python3 pass validates the whole file (every entry has pair/date/reason,
# no banned "pending"-shaped reason) and prints one line per valid entry as
# "<pair>\t<reason>" for the bash side to consume. A malformed file exits
# non-zero and prints nothing else — the guard below treats that as "no
# allowlist available" AND flips the overall run to FAIL, since a broken
# allowlist file must never silently behave like an empty (permissive-only
# for identical pairs) one.
ALLOWLIST_OK=true
ALLOWLIST_ENTRIES=""
if [ -f "$ALLOWLIST" ]; then
  ALLOWLIST_ENTRIES="$(python3 - "$ALLOWLIST" <<'PYEOF'
import json, re, sys
from datetime import date as _date

path = sys.argv[1]
BANNED = ("pending", "follow-up", "followup", "reconcile later")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"ERROR: could not parse {path}: {e}", file=sys.stderr)
    sys.exit(1)

entries = data.get("entries", [])
if not isinstance(entries, list):
    print(f"ERROR: {path}: 'entries' must be a list", file=sys.stderr)
    sys.exit(1)

bad = 0
for e in entries:
    pair = e.get("pair") if isinstance(e, dict) else None
    date = e.get("date") if isinstance(e, dict) else None
    reason = e.get("reason") if isinstance(e, dict) else None
    if not pair or not date or not reason:
        print(f"ERROR: {path}: entry missing pair/date/reason: {e}", file=sys.stderr)
        bad += 1
        continue
    # Reject anything that isn't a real ISO 8601 calendar date (YYYY-MM-DD).
    # A regex match alone accepts "2026-13-40"; date.fromisoformat() also
    # rejects an out-of-range month/day, not just a wrong shape.
    if not isinstance(date, str) or not ISO_DATE_RE.match(date):
        print(f"ERROR: {path}: entry '{pair}' date is not ISO 8601 YYYY-MM-DD: {date!r}", file=sys.stderr)
        bad += 1
        continue
    try:
        _date.fromisoformat(date)
    except ValueError:
        print(f"ERROR: {path}: entry '{pair}' date is not a real calendar date: {date!r}", file=sys.stderr)
        bad += 1
        continue
    low = reason.lower()
    if any(b in low for b in BANNED):
        print(f"ERROR: {path}: entry '{pair}' reason reads as a placeholder, not a deliberate-variant reason: {reason!r}", file=sys.stderr)
        bad += 1
        continue
    print(f"{pair}\t{reason}")

if bad:
    sys.exit(1)
PYEOF
)"
  ALLOWLIST_RC=$?
  if [ "$ALLOWLIST_RC" -ne 0 ]; then
    ALLOWLIST_OK=false
    echo "ALLOWLIST INVALID — see errors above; treating every pair as unlisted until fixed" >&2
  fi
else
  # No allowlist file at all is not an error — it just means no family has any
  # deliberate variants right now.
  ALLOWLIST_ENTRIES=""
fi

# is_allowlisted <family> <basename> — prints the reason and returns 0 if
# allowlisted (and the allowlist itself parsed cleanly); returns 1 otherwise.
is_allowlisted() {
  local family="$1" name="$2" key="${1}:${2}"
  [ "$ALLOWLIST_OK" = "true" ] || return 1
  [ -n "$ALLOWLIST_ENTRIES" ] || return 1
  local line
  line="$(printf '%s\n' "$ALLOWLIST_ENTRIES" | awk -F'\t' -v k="$key" '$1==k{print $2; exit}')"
  if [ -n "$line" ]; then
    printf '%s' "$line"
    return 0
  fi
  return 1
}

# ── shared pair-check helper ────────────────────────────────────────────────
# check_pair <family> <name-for-messages> <path-a> <path-b>
# path-a is treated as the reference for the diff display (matches the
# original guard's "diff -u top_path claude_path" orientation: -u old new).
check_pair() {
  local family="$1" name="$2" path_a="$3" path_b="$4"
  local reason
  if cmp -s "$path_a" "$path_b"; then
    echo "PASS $name ($family) — identical"
    MATCHED=$((MATCHED + 1))
    return
  fi
  if reason="$(is_allowlisted "$family" "$name")"; then
    echo "PASS $name ($family) — allowlisted deliberate variant: $reason"
    ALLOWED=$((ALLOWED + 1))
    return
  fi
  echo "FAIL $name ($family) — $path_a and $path_b differ:"
  diff -u "$path_a" "$path_b" | sed 's/^/    /'
  FAILED=$((FAILED + 1))
}

# ═══════════════════════════════════════════════════════════════════════════
# Family: commands  (.claude/commands/*.md <-> commands/<name>)  — unchanged
# from the original D#2486 guard.
# ═══════════════════════════════════════════════════════════════════════════
echo "── commands ──────────────────────────────────────────────────────────"
CLAUDE_DIR=".claude/commands"
TOP_DIR="commands"

if [ ! -d "$CLAUDE_DIR" ]; then
  echo "FAIL commands — $CLAUDE_DIR is not a directory"
  FAILED=$((FAILED + 1))
else
  while IFS= read -r claude_path; do
    name="$(basename "$claude_path")"
    top_path="$TOP_DIR/$name"
    if [ ! -f "$top_path" ]; then
      echo "FAIL $name (commands) — $claude_path has no top-level twin at $top_path"
      FAILED=$((FAILED + 1))
      continue
    fi
    check_pair commands "$name" "$top_path" "$claude_path"
  done < <(find "$CLAUDE_DIR" -maxdepth 1 -type f -name '*.md' | sort)

  if [ -d "$TOP_DIR" ]; then
    while IFS= read -r top_path; do
      name="$(basename "$top_path")"
      if [ ! -f "$CLAUDE_DIR/$name" ]; then
        echo "NOTE $name (commands) — $top_path has no .claude/commands/ counterpart (not flagged: structural question open, see D#2486)"
      fi
    done < <(find "$TOP_DIR" -maxdepth 1 -type f -name '*.md' | sort)
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# Family: agents  (.claude/agents/*.md <-> agents/<name>) — NOT byte-identity
# (D#2598 fix-round 2 item 2): agents/ is GENERATED from .claude/agents/ via
# generate_agents_plugin_mirror, which strips this project's Discussion-plane
# repo down to a runtime resolver call. .claude/agents/ is canonical (it's
# what Claude Code actually loads for this project's own team) and is never
# touched by the generator; top-level agents/ must equal
# generate_agents_plugin_mirror(.claude/agents/<name>) exactly. Missing-twin
# rules are otherwise unchanged from before this round (same asymmetric
# shape as commands, D#2598 item 12): a canonical file with no top-level
# twin at all still fails, and a top-level-only file is still only noted.
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── agents ────────────────────────────────────────────────────────────"
CLAUDE_AGENTS_DIR=".claude/agents"
TOP_AGENTS_DIR="agents"

if [ ! -d "$CLAUDE_AGENTS_DIR" ]; then
  echo "FAIL agents — $CLAUDE_AGENTS_DIR is not a directory"
  FAILED=$((FAILED + 1))
else
  AGENTS_GEN_TMPDIR="$(mktemp -d)"
  while IFS= read -r claude_path; do
    name="$(basename "$claude_path")"
    top_path="$TOP_AGENTS_DIR/$name"
    if [ ! -f "$top_path" ]; then
      echo "FAIL $name (agents) — $claude_path has no top-level twin at $top_path"
      FAILED=$((FAILED + 1))
      continue
    fi
    gen_path="$AGENTS_GEN_TMPDIR/$name"
    generate_agents_plugin_mirror "$claude_path" > "$gen_path"
    check_pair agents "$name" "$top_path" "$gen_path"
  done < <(find "$CLAUDE_AGENTS_DIR" -maxdepth 1 -type f -name '*.md' | sort)
  rm -rf "$AGENTS_GEN_TMPDIR"

  if [ -d "$TOP_AGENTS_DIR" ]; then
    while IFS= read -r top_path; do
      name="$(basename "$top_path")"
      if [ ! -f "$CLAUDE_AGENTS_DIR/$name" ]; then
        echo "NOTE $name (agents) — $top_path has no .claude/agents/ counterpart (not flagged — same structural question as commands/, D#2486)"
      fi
    done < <(find "$TOP_AGENTS_DIR" -maxdepth 1 -type f -name '*.md' | sort)
  fi

  # Direct regression scan, independent of the generation-match check above:
  # even a file that perfectly matches its own freshly-generated mirror is
  # only as identity-free as the two literal patterns
  # generate_agents_plugin_mirror knows about. A NEW hardcoded mention added
  # to .claude/agents/ in some other spelling would sail through the
  # generation-match check (both sides would still agree, both still
  # leaking) without this second, independent assertion.
  if [ -d "$TOP_AGENTS_DIR" ]; then
    while IFS= read -r top_path; do
      name="$(basename "$top_path")"
      if grep -q "autonomous-agent-7" "$top_path" 2>/dev/null; then
        echo "FAIL $name (agents) — $top_path still contains a literal 'autonomous-agent-7' mention after generation"
        FAILED=$((FAILED + 1))
      fi
    done < <(find "$TOP_AGENTS_DIR" -maxdepth 1 -type f -name '*.md' | sort)
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# Family: scripts  (loop-bootstrap/scripts/* <-> scripts/<name>) — driven by
# the loop-bootstrap side (D#2598 item 5). A loop-bootstrap-only file with no
# live twin is a legitimate bootstrap-only residue script, not drift.
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── scripts ───────────────────────────────────────────────────────────"
BOOT_SCRIPTS_DIR="loop-bootstrap/scripts"
LIVE_SCRIPTS_DIR="scripts"

if [ ! -d "$BOOT_SCRIPTS_DIR" ]; then
  echo "NOTE scripts — $BOOT_SCRIPTS_DIR does not exist — nothing to pair"
else
  while IFS= read -r boot_path; do
    name="$(basename "$boot_path")"
    live_path="$LIVE_SCRIPTS_DIR/$name"
    if [ ! -f "$live_path" ]; then
      echo "NOTE $name (scripts) — $boot_path has no live scripts/ twin (bootstrap-only residue script, expected)"
      continue
    fi
    check_pair scripts "$name" "$live_path" "$boot_path"
  done < <(find "$BOOT_SCRIPTS_DIR" -maxdepth 1 -type f | sort)
fi

# ═══════════════════════════════════════════════════════════════════════════
# Family: memories  (loop-bootstrap/memories/*.md <-> scripts/memory-triage/<name>)
# driven by the loop-bootstrap side, same shape as "scripts" (D#2598 item
# 13/14). Since memories now ship by reading scripts/memory-triage/ directly
# by tier (see loop-bootstrap/bootstrap.sh step 1), loop-bootstrap/memories/
# is not expected to exist at all; when it doesn't, this family has zero pairs.
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── memories ──────────────────────────────────────────────────────────"
BOOT_MEMORIES_DIR="loop-bootstrap/memories"
TRIAGE_DIR="scripts/memory-triage"

if [ ! -d "$BOOT_MEMORIES_DIR" ]; then
  echo "NOTE memories — $BOOT_MEMORIES_DIR does not exist — memories are derived from $TRIAGE_DIR by tier (see loop-bootstrap/bootstrap.sh), nothing to pair"
else
  while IFS= read -r boot_path; do
    name="$(basename "$boot_path")"
    triage_path="$TRIAGE_DIR/$name"
    if [ ! -f "$triage_path" ]; then
      echo "NOTE $name (memories) — $boot_path has no $TRIAGE_DIR twin"
      continue
    fi
    check_pair memories "$name" "$triage_path" "$boot_path"
  done < <(find "$BOOT_MEMORIES_DIR" -maxdepth 1 -type f -name '*.md' | sort)
fi

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
if [ "$ALLOWLIST_OK" != "true" ]; then
  echo "commands-twin-divergence-guard: FAIL — allowlist $ALLOWLIST is malformed (see errors above)" >&2
  exit 1
fi
if [ "$FAILED" -gt 0 ]; then
  echo "commands-twin-divergence-guard: FAIL — $FAILED pair(s) diverged or missing a required twin, $MATCHED matched, $ALLOWED allowlisted" >&2
  exit 1
fi

echo "commands-twin-divergence-guard: OK — $MATCHED pair(s) identical, $ALLOWED allowlisted"
exit 0
