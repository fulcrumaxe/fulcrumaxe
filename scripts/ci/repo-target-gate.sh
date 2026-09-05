#!/usr/bin/env bash
# scripts/ci/repo-target-gate.sh — fail-closed check for executable defaults
# that resolve to the PRIVATE engine owner in this repository's own tree.
#
# Ported from open-source/checks/repo-target-gate.sh (D#2348 PR-i). That
# original still exists and still runs, unchanged, from
# open-source/verify-export.sh; this is a sibling with a different target,
# not a replacement for it. The two are retired together when D#2348's
# archive phase retires the export.
#
# WHAT CHANGED IN THE PORT, and why each change was needed:
#
#   1. TARGET. The original scans a produced export tree — a tree
#      export.sh's rewrite pass has already been through, so the literal it
#      hunts is the POST-rewrite slug (fulcrumaxe/fulcrumaxe). Once
#      development happens in the public repo there is no rewrite pass and
#      no export tree; the repository itself is the artifact. So the
#      forbidden literal narrows to the PRIVATE engine owner — any
#      "<engine-owner>/<repo>" appearing as an executable default is wrong
#      by construction, because the code that ships is not published under
#      that owner. The public slug is correct here and is no longer
#      forbidden. The owner name itself is not written down in this file;
#      see the RULES_FILE block below for where it comes from and why.
#
#   2. SUBJECT SET. `git ls-files` instead of `find`. The published artifact
#      is the tracked tree; untracked build output (node_modules/, dist/)
#      is not published and scanning it produces only noise. This also
#      means an untracked file is invisible to this gate — stated as blind
#      spot 5 below rather than left to be discovered.
#
#   3. SCAN SCOPE. Directories that never reach a public push are excluded
#      (SCAN_EXCLUDE_PREFIXES below), reusing the same list and the same
#      reasoning as scripts/check-forbidden-identifiers.sh rather than
#      inventing a second one that can drift from it.
#
#   4. OWNER SOURCE. The owner half of the slug is read at runtime from
#      IDENTIFIER-RULES.txt rather than written down here. This was not the
#      original design; it is what CI required, and it is the better one.
#      See the RULES_FILE block below.
#
# All seven of the original's allowlist entries carry over verbatim, reasons
# included: they were arguments about what a line MEANS, and narrowing the
# literal does not change any of them. One entry is added, for a
# dashboard_tui/ line — see its reason, which is the only one here that is
# cover for a known live defect rather than a judgement that the line is
# fine.
#
# D#1870 (original rationale, still the point): a literal substitution
# cannot tell "the upstream project" (a README CI badge, a git-clone URL)
# from "your repo" (a hard-coded fallback default in resolver code). This
# check catches the second one by SHAPE: an executable assignment/default,
# not a URL and not documentation prose. Same path:anchor:reason
# content-anchored allowlist mechanics as identifier-gate.sh (D#2192), but
# a separate rules set — this check is about repo-*target* defaults
# specifically, not the general identifier sweep that
# scripts/check-forbidden-identifiers.sh owns pre-push.
#
# Usage: bash scripts/ci/repo-target-gate.sh [target-dir]
#
# With no argument it scans the repository it lives in, which is how CI runs
# it. The optional target-dir is for tests — see the note at TARGET_DIR
# below. Either way the tree must be a git checkout.
#
# Exit 0 = clean: every assignment-shaped hit is covered by a matching,
#          non-stale allowlist entry (or there were no hits at all).
# Exit 1 = one or more unallowlisted hits, or a stale allowlist entry whose
#          line no longer matches a forbidden shape.
# Exit 2 = usage/argument error.
#
# ---------------------------------------------------------------------------
# Documented blind spots (D#1870 Spec item 8) — shape is a proxy for
# meaning, not meaning itself. Re-stated here rather than left behind in the
# original's header, because a gate whose limits live in another file's
# comments has limits nobody reads. This check will NOT catch:
#
#   1. Prose-shaped wrong guidance. ".claude/agents/*.md" and "CLAUDE.md"
#      tell the agent which repo to target in prose, not in an executable
#      assignment. This check skips *.md/*.markdown entirely (see
#      SCAN_EXTENSIONS below) precisely so it doesn't also have to flag
#      README.md's CI badge and git-clone URL, which live in the same file
#      type. This is the largest gap and it matters more after the cutover,
#      not less: D#2348's own consensus panel named CLAUDE.md's Repo Scope
#      Invariant the highest-consequence single edit in the whole cutover,
#      and it is exactly the prose shape this cannot see.
#   2. Test fixtures. backend/tests/**, ts-backend/tests/**, and files
#      matching test_*/​*_test.*/​*.test.* legitimately reference the real
#      repo slug as realistic fixture data (PR/discussion URLs, assertion
#      values). This check skips any path under a tests/ directory or
#      matching those test-file naming conventions.
#   3. Multi-line / indirect assignment. Detection is single-line regex
#      against each forbidden shape; a default built across multiple
#      statements or via a helper function call is invisible to it.
#   4. Slugs built by concatenation or interpolation (the owner half + "/" +
#      the name half, or an f-string/template assembling the two) never
#      appear as the literal this check greps for.
#   5. Untracked files. The subject set is `git ls-files`; a file that is
#      not tracked is not scanned. That is correct for a gate about what
#      gets published, and wrong for anyone using this as a general lint.
#
# Known, allowlisted gaps in the fix set: see ALLOWLIST below — each entry
# documents why that specific hit isn't fixed rather than being invisible.
# ---------------------------------------------------------------------------

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -gt 1 ]]; then
  echo "usage: $(basename "$0") [target-dir]" >&2
  exit 2
fi

# Default target is the repository this script lives in — that is the whole
# point of the port. The optional argument is kept for one reason: it is
# what makes this gate testable against a synthetic fixture instead of only
# against the live tree, and a gate whose failing direction can only be
# exercised by mutating the real repo is a gate whose failing direction
# stops being exercised.
#
# Resolved with `git rev-parse --show-toplevel`, not "$SCRIPT_DIR/../..":
# in a linked worktree .git is a FILE, so a -d test on it reports "not a
# checkout" for a perfectly good tree — which is where every executor on
# this project runs.
TARGET_DIR="${1:-}"
if [[ -z "$TARGET_DIR" ]]; then
  if ! TARGET_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "error: $SCRIPT_DIR is not inside a git checkout — this gate enumerates its subject set with git ls-files" >&2
    exit 2
  fi
else
  if [[ ! -d "$TARGET_DIR" ]]; then
    echo "error: target dir not found: $TARGET_DIR" >&2
    exit 2
  fi
  if ! TARGET_DIR="$(git -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "error: $TARGET_DIR is not a git checkout — this gate enumerates its subject set with git ls-files" >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# The forbidden slug shape, READ AT RUNTIME rather than written down here.
#
# The original could hard-code its literal because it lives under
# open-source/, which never ships. This file lives under scripts/, which
# ships wholesale — and the engine owner's name is itself a forbidden
# identifier, so a version of this gate that spelled the owner out failed
# open-source/checks/identifier-gate.sh on six lines of its own source. A
# gate that cannot survive the gate next to it is not a gate anyone can
# keep, and this one is measured, not hypothetical.
#
# So the owner comes from IDENTIFIER-RULES.txt's IDENTITIES block — the same
# file export.sh's rewrite pass, open-source/checks/identifier-gate.sh and
# scripts/check-forbidden-identifiers.sh all read (D#1837). One source, no
# literal in a shipped file, and nothing here to drift.
#
# Only the OWNER comes from there, not a whole pattern. This gate's question
# is different from that file's: FORBIDDEN_PATTERNS is about identifiers
# that must not appear at all, while a repo slug is perfectly correct in a
# URL or in prose and wrong only in an executable default. Merging the two
# rule sets would make both less precise (D#1870).
#
# The repo half stays a bounded character class so the trailing quote in
# each pattern below still anchors: after an opening quote the next
# character must be the first character of the owner name, which is what
# keeps "https://github.com/<owner>/..." from satisfying any of them. It is
# a class rather than a fixed repo name because the owner has had more than
# one repository name and both are equally wrong as a default.
# ---------------------------------------------------------------------------
RULES_FILE="$TARGET_DIR/open-source/IDENTIFIER-RULES.txt"

# Tree-shape check FIRST, exactly as scripts/lib/preflight-common.sh does
# for its sibling gate. open-source/ never ships, so an exported or adopter
# tree legitimately has no rules file — and in a tree that is not this
# engine's checkout there is no "our private owner" to hunt, so there is
# nothing here to gate. Only once open-source/ is confirmed present does a
# missing rules file count as rot rather than legitimate export shape.
if [[ ! -d "$TARGET_DIR/open-source" ]]; then
  echo "SKIP: no open-source/ directory under $TARGET_DIR — export or adopter tree, no owner identity to resolve and nothing for this gate to check"
  exit 0
fi
if [[ ! -f "$RULES_FILE" ]]; then
  echo "FAIL: open-source/ is present but $RULES_FILE is not — refusing to guess the owner identity this gate is built to hunt"
  exit 1
fi

# Deliberately a targeted read of one key rather than the full block parser
# the sibling gates carry: this needs exactly one value, and a second copy
# of a 30-line parser for one key is the abstraction-for-one-use-case this
# project's constitution warns about.
OWNER="$(sed -n 's/^[[:space:]]*OLD_OWNER=\(.*\)$/\1/p' "$RULES_FILE" | head -1)"
OWNER="${OWNER%"${OWNER##*[![:space:]]}"}"
if [[ -z "$OWNER" ]]; then
  echo "FAIL: could not read OLD_OWNER from $RULES_FILE — a gate that cannot name what it is hunting must not report a pass"
  exit 1
fi

SLUG="$OWNER/[A-Za-z0-9._-]+"

# ---------------------------------------------------------------------------
# Forbidden shapes (grep -E, tight-anchored so a URL value never matches —
# the quote/brace must be immediately followed by the bare slug and nothing
# else, so "https://github.com/fulcrumaxe/fulcrumaxe" can't satisfy any of
# these: the character right after the opening quote is 'h', not 'f').
# ---------------------------------------------------------------------------
PATTERNS=(
  # 1. Variable assignment: X = "SLUG" / X = 'SLUG' (excludes ==, !=, <=, >=)
  '[^=!<>]=[[:space:]]*"'"$SLUG"'"'
  "[^=!<>]=[[:space:]]*'""$SLUG""'"
  # 2. Bare return: return "SLUG"
  'return[[:space:]]+["'"'"']'"$SLUG"'["'"'"']'
  # 3. env-var-style default: X.get(..., "SLUG") / getenv(..., "SLUG")
  '\.get\([^)]*,[[:space:]]*["'"'"']'"$SLUG"'["'"'"'][[:space:]]*\)'
  'getenv\([^)]*,[[:space:]]*["'"'"']'"$SLUG"'["'"'"']'
  # 4. Shell parameter-expansion default: ${VAR:-SLUG}
  '\$\{[A-Za-z_][A-Za-z0-9_]*:-'"$SLUG"'\}'
  # 5. JSON/JS/TS object field: "key": "SLUG" (or single/backtick quotes)
  ':[[:space:]]*["'"'"'\`]'"$SLUG"'["'"'"'\`]'
  # 6. CLI flag default: --repo SLUG / --repo=SLUG
  '\-\-repo[=[:space:]]+'"$SLUG"
  # 7. Keyword default: default="SLUG" / default: 'SLUG'
  'default[[:space:]]*[=:][[:space:]]*["'"'"']'"$SLUG"'["'"'"']'
)

# ---------------------------------------------------------------------------
# ALLOWLIST — path:anchor:reason, content-anchored (D#2192; ported from
# identifier-gate.sh's own mechanism, D#2186 — see that script for the
# canonical rule set, duplicated below rather than shared: two call sites
# doesn't justify the abstraction yet, see the PR description).
#
# A line number drifts silently every time an earlier edit in the same file
# shifts everything below it. That's not hypothetical: PR #2274 added 14
# lines near the top of loop-bootstrap/bootstrap.sh, shifting its
# allowlisted SOURCE_REPO line from 92 to 106 without touching its content
# at all — the gate reported both a brand-new unallowlisted hit at 106 and
# a stale entry at 92, for a line that never actually changed. A content
# anchor — a fixed-string substring of the allowlisted line, not its line
# number — moves with the line it names instead of quietly pointing at the
# wrong one, and PR #2274 had to hand-edit the pin to unblock itself, which
# is the exact recurring cost this port removes.
#
# Five fail-closed rules, all mandatory, identical to identifier-gate.sh's:
#   1. The entry must contain EXACTLY 2 colons total (the two path:anchor
#      and anchor:reason separators) — neither the anchor nor the reason
#      may contain a colon anywhere. Checked on the raw entry before any
#      %%/#* split, because after the split $anchor can never contain a
#      colon by construction and the check would be unreachable dead code
#      (the exact D#2186 review finding this rule exists to fix).
#   2. A purely numeric anchor is rejected — a re-armed line-pin wearing
#      the new field name, not actual content.
#   3. A path that doesn't exist in the scanned tree is rejected.
#   4. The anchor must match EXACTLY ONE line in that file: zero matches
#      means the entry is stale (the anchored text is gone), two or more
#      means the anchor is ambiguous (it doesn't pin down one line).
#   5. Once resolved to a line number, the entry still has to pass the
#      existing stale check below: if that line isn't actually a
#      forbidden-shape hit, the entry is stale cover, not a valid
#      allowlisting.
#
# Three reasons below carry a punctuation-only edit from their original
# line-keyed wording: rule 1 forbids any colon in the reason text, and
# "Usage::" (runtime.py), a "backend.ts:75" cross-reference (index.tsx —
# also now a stale line number in its own right), and a "):" join
# (bootstrap.sh) each had exactly one incidental colon. Each was reworded
# to remove only the colon; no reasoning, citation, or decision recorded in
# any entry was shortened or dropped.
#
# An allowlisted line that no longer matches ANY forbidden shape is drift
# (the code changed, the entry is now unverified cover) and FAILs the run.
# No blanket path exclusions.
# ---------------------------------------------------------------------------
ALLOWLIST_ENTRIES=(
  "backend/fleet/runtime.py:#     \"repo\":Illustrative comment inside the module docstring's Usage-block example showing discover_running_projects()'s return shape — not live code, not a resolver default."
  "ts-backend/src/config/repo.ts:export const DEFAULT_REPO:DEFAULT_REPO is the last-resort fallback of a four-step precedence chain (config.json, then GH_REPO, then _REPO, then this literal), so an adopter who configures any earlier step never reaches it. D#2348 removed the FROZEN RULE that used to justify this entry — that rule reserved edits of the literal to export.sh's substitution pass, and D#2348 retires that pass — but the literal is still a hard-coded repo-target default of exactly the shape this check exists to name. Fixing it for real means giving ts-backend an origin-remote resolver like backend/_repo.py's, which is its own phase of D#2348, not a one-line patch."
  "tui/src/backend.ts:GH_REPO:GH_REPO passed to a subprocess env; same defect class as backend/_repo.py but the TUI has no equivalent resolver module yet — needs one built, not a one-line patch. Deferred to a focused TUI-config follow-up."
  "tui/src/index.tsx:gh api graphql --repo:Embedded gh command template hard-codes --repo and the GraphQL owner/name context; same TUI-config gap as backend.ts above, same follow-up."
  "loop-bootstrap/bootstrap.sh:SOURCE_REPO=\"\${LOOP_BOOTSTRAP_SOURCE_REPO:SOURCE_REPO is a sed SEARCH KEY (the literal string do_install's rewrite looks for in the copied corpus), not a repo-target default — the actual target an adopter's bootstrap run acts on comes from the mandatory --repo flag a few lines below, which has no default and hard-errors if omitted. This is the exact D#1870 blind-spot shape (shape is a proxy for meaning, not meaning itself) — re-allowlisted now that loop-bootstrap/ ships again (see the removal note this replaces, right below) with the same reasoning D#1872's original entry used."
  "loop-bootstrap/bootstrap.sh:ENGINE_CANONICAL_REPO=\"\${LOOP_BOOTSTRAP_ENGINE_REPO:This literal (D#2335 /update PR 1) is the ENGINE's own upstream identity, written into an adopter's engine-install.json baseline stamp as the default source_repo scripts/update-check.sh compares against — it is not a resolver for the adopter's OWN project identity, which the mandatory --repo flag above already owns. export.sh's identifier rewrite turns this literal into the exporting fork's own slug exactly like the SOURCE_REPO entry directly above, so a forked engine's export correctly points an adopter's /update at that fork's own upstream instead of this repo — precisely the behavior the Spec's Implementation Notes ask for ('an adopter pointed at a fork is compared against their own upstream')."
  "scripts/update-check.sh:DEFAULT_ENGINE_REPO=\"\${LOOP_BOOTSTRAP_ENGINE_REPO:Same literal, same reasoning, same D#2335 /update PR 1 — this is update-check.sh's own fallback (used by --record-baseline and read as a default when a stamp omits source_repo), not a resolver for the adopter's own project identity. See the loop-bootstrap/bootstrap.sh ENGINE_CANONICAL_REPO entry above for the full reasoning; duplicated here because it is a separate literal in a separate shipped file, not a second reference to the same line."
)
# NOTE (D#2348 phase 1): dashboard_tui/readers/pr_detail.py:_REPO was
# allowlisted here as cover for a known live resolver default. dashboard_tui/
# has moved to the private internal repo, so the path is no longer in the
# scanned tree and the entry became stale cover — which this gate fails on by
# design ("an allowlisted line that no longer matches ANY forbidden shape is
# drift"). Removed rather than reworded, same as the D#1890 and D#1879 entries
# below. The underlying defect did not go away and is not fixed here: the
# literal moved with the file, and the TUI-config Discussion that entry called
# for still owns it. Nothing in this repo is scanning it any more, which is the
# honest state to record rather than keeping an entry that cannot match.
# NOTE (D#2348 PR-a): hooks/repo_scope_warn.py's _TARGET_REPO used to be
# allowlisted here as "PARKED — hooks/ logic changes split out of D#1870 by
# owner direction". It is no longer parked and no longer a literal: the hook
# now resolves its target through backend._repo at warn time, so the line no
# longer matches any forbidden shape and an entry for it would itself be
# stale cover. Removed rather than reworded, same as the D#1879 entry below.
# NOTE (D#1890, superseded above): this entry was removed 2026-08-17 when
# loop-bootstrap/ became a MANIFEST exclusion (a security review had found
# backend-snapshot/ genuinely failing identifier-gate.sh) — the path this
# check scans stopped shipping, so the entry pointed nowhere. loop-bootstrap/
# is a ship-listed MANIFEST.md PATHS entry again as of this PR (the specific
# blocker D#1890 named, backend-snapshot/, no longer exists in the tree), so
# the entry is restored above rather than left as a gap. Kept as a note, not
# deleted, so a future reader can see why this line has come and gone twice
# instead of re-deriving it from git blame.
# NOTE (D#1879, re-measured 2026-09-04 in this port): scripts/lib/
# external_intake_gate.py's module-level default repo slug used to be
# allowlisted here on the claim that "every real call site passes repo_slug
# explicitly from $_REPO" — that env var does not exist and the module reads
# no such variable; the real call sites (pre-spawn-check.sh,
# merge-and-hook.sh, loop-phased-step5.sh x2, team-lead-iteration.sh,
# ci-status-check.sh) call check_discussion/classify_and_label/etc. with only
# a Discussion number, so the default WAS live-reachable — a false allowlist
# justification hiding a real confused-deputy vulnerability (CWE-863/441/668)
# on an adopter fork. Fixed for real instead: the module resolves its default
# rather than hard-coding it, so the line no longer matches any forbidden
# shape and needs no entry here.
#
# The original of this note pinned that symbol as "external_intake_gate.py:74's
# DEFAULT_REPO_SLUG". Both halves of that pin are now wrong and it was carried
# forward corrected rather than copied. The symbol is now
# DEFAULT_DISCUSSION_REPO_SLUG — renamed by D#2348 PR-f2 (#2373), which pointed
# it at the Discussion plane rather than the code plane — and the ":74" had
# drifted more than a hundred lines off it by the time this port checked. The
# note is worth keeping because the REASON is still live: a line that stopped
# matching is not an entry to reword, it is an entry to delete. But a note
# naming a symbol nobody can grep for teaches the reader nothing, and the
# corrected text carries no line number at all — a pin in a comment drifts
# exactly the way the line-keyed allowlist this check replaced did.

# ---------------------------------------------------------------------------
# File selection: only scan surfaces where "assignment" is executable —
# skip markdown (prose blind spot #1) and test paths/files (blind spot #2).
# ---------------------------------------------------------------------------
SCAN_EXTENSIONS=(py sh ts tsx js jsx json yaml yml)

# Paths that never reach a public push, so a hit inside them is not a
# publishing defect and scanning them produces only false blocks. This list
# and its reasoning are deliberately the SAME as
# scripts/check-forbidden-identifiers.sh's — the push set is one fact and
# two gates disagreeing about it would be worse than either gate's own
# blind spots. The authoritative push set is the owner decision recorded on
# D#2348 ("Owner decision — PR-l's exclusion set, now explicit"), not
# MANIFEST.md, which described what the export shipped.
#
# archive/ earns its place twice over: it is not published, and the Archive
# Protocol moves whole files under it, so an archive move of anything
# carrying one of these literals would otherwise fail this gate for doing
# exactly what the protocol requires.
#
# open-source/ is excluded for the same reason it always was, plus one
# specific to this port: open-source/checks/repo-target-gate.sh, the file
# this was copied from, necessarily contains slug text as pattern data.
#
# dashboard_tui/ is NOT here even though it is outside the push set — see
# the dashboard_tui allowlist entry above for why that omission is
# deliberate.
SCAN_EXCLUDE_PREFIXES=(
  ".autonomous-team/"
  "archive/"
  "open-source/"
  "wiki/"
  "scripts/training/"
  "scripts/serving/"
  "scripts/gemma-sandbox/"
)

is_excluded_path() {
  local p="$1" prefix
  for prefix in "${SCAN_EXCLUDE_PREFIXES[@]}"; do
    [[ "$p" == "$prefix"* ]] && return 0
  done
  return 1
}

is_test_path() {
  local rel="$1" base
  base="$(basename "$rel")"
  case "$rel" in
    */tests/*|tests/*) return 0 ;;
  esac
  case "$base" in
    test_*|test-*) return 0 ;;
    *_test.*|*.test.*) return 0 ;;
  esac
  return 1
}

# `git ls-files` rather than `find`: the tracked tree is the artifact, and
# untracked build output (node_modules/, dist/) is not published. Paths come
# back relative to the repo root, which is what the allowlist keys on.
CANDIDATE_FILES=()
while IFS= read -r rel; do
  [[ -z "$rel" ]] && continue
  is_excluded_path "$rel" && continue
  is_test_path "$rel" && continue
  ext="${rel##*.}"
  match=0
  for e in "${SCAN_EXTENSIONS[@]}"; do
    [[ "$ext" == "$e" ]] && match=1 && break
  done
  [[ "$match" -eq 0 ]] && continue
  # A tracked path can be absent from the working tree mid-rebase; skip
  # rather than hand grep a filename it will error on.
  [[ -f "$TARGET_DIR/$rel" ]] || continue
  CANDIDATE_FILES+=("$TARGET_DIR/$rel")
done < <(git -C "$TARGET_DIR" ls-files)

FILES_SCANNED="${#CANDIDATE_FILES[@]}"

# A subject set of zero is the silent-skip this gate exists to prevent: a
# scan with nothing to scan would report every file fine.
if [[ "$FILES_SCANNED" -eq 0 ]]; then
  echo "FAIL: zero candidate files after filtering — a scan with no subjects cannot vouch for anything"
  exit 1
fi
SUMMARY="patterns=${#PATTERNS[@]} allowlist=${#ALLOWLIST_ENTRIES[@]} files_scanned=$FILES_SCANNED"

if [[ "${#PATTERNS[@]}" -eq 0 ]]; then
  echo "FAIL: zero forbidden patterns defined ($SUMMARY) — refusing to report a vacuous PASS"
  exit 1
fi

# ---------------------------------------------------------------------------
# Parse allowlist into a key -> reason map; track which entries got hit.
#
# HITS must be initialised BEFORE this loop, not after it: a malformed entry
# increments HITS at first use (below), and under `set -u` (line 52)
# referencing an unset HITS here would abort the whole script with an
# "unbound variable" error before any file is scanned and before the
# cleanup trap is installed — an uncontrolled abort, not a deliberate
# fail-closed FAIL. The naive fix of only adding `HITS=0` here without also
# removing the old post-loop reset is worse than doing nothing: it lets a
# later `HITS=0` clobber the malformed-entry count back to zero, so the gate
# prints "FAIL: malformed allowlist entry" and then reports PASS with rc=0
# (D#1879). Both halves are required together.
#
# Each entry resolves anchor -> line number itself (rather than trusting a
# hand-written one) via the same five rules as identifier-gate.sh's ALLOWLIST
# parser — see the comment above ALLOWLIST_ENTRIES for the full rule list.
# ---------------------------------------------------------------------------
declare -A ALLOWLIST_SEEN
ALLOWLIST_KEYS=()
HITS=0
for entry in "${ALLOWLIST_ENTRIES[@]}"; do
  # Rule 1: exactly 2 colons in the raw entry, checked before any split —
  # once $rest has been truncated at its first colon to produce $anchor,
  # that value can never contain a colon by construction, so testing the
  # post-split value can never fire (D#2186 review finding).
  colon_count="$(grep -o ':' <<<"$entry" | wc -l)"
  if [[ "$colon_count" -ne 2 ]]; then
    echo "FAIL: allowlist entry has $colon_count colon(s), need exactly 2 (path:anchor:reason) — an extra colon can't be safely attributed to the anchor or the reason: $entry"
    HITS=$((HITS + 1))
    continue
  fi
  path="${entry%%:*}"
  rest="${entry#*:}"
  anchor="${rest%%:*}"
  reason="${rest#*:}"
  if [[ -z "$path" || -z "$anchor" || -z "$reason" || "$reason" == "$rest" ]]; then
    echo "FAIL: malformed allowlist entry (need path:anchor:reason): $entry"
    HITS=$((HITS + 1))
    continue
  fi
  # Rule 2: a purely numeric anchor is a re-armed line-pin, not content.
  if [[ "$anchor" =~ ^[0-9]+$ ]]; then
    echo "FAIL: allowlist anchor '$anchor' is purely numeric — that is a re-armed line-pin, not a content anchor: $entry"
    HITS=$((HITS + 1))
    continue
  fi
  # Rule 3: the path must exist in the scanned tree.
  anchor_file="$TARGET_DIR/$path"
  if [[ ! -f "$anchor_file" ]]; then
    echo "FAIL: allowlist entry references a path not found in the scanned tree: $path"
    HITS=$((HITS + 1))
    continue
  fi
  # Rule 4: the anchor must match EXACTLY ONE line — zero is stale, two or
  # more is ambiguous (it doesn't pin down one line).
  mapfile -t anchor_lines < <(grep -nF -- "$anchor" "$anchor_file" 2>/dev/null)
  match_count="${#anchor_lines[@]}"
  if [[ "$match_count" -eq 0 ]]; then
    echo "FAIL: allowlist anchor matches zero lines in $path (stale): $anchor"
    HITS=$((HITS + 1))
    continue
  fi
  if [[ "$match_count" -gt 1 ]]; then
    echo "FAIL: allowlist anchor matches $match_count lines in $path (ambiguous): $anchor"
    HITS=$((HITS + 1))
    continue
  fi
  lineno="${anchor_lines[0]%%:*}"
  # Rule 5 (drift/stale-cover check) happens below, once the scan loop has
  # run: this key is added to ALLOWLIST_KEYS/ALLOWLIST_SEEN exactly like the
  # old line-keyed version did, so the existing stale-allowlist-entry logic
  # applies unchanged to an anchor-resolved line number.
  key="$path:$lineno"
  ALLOWLIST_KEYS+=("$key")
  ALLOWLIST_SEEN["$key"]=0
done

GREP_OUT="$(mktemp)"
GREP_ERR="$(mktemp)"
cleanup() { rm -f "$GREP_OUT" "$GREP_ERR"; }
trap cleanup EXIT

if [[ "${#CANDIDATE_FILES[@]}" -gt 0 ]]; then
  for pattern in "${PATTERNS[@]}"; do
    grep -HnE "$pattern" "${CANDIDATE_FILES[@]}" >"$GREP_OUT" 2>"$GREP_ERR"
    grep_rc=$?
    if [[ "$grep_rc" -ge 2 ]]; then
      echo "FAIL: pattern errored during scan (grep exit $grep_rc): $pattern"
      if [[ -s "$GREP_ERR" ]]; then
        while IFS= read -r errline; do
          echo "       grep stderr: $errline"
        done < "$GREP_ERR"
      fi
      HITS=$((HITS + 1))
      continue
    fi
    while IFS=: read -r f lineno rest; do
      [[ -z "$f" ]] && continue
      rel="${f#"$TARGET_DIR"/}"
      key="$rel:$lineno"
      if [[ -n "${ALLOWLIST_SEEN[$key]+x}" ]]; then
        ALLOWLIST_SEEN["$key"]=1
      else
        echo "FAIL: unallowlisted repo-target default at $key: $rest"
        HITS=$((HITS + 1))
      fi
    done < "$GREP_OUT"
  done
fi

# Stale allowlist entries — the line no longer matches any forbidden shape.
for key in "${ALLOWLIST_KEYS[@]}"; do
  if [[ "${ALLOWLIST_SEEN[$key]:-0}" -eq 0 ]]; then
    echo "FAIL: stale allowlist entry '$key' — line no longer matches a forbidden shape"
    HITS=$((HITS + 1))
  fi
done

if [[ "$HITS" -gt 0 ]]; then
  echo "FAIL ($SUMMARY)"
  exit 1
fi
echo "PASS ($SUMMARY)"
exit 0
