#!/usr/bin/env bash
# scripts/ci/dangling-doc-commands.sh — scans the published docs (README.md,
# CONTRIBUTING.md) for interpreter invocations (`bash <path>`, `sh <path>`,
# `python <path>`, `python3 <path>`, `node <path>`) and direct executions
# (`./<path>`) that reference a file the tree does not contain — in fenced
# code blocks (both ``` and ~~~) *and* inline code spans.
#
# Ported from open-source/checks/dangling-doc-commands.sh (D#2348 PR-i). The
# original still exists and still runs from open-source/verify-export.sh
# against a produced export tree; this one defaults to the repository
# itself, because once development happens in the public repo the
# repository IS the tree a reader clones and follows.
#
# This exists because a doc can reference a real file that the published
# tree does not carry — invisible to whoever wrote the doc (the file is
# right there in their checkout) and only bites the reader following it.
# See D#1824.
#
# ---------------------------------------------------------------------------
# ONE NARROWING IN THE PORT, and it is not cosmetic. The original also
# scanned wiki/*.md when a wiki/ directory was present. On an export tree
# that branch never fired — wiki/ left the export manifest in PR #1867
# (2026-08-17) — so the original's own header already recorded that "the
# guard's real document surface today is just README.md + CONTRIBUTING.md".
# Pointed at the SOURCE tree, where wiki/ is right there, that dormant
# branch wakes up: measured 2026-09-04 on this host at dc302299, running the
# original against `.` produced 51 findings, all 51 from wiki/, and none
# from README.md or CONTRIBUTING.md.
#
# Those 51 are not 51 defects. wiki/ is outside the public push set (D#2348
# owner decision, 2026-09-04), so nothing in it is a published dangling
# reference; and most of the hits are the detector's own heuristic misfiring
# on prose-dense docs, which take the token after an interpreter as a path
# and so flag things like '&&', '3', and '(handles'. Carrying that branch
# forward would have landed a gate that is red on arrival, and a
# permanently-red gate gets disabled.
#
# So the document set here is README.md + CONTRIBUTING.md, explicitly, and
# the scope line below still prints exactly what was scanned. wiki/ is
# checked by scripts/wiki-linkcheck.sh, which is built for it.
# ---------------------------------------------------------------------------
#
# History: this guard originally flagged only BARE ROOT-LEVEL references (no
# directory component), on the theory that a directory-component reference
# (`scripts/foo.sh`) ships or doesn't ship as a whole with that directory —
# a manifest question, not this defect class — and that checking it would
# also flag placeholder example paths like `/path/to/myproject/backend/x.py`.
# That narrowing existed because `wiki/` was ~60 docs deep at the time and
# widening without it was noisy. A maximal variant (all five interpreters,
# direct-exec, inline spans, both fence styles, any path depth) measured
# against the export on 2026-08-20 returned exactly two findings, both the
# same real defect and zero noise. The constraint that justified the
# narrowing was measured away, not overlooked — see D#1831.
#
# Still out of scope, by design (not defects, not silently missed):
#   - a reference wrapped in quotes or containing a shell variable — not a
#     literal, checkable path (e.g. `bash "$SCRIPT"`).
#   - 4-space indented code blocks and HTML <pre> blocks — no shipped doc
#     uses either form today; adding detection for hypothetical future use
#     is speculative, not a fix for a live defect (see tests/test_dangling_
#     doc_commands.sh, which declares both misses with this reason).
#
# Usage: bash scripts/ci/dangling-doc-commands.sh [target-dir]  (default: repo root)
#
# Exit 0 = no dangling references found (scope line reports what was checked).
# Exit 1 = one or more findings (each printed as a "FAIL: ..." line).
# Exit 2 = usage/argument error, OR the document list came out empty — a
#          guard that scanned nothing must not report success (D#1831).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "error: target dir not found: $TARGET_DIR" >&2
  exit 2
fi

INTERPRETERS='bash|sh|python3|python|node'

DOCS=()
[[ -f "$TARGET_DIR/README.md" ]] && DOCS+=("$TARGET_DIR/README.md")
[[ -f "$TARGET_DIR/CONTRIBUTING.md" ]] && DOCS+=("$TARGET_DIR/CONTRIBUTING.md")

if [[ "${#DOCS[@]}" -eq 0 ]]; then
  echo "error: no documents found to scan (no README.md, no CONTRIBUTING.md under $TARGET_DIR) — a guard that scanned nothing cannot report success" >&2
  exit 2
fi

FINDINGS=0
declare -A SEEN

# scan_text <rel-path> <lineno> <text> — finds interpreter invocations and
# direct-exec references in $text and reports any whose target is missing
# from the export. Shared by fenced-block lines (raw line text) and inline
# spans (backtick-stripped span text), which is how the same detection
# covers both shapes without duplicating the filter/report logic.
scan_text() {
  local rel="$1" lineno="$2" text="$3"
  local match cmd rest tok ref found

  # Interpreter-prefixed: "<bash|sh|python|python3|node> [flags...] <path>".
  # Greedy match to end of text/span; we only need the first invocation on
  # a given line/span, which is all any planted shape ever puts there.
  while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    read -r cmd rest <<< "$match"
    ref=""
    for tok in $rest; do
      case "$tok" in
        -*) continue ;;   # skip leading flags (item 7: `bash -x foo.sh`)
        *) ref="$tok"; break ;;
      esac
    done
    [[ -z "$ref" ]] && continue
    # Quoted or variable arguments aren't a literal, checkable path —
    # declared out of scope (see header).
    case "$ref" in
      *\"*|*\'*|*\$*) continue ;;
    esac
    found="${rel}:${lineno}:${ref}"
    [[ -n "${SEEN[$found]:-}" ]] && continue
    if [[ ! -e "$TARGET_DIR/$ref" ]]; then
      echo "FAIL: $rel:$lineno references '$ref' (via '$cmd') — not present in $TARGET_DIR"
      FINDINGS=$((FINDINGS + 1))
      SEEN[$found]=1
    fi
  done < <(grep -oE "\\<(${INTERPRETERS})\\>[[:space:]]+.*" <<< "$text" 2>/dev/null)

  # Direct execution: "./<path>" with no interpreter prefix (item 5/v08).
  while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    ref="$(sed -E 's/^[[:space:]]+//' <<< "$match")"
    case "$ref" in
      *\"*|*\'*|*\$*) continue ;;
    esac
    found="${rel}:${lineno}:${ref}"
    [[ -n "${SEEN[$found]:-}" ]] && continue
    if [[ ! -e "$TARGET_DIR/$ref" ]]; then
      echo "FAIL: $rel:$lineno references '$ref' (via './') — not present in $TARGET_DIR"
      FINDINGS=$((FINDINGS + 1))
      SEEN[$found]=1
    fi
  done < <(grep -oE '(^|[[:space:]])\./[^[:space:]`'"'"'"]+' <<< "$text" 2>/dev/null)
}

for doc in "${DOCS[@]}"; do
  in_fence=0
  lineno=0
  rel="${doc#"$TARGET_DIR"/}"
  while IFS= read -r line; do
    lineno=$((lineno + 1))

    # Both ``` and ~~~ toggle fence state (item 6).
    if [[ "$line" == '```'* || "$line" == '~~~'* ]]; then
      if [[ "$in_fence" -eq 0 ]]; then
        in_fence=1
      else
        in_fence=0
      fi
      continue
    fi

    if [[ "$in_fence" -eq 1 ]]; then
      scan_text "$rel" "$lineno" "$line"
      continue
    fi

    # Outside a fence: scan inline code spans only (item 3). Prose text
    # outside backticks is never a runnable-command claim.
    while IFS= read -r span; do
      [[ -z "$span" ]] && continue
      # Strip the surrounding backticks.
      scan_text "$rel" "$lineno" "${span:1:-1}"
    done < <(grep -oE '`[^`]+`' <<< "$line" 2>/dev/null)
  done < "$doc"
done

DOC_NAMES=""
for doc in "${DOCS[@]}"; do
  DOC_NAMES="${DOC_NAMES}${DOC_NAMES:+, }${doc#"$TARGET_DIR"/}"
done

# Scope line — printed on success AND failure (D#1831 item 1). A green run
# only ever meant "no bare root-level bash/python3 reference in a fence, in
# one of two documents, points at a missing file"; this line is what makes
# the actual, narrower scope of a green run legible without opening the
# script.
echo "dangling-doc-commands: scanned ${#DOCS[@]} doc(s) [${DOC_NAMES}] for ${INTERPRETERS//|/, } and ./ direct-exec, in fenced (\`\`\` and ~~~) and inline code spans; ${FINDINGS} finding(s)"

if [[ "$FINDINGS" -gt 0 ]]; then
  exit 1
fi
exit 0
