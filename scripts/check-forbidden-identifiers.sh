#!/usr/bin/env bash
# scripts/check-forbidden-identifiers.sh — pre-push forbidden-identifier scan.
#
# D#2348 PR-g. `open-source/export.sh` used to run a forbidden-identifier
# scan on the way out, and nothing was published until it passed. Once
# development happens in the public repo that door is gone, and a
# `pull_request` CI job is not a replacement: a pull_request job runs after
# the branch is already pushed, and on a public repo that branch is
# world-readable the moment it lands. So this fires PRE-PUSH, from
# run_always_gates() in scripts/lib/preflight-common.sh — the same function
# executors run locally and the `preflight` CI job runs. One mechanism, two
# invocation points; this file adds no third.
#
# WHAT THIS IS. A guardrail, not a security boundary. It is bypassable —
# skip preflight, or push without running it, and nothing here fires. The
# actor it exists for is our own executor making a mistake, not an
# adversary. Same framing as CLAUDE.md's "hooks/ is a Guardrail, Not a
# Security Boundary".
#
# DATA SOURCE. Every pattern and every allowlist entry is read at runtime
# from open-source/IDENTIFIER-RULES.txt — the SAME file export.sh's rewrite
# pass and open-source/checks/identifier-gate.sh read (D#1837). No pattern
# literal is duplicated into this script or into preflight-common.sh. That
# is the point: a copied pattern list drifts, and a gate checking a stale
# copy of the rules is worse than no gate.
#
# SCOPE — ADDED LINES, NOT THE WHOLE TREE. This scans lines the working
# tree ADDS relative to a base ref, not every tracked file. Two reasons,
# both measured rather than assumed (host nixos, tracked tree at b3aeab52):
#
#   1. It matches the threat. The failure this guards against is an
#      executor writing a forbidden identifier into a commit. Scanning
#      added lines catches exactly that, and does not block a PR that
#      touches a file which already contained one for unrelated reasons.
#   2. The whole tree is not clean, and this PR cannot make it clean.
#      Outside archive/ and open-source/, the proprietary-project pattern
#      still matches 26 tracked files (backend/, dashboard/, scripts/,
#      wiki/) — docstring and comment examples that export.sh's rewrite
#      pass fixed on the way out. D#2348 PR-b fixed the `tests/` half; the
#      rest is a follow-up remediation, not this gate's job. A whole-tree
#      scan here would be red on every run from the day it landed, and a
#      permanently-red gate gets disabled.
#
# ENFORCED SUBSET. Not every FORBIDDEN_PATTERNS entry is enforced pre-push.
# The rules file carries a PREPUSH_EXEMPT block naming the ones that are
# not, each with a written reason — and the default is ENFORCE: a pattern
# added to FORBIDDEN_PATTERNS is enforced here automatically unless someone
# explicitly exempts it. Every exempt line must match a FORBIDDEN_PATTERNS
# line verbatim, so editing a pattern there without editing the exemption
# is a hard failure rather than a silently stale exemption.
#
# Usage:
#   bash scripts/check-forbidden-identifiers.sh [--base <ref>]
#   bash scripts/check-forbidden-identifiers.sh --list-patterns
#
# Exit 0 = clean: at least one pattern was parsed and applied, every
#          allowlist entry resolved and is non-stale, and no added line
#          carries an unallowlisted hit.
# Exit 1 = one or more unallowlisted hits, a stale/malformed allowlist
#          entry, a malformed or stale exemption, an unresolved {TOKEN}, a
#          pattern that errored during the scan, or a rules file that
#          yielded zero enforceable patterns. That last case is a hard
#          failure specifically so this script can never print PASS having
#          checked nothing (same rule as identifier-gate.sh, D#1844).
# Exit 2 = usage/argument error, or the rules file is absent. The caller
#          decides whether an absent rules file is export shape (skip) or
#          rot (hard fail) — this script does not guess.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RULES_FILE="$REPO_ROOT/open-source/IDENTIFIER-RULES.txt"

BASE_REF=""
LIST_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      BASE_REF="${2:-}"
      if [[ -z "$BASE_REF" ]]; then
        echo "error: --base requires a ref" >&2
        exit 2
      fi
      shift 2
      ;;
    --list-patterns)
      LIST_ONLY=1
      shift
      ;;
    -h|--help)
      sed -n '/^# Usage:/,/^# Exit 2/p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$RULES_FILE" ]]; then
  echo "error: rules file not found: $RULES_FILE" >&2
  exit 2
fi

# Paths that never reach a public push, so a hit inside them is not a leak
# and scanning them produces only false blocks.
#
# This is a SCAN scope, not the ship list, and the two are no longer the
# same thing. MANIFEST.md describes what the *export* shipped; after the
# cutover the tree itself is the artifact, and the authoritative push set is
# the owner decision recorded on D#2348 ("Owner decision — PR-l's exclusion
# set, now explicit"). wiki/ below is settled by that decision — it does not
# ship. It is NOT the "explicitly undecided" item the Discussion body lists;
# that question was answered after the body was written, and citing it as
# open would ship a pointer to something already closed.
#
# The push set also excludes dashboard_tui/, docker/, systemd/,
# verification-report/ and templates/, which are deliberately absent here.
# Measured 2026-09-04 (host nixos): none of those five, nor engine/,
# testsupport/, or the three scripts/ carve-outs below, contains a single
# hit for any of the three patterns this gate enforces. Listing them would
# change no outcome, so they are left out rather than added as inert
# entries that would then need maintaining. wiki/ is the only one of the
# newly-named exclusions that matters — it holds 5 files with hits.
#
# archive/ earns its place for a second reason: the Archive Protocol moves
# whole files under it, and a git-mv'd file reads as wholly-added in a -U0
# diff, so without this an archive move of anything containing an
# identifier would fail the gate for doing exactly what the protocol
# requires.
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
  # *.env* — credentials, per MANIFEST.md.
  [[ "$(basename -- "$p")" == *.env* ]] && return 0
  return 1
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Markers are matched by EXACT equality on the trimmed line, never by
# substring — a substring test matches the rules file's own prose about the
# markers and reopens the wrong block early, which was a live bug
# (identifier-gate.sh header, D#1844 security review, error 2).
parse_block() {
  local section="$1" in_block=0 line t
  while IFS= read -r line; do
    t="$(trim "$line")"
    if [[ "$t" == "=== ${section}_START ===" ]]; then in_block=1; continue; fi
    if [[ "$t" == "=== ${section}_END ===" ]]; then in_block=0; continue; fi
    [[ "$in_block" -eq 1 ]] || continue
    [[ -z "$t" || "$t" == \#* ]] && continue
    printf '%s\n' "$t"
  done < "$RULES_FILE"
}

HITS=0

# --- IDENTITIES ---
declare -A IDENTITIES
while IFS= read -r t; do
  key="${t%%=*}"
  value="${t#*=}"
  if [[ -z "$key" || "$key" == "$t" ]]; then
    echo "FAIL: malformed IDENTITIES entry (need key=value): $t"
    HITS=$((HITS + 1))
    continue
  fi
  IDENTITIES["$key"]="$value"
done < <(parse_block IDENTITIES)

fill_tokens() {
  local text="$1" key
  for key in "${!IDENTITIES[@]}"; do
    text="${text//\{$key\}/${IDENTITIES[$key]}}"
  done
  printf '%s' "$text"
}

# --- FORBIDDEN_PATTERNS (raw + filled, kept index-aligned) ---
RAW_PATTERNS=()
ALL_PATTERNS=()
while IFS= read -r t; do
  filled="$(fill_tokens "$t")"
  if [[ "$filled" == *"{"*"}"* ]]; then
    echo "FAIL: forbidden pattern still contains an unresolved {TOKEN} after substitution — refusing to run it as a regex: $filled"
    HITS=$((HITS + 1))
    continue
  fi
  RAW_PATTERNS+=("$t")
  ALL_PATTERNS+=("$filled")
done < <(parse_block FORBIDDEN_PATTERNS)

if [[ "${#ALL_PATTERNS[@]}" -eq 0 ]]; then
  echo "FAIL: zero forbidden patterns parsed from $RULES_FILE — refusing to report a vacuous PASS"
  exit 1
fi

# --- PREPUSH_EXEMPT ---
# Tab-separated `pattern<TAB>reason`. A line with no tab is a hard failure
# rather than an exemption with no stated reason (the same rule export.sh
# applies to REWRITE lines). A pattern here that is not a
# FORBIDDEN_PATTERNS line verbatim is a hard failure: that coupling is what
# stops an exemption from silently outliving the pattern it exempts.
declare -A EXEMPT_RAW
while IFS= read -r t; do
  if [[ "$t" != *$'\t'* ]]; then
    echo "FAIL: PREPUSH_EXEMPT entry has no tab (need pattern<TAB>reason) — refusing to treat it as an exemption with no stated reason: $t"
    HITS=$((HITS + 1))
    continue
  fi
  ex_pattern="${t%%$'\t'*}"
  ex_reason="$(trim "${t#*$'\t'}")"
  if [[ -z "$ex_pattern" || -z "$ex_reason" ]]; then
    echo "FAIL: malformed PREPUSH_EXEMPT entry (need pattern<TAB>reason, both non-empty): $t"
    HITS=$((HITS + 1))
    continue
  fi
  found=0
  for raw in "${RAW_PATTERNS[@]}"; do
    [[ "$raw" == "$ex_pattern" ]] && { found=1; break; }
  done
  if [[ "$found" -eq 0 ]]; then
    echo "FAIL: PREPUSH_EXEMPT names a pattern that is not in FORBIDDEN_PATTERNS verbatim (stale exemption — the pattern was edited or removed without updating this entry): $ex_pattern"
    HITS=$((HITS + 1))
    continue
  fi
  EXEMPT_RAW["$ex_pattern"]=1
done < <(parse_block PREPUSH_EXEMPT)

# Enforced = every forbidden pattern that is not explicitly exempt.
# Default-enforce: a NEW pattern added to FORBIDDEN_PATTERNS is enforced
# here with no registration step.
PATTERNS=()
for i in "${!RAW_PATTERNS[@]}"; do
  [[ -n "${EXEMPT_RAW[${RAW_PATTERNS[$i]}]+x}" ]] && continue
  PATTERNS+=("${ALL_PATTERNS[$i]}")
done

if [[ "$LIST_ONLY" -eq 1 ]]; then
  [[ "$HITS" -gt 0 ]] && exit 1
  printf '%s\n' "${PATTERNS[@]}"
  exit 0
fi

if [[ "${#PATTERNS[@]}" -eq 0 ]]; then
  echo "FAIL: every forbidden pattern is exempt from the pre-push scan — this gate would check nothing, which is worse than not existing"
  exit 1
fi

# --- ALLOWLIST ---
# path:anchor:reason, content-anchored (D#2186). The five fail-closed rules
# are preserved from identifier-gate.sh: exactly 2 colons, no purely-numeric
# anchor, path must exist, anchor must match exactly one line, and the
# resolved line must still match a forbidden pattern (stale cover is a
# FAIL, not a silent drop).
#
# One adaptation, stated rather than hidden: identifier-gate.sh proves rule
# 5 as a side effect of its whole-tree scan (an entry never "seen" during
# the scan is stale). This scan is diff-scoped, so an allowlisted line
# normally is not in the diff at all and that coupling would report every
# entry as stale. Rule 5 is therefore checked DIRECTLY against the resolved
# line here. Same rule, checked head-on instead of inferred.
declare -A ALLOWLIST_KEY
ALLOWLIST_COUNT=0
while IFS= read -r t; do
  colon_count="$(grep -o ':' <<<"$t" | wc -l)"
  if [[ "$colon_count" -ne 2 ]]; then
    echo "FAIL: allowlist entry has $colon_count colon(s), need exactly 2 (path:anchor:reason): $t"
    HITS=$((HITS + 1))
    continue
  fi
  path="${t%%:*}"
  rest="${t#*:}"
  anchor="${rest%%:*}"
  reason="${rest#*:}"
  if [[ -z "$path" || -z "$anchor" || -z "$reason" || "$reason" == "$rest" ]]; then
    echo "FAIL: malformed allowlist entry (need path:anchor:reason): $t"
    HITS=$((HITS + 1))
    continue
  fi
  if [[ "$anchor" =~ ^[0-9]+$ ]]; then
    echo "FAIL: allowlist anchor '$anchor' is purely numeric — that is a re-armed line-pin, not a content anchor: $t"
    HITS=$((HITS + 1))
    continue
  fi
  anchor_file="$REPO_ROOT/$path"
  if [[ ! -f "$anchor_file" ]]; then
    echo "FAIL: allowlist entry references a path not found in the tree: $path"
    HITS=$((HITS + 1))
    continue
  fi
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
  resolved_text="${anchor_lines[0]#*:}"
  still_forbidden=0
  for p in "${ALL_PATTERNS[@]}"; do
    if grep -qE -- "$p" <<<"$resolved_text" 2>/dev/null; then
      still_forbidden=1
      break
    fi
  done
  if [[ "$still_forbidden" -eq 0 ]]; then
    echo "FAIL: stale allowlist entry '$path:$lineno' — the anchored line no longer matches a forbidden pattern"
    HITS=$((HITS + 1))
    continue
  fi
  ALLOWLIST_KEY["$path:$lineno"]=1
  ALLOWLIST_COUNT=$((ALLOWLIST_COUNT + 1))
done < <(parse_block ALLOWLIST)

# --- Resolve the diff base ---
# Same resolution order as preflight-common.sh's get_changed_files: an
# unresolvable base is a hard failure, never an empty diff reported as
# "nothing changed".
#
# The validation below is deliberately OUTSIDE the auto-resolve branch, and
# that placement is the whole point. When it only guarded the auto-resolve
# path, `--base <nonexistent-ref>` sailed past it: `git diff` printed
# nothing to stdout, its error went to /dev/null, and the scan reported
# `PASS ... files=0 added_lines=0` with a real forbidden identifier sitting
# in a tracked file. A gate reporting success having scanned nothing is
# precisely the defect this gate exists to prevent, and it does not stop
# being that defect when it is the gate's own. run_always_gates() passes no
# arguments and so never hit it — but a CI job naturally passes an explicit
# base, which is exactly how the next phase of this work will call it.
if [[ -z "$BASE_REF" ]]; then
  if git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/main^{commit}" >/dev/null 2>&1; then
    BASE_REF="origin/main"
  elif git -C "$REPO_ROOT" rev-parse --verify --quiet "HEAD~1^{commit}" >/dev/null 2>&1; then
    BASE_REF="HEAD~1"
  else
    echo "FAIL: could not resolve a diff base — neither 'origin/main' nor 'HEAD~1' exists in this checkout. Refusing to report an empty diff as \"no lines added\"; in CI this usually means the checkout needs a deeper fetch."
    exit 1
  fi
fi

if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null 2>&1; then
  echo "FAIL: diff base '$BASE_REF' does not resolve to a commit in this checkout. Refusing to report an empty diff as \"no lines added\" — a scan with an unresolvable base reads exactly like a clean tree."
  exit 1
fi

# --- Collect added lines from the diff ---
# `git diff -U0 <base>` compares the WORKING TREE against the base, so an
# identifier that is written but not yet committed is caught too — which is
# the point of a gate an executor runs mid-task, before any push.
#
# --no-renames is deliberate: a rename recorded as a rename shows no added
# content, which would let an archive-shaped move of a file smuggle
# identifiers past the scan under a different path. Excluded paths are
# dropped by rewriting their file header to /dev/null in the filter below,
# so archive/ moves stay out of scope on their destination path, explicitly.
HIT_PATHS=()
HIT_LINES=()
CONTENT_FILE="$(mktemp)"
GREP_OUT="$(mktemp)"
GREP_ERR="$(mktemp)"
cleanup() { rm -f "$CONTENT_FILE" "$GREP_OUT" "$GREP_ERR"; }
trap cleanup EXIT

cur_file=""
cur_line=0
in_hunks=0
scanned_files=0
declare -A seen_files
while IFS= read -r line; do
  # `in_hunks` disambiguates a file header from content. An ADDED line whose
  # own text starts with "++" arrives as "+++ b/..." and is indistinguishable
  # from a file header by prefix alone. Headers only ever appear before the
  # first @@ of a file, so once hunks have started, "+++" is content.
  case "$line" in
    "diff --git "*)
      cur_file=""
      cur_line=0
      in_hunks=0
      continue
      ;;
  esac
  if [[ "$in_hunks" -eq 0 ]]; then
    case "$line" in
      "+++ b/"*)
        cur_file="${line#+++ b/}"
        if is_excluded_path "$cur_file"; then
          cur_file=""
          continue
        fi
        if [[ -z "${seen_files[$cur_file]+x}" ]]; then
          seen_files["$cur_file"]=1
          scanned_files=$((scanned_files + 1))
        fi
        continue
        ;;
      "+++ /dev/null")
        cur_file=""
        continue
        ;;
    esac
  fi
  case "$line" in
    "@@"*)
      # @@ -a,b +c,d @@ — c is the first new-file line number of the hunk.
      in_hunks=1
      hunk="${line#*+}"
      hunk="${hunk%% *}"
      cur_line="${hunk%%,*}"
      ;;
    "+"*)
      [[ -z "$cur_file" ]] && continue
      HIT_PATHS+=("$cur_file")
      HIT_LINES+=("$cur_line")
      printf '%s\n' "${line:1}" >> "$CONTENT_FILE"
      cur_line=$((cur_line + 1))
      ;;
  esac
done < <(git -C "$REPO_ROOT" diff -U0 --no-color --no-ext-diff --no-renames "$BASE_REF" -- . 2>/dev/null)

# Untracked files are absent from `git diff` entirely, so a brand-new file
# carrying a forbidden identifier would read as clean right up until the
# `git add` — which is the worst possible moment for this gate to be
# quiet, since an executor runs preflight while still writing. Every line
# of an untracked file is an added line, so they are scanned whole.
# --exclude-standard honours .gitignore, so build output and node_modules
# stay out. Read-only: no `git add -N`, nothing here touches the index.
while IFS= read -r untracked; do
  [[ -z "$untracked" ]] && continue
  is_excluded_path "$untracked" && continue
  # -I makes grep report nothing for binary files, so they are skipped.
  [[ -n "$(grep -Ilm1 '' -- "$REPO_ROOT/$untracked" 2>/dev/null)" ]] || continue
  if [[ -z "${seen_files[$untracked]+x}" ]]; then
    seen_files["$untracked"]=1
    scanned_files=$((scanned_files + 1))
  fi
  n=0
  while IFS= read -r content || [[ -n "$content" ]]; do
    n=$((n + 1))
    HIT_PATHS+=("$untracked")
    HIT_LINES+=("$n")
    printf '%s\n' "$content" >> "$CONTENT_FILE"
  done < "$REPO_ROOT/$untracked"
done < <(git -C "$REPO_ROOT" ls-files --others --exclude-standard 2>/dev/null)

ADDED_LINES="${#HIT_PATHS[@]}"
SUMMARY="base=$BASE_REF patterns=${#PATTERNS[@]}/${#ALL_PATTERNS[@]} allowlist=$ALLOWLIST_COUNT files=$scanned_files added_lines=$ADDED_LINES"

if [[ "$ADDED_LINES" -gt 0 ]]; then
  for pattern in "${PATTERNS[@]}"; do
    # grep into a file rather than a pipeline so its real exit status is
    # reachable: 0 = matches, 1 = none, 2+ = the pattern itself errored.
    # Swallowing 2+ means a broken pattern checks nothing and nobody can
    # tell (D#1844 security review, warning 4).
    grep -nE -- "$pattern" "$CONTENT_FILE" >"$GREP_OUT" 2>"$GREP_ERR"
    grep_rc=$?
    if [[ "$grep_rc" -ge 2 ]]; then
      echo "FAIL: pattern errored during scan (grep exit $grep_rc), treating as a hard failure rather than a silent skip: $pattern"
      [[ -s "$GREP_ERR" ]] && sed 's/^/       grep stderr: /' "$GREP_ERR"
      HITS=$((HITS + 1))
      continue
    fi
    while IFS= read -r out; do
      [[ -z "$out" ]] && continue
      idx="${out%%:*}"
      idx=$((idx - 1))
      hit_path="${HIT_PATHS[$idx]}"
      hit_line="${HIT_LINES[$idx]}"
      if [[ -n "${ALLOWLIST_KEY[$hit_path:$hit_line]+x}" ]]; then
        continue
      fi
      echo "FAIL: forbidden identifier added at $hit_path:$hit_line [pattern: $pattern]"
      HITS=$((HITS + 1))
    done < "$GREP_OUT"
  done
fi

if [[ "$HITS" -gt 0 ]]; then
  echo "FAIL ($SUMMARY)"
  echo "  This is a pre-push guardrail: once development is public, a commit carrying one of these identifiers is world-readable the moment it lands, and force-push does not recall it. Fix the line at source — open-source/IDENTIFIER-RULES.txt's IDENTITIES block names the replacement. An allowlist entry is not the remedy."
  exit 1
fi
echo "PASS ($SUMMARY)"
exit 0
