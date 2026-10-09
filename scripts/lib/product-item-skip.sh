#!/usr/bin/env bash
# scripts/lib/product-item-skip.sh — keep the internal loop off product-owned issues.
#
# An issue carrying the label `fulcrumaxe:product` belongs to the product
# pipeline, which builds it on its own runner and leaves the merge to a person.
# If the loop also picked it up, the same item would be worked twice, so the
# loop's issue scan drops every issue with that label, whoever applied it.
# Trusting only maintainers' labels would be the unsafe direction: an issue
# labelled by someone without maintain access would then be ignored by the
# product side and worked by the loop. Skipping on the label alone makes both
# sides leave it alone, so the failure mode is "nobody works it", never "two do".
#
# Usage:
#   gh issue list --state open --json number,title,labels \
#     | bash scripts/lib/product-item-skip.sh [--log-issue N --repo OWNER/NAME]
#
# stdin   JSON array of issues; each has "number" and "labels" (a list of
#         label names or of {"name": ...} objects, as `gh --json labels` gives).
# stdout  the same array minus the product items. Every other field is kept.
# stderr  one "skipped: product item #N" line per dropped issue.
# --log-issue N --repo R  also posts that line, timestamped, as a comment on
#         the team-log issue N. A failed post is reported on stderr and does not
#         stop the scan: logging must not decide what gets routed.
#
# Exit 0 on success. Exit 2 if stdin is not a JSON array: an unreadable list is
# never treated as "nothing to skip", so the caller does not route on it.

set -euo pipefail

PRODUCT_LABEL="fulcrumaxe:product"

LOG_ISSUE=""
LOG_REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log-issue) LOG_ISSUE="${2:?--log-issue needs a number}"; shift 2 ;;
    --repo)      LOG_REPO="${2:?--repo needs OWNER/NAME}"; shift 2 ;;
    *) echo "product-item-skip: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$LOG_ISSUE" && -z "$LOG_REPO" ]]; then
  echo "product-item-skip: --log-issue needs --repo (a bare gh call resolves the repo from the checkout)" >&2
  exit 2
fi

_TMP_OUT="$(mktemp)"
_TMP_SKIPPED="$(mktemp)"
trap 'rm -f "$_TMP_OUT" "$_TMP_SKIPPED"' EXIT

# Case-insensitive on the label: GitHub treats label names as case-insensitive,
# and a miss here is the unsafe direction.
if ! python3 -c '
import json, sys
label = sys.argv[1].lower()
out_path, skipped_path = sys.argv[2], sys.argv[3]
try:
    issues = json.load(sys.stdin)
    if not isinstance(issues, list):
        raise ValueError("not a list")
except Exception as exc:
    print("product-item-skip: stdin is not a JSON array of issues: %s" % exc, file=sys.stderr)
    sys.exit(2)

def names(issue):
    for lab in issue.get("labels") or []:
        yield (lab.get("name") if isinstance(lab, dict) else lab) or ""

kept, skipped = [], []
for issue in issues:
    if any(str(n).lower() == label for n in names(issue)):
        skipped.append(issue.get("number"))
    else:
        kept.append(issue)
json.dump(kept, open(out_path, "w"))
open(skipped_path, "w").write("".join("%s\n" % n for n in skipped))
' "$PRODUCT_LABEL" "$_TMP_OUT" "$_TMP_SKIPPED"; then
  exit 2
fi

while IFS= read -r _n; do
  [[ -z "$_n" ]] && continue
  _line="skipped: product item #${_n}"
  echo "$_line" >&2
  if [[ -n "$LOG_ISSUE" ]]; then
    gh issue comment "$LOG_ISSUE" --repo "$LOG_REPO" \
      --body "[$(date +%H:%M)] loop: ${_line}" >/dev/null \
      || echo "product-item-skip: could not post the team-log line for #${_n}" >&2
  fi
done < "$_TMP_SKIPPED"

cat "$_TMP_OUT"
echo
