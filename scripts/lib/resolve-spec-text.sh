#!/usr/bin/env bash
# scripts/lib/resolve-spec-text.sh — Spec text resolver (D#2008).
#
# A frozen Spec's verification substance sometimes lives in the Discussion
# body (the normal case) and sometimes in a single linked comment instead —
# D#1944's shape, where the body's own STATUS line moved on to DONE while the
# authoritative Spec text stayed pinned in the comment that froze it
# (`...#discussioncomment-<id>`). A reviewer template that reads only
# `discussion(number:N){body}` (this repo's Stage-2 gate did, before D#2008)
# silently misses that second shape and treats the PR as though its Spec has
# no verification content at all, even when the pinned comment plainly does.
# That is why PR #1995 deadlocked: hand-verified — its Discussion's body
# carries no `## Spec (Acceptance)` heading; comment 18074578 does.
#
# This file does the ONLY network calls this resolution needs (one GraphQL
# call per comment page — see pagination note below), so
# backend/spec_verification_substance.py's matcher stays pure and
# offline-testable. Sourceable, following the scripts/lib/spec-ready-gate.sh
# pattern established at D#1798: spawn-agent.sh is on the PreToolUse
# forbidden-command list and cannot be executed to test this end to end, so
# this file defines a function a shell test can source and call directly
# instead of re-implementing (paraphrasing) the logic.
#
# Pagination (D#2008 code review, second round): a Discussion can carry more
# than 100 comments, and the frozen-Spec comment this resolver looks for can
# sit past that first page — the same silent-degrade-to-body-only shape AC4
# exists to close, just triggered by comment count instead of comment
# location. This function pages through all of them (100 at a time) rather
# than reading only the first page. If a Discussion somehow exceeds
# _RESOLVE_SPEC_TEXT_MAX_PAGES pages (20 pages = 2000 comments — no
# Discussion in this repo is remotely close), it stops and prints a loud
# warning to stderr instead of silently truncating; degrading silently is
# exactly what this pagination fix exists to avoid.
#
# Usage (source, then call):
#   source "$REPO_ROOT/scripts/lib/resolve-spec-text.sh"
#   TEXT=$(resolve_spec_text "$DISCUSSION_NUMBER")
#
# Or run directly:
#   bash scripts/lib/resolve-spec-text.sh <discussion_number>
#
# Output (stdout): the Discussion body. When the body links a frozen-Spec
# comment (a "#discussioncomment-<id>" URL fragment) within its first 10
# lines — anywhere later is prose referencing something else, not the
# authoritative freeze pointer — that comment's body is appended after a
# blank line and a `<!-- SPEC_TEXT_FROM_COMMENT:<id> -->` marker line, so a
# caller that cares about provenance (spec_verification_substance.py's
# `check --discussion` path, which tags results with a `spec_in_comment`
# flag) can find the boundary, while a plain grep/awk caller just sees one
# contiguous text.

_RESOLVE_SPEC_TEXT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_RESOLVE_SPEC_TEXT_REPO_ROOT="${REPO_ROOT:-$(cd "$_RESOLVE_SPEC_TEXT_DIR/../.." && pwd)}"
_RESOLVE_SPEC_TEXT_MAX_PAGES=20

resolve_spec_text() {
  local disc_num="$1"
  if [[ -z "$disc_num" ]]; then
    echo "resolve_spec_text: usage: resolve_spec_text <discussion_number>" >&2
    return 1
  fi
  # Defense in depth (security review, D#2008): $disc_num is interpolated
  # directly into the GraphQL query text below. No untrusted caller exists
  # today -- every caller passes a Discussion number it already validated
  # -- but a numeric guard here costs one line and closes the class rather
  # than trusting every future caller to keep validating upstream.
  if [[ ! "$disc_num" =~ ^[0-9]+$ ]]; then
    echo "resolve_spec_text: discussion number must be numeric, got: $disc_num" >&2
    return 1
  fi

  # shellcheck source=scripts/lib/repo-resolve.sh
  source "$_RESOLVE_SPEC_TEXT_DIR/repo-resolve.sh"
  local repo owner name
  repo="$(_resolve_repo)" || return 1
  owner="${repo%%/*}"
  name="${repo##*/}"

  # Page through every comment (100 at a time) rather than reading only the
  # first page — the linked frozen-Spec comment this resolver looks for can
  # sit past comment #100 on a long-running Discussion, which would
  # otherwise silently degrade to the same body-only failure AC4 exists to
  # close. Each page's raw GraphQL response is appended as one line to
  # pages_file; the final merge (body + all comment nodes + the freeze-link
  # resolution) happens in a single python pass over that file.
  local pages_file
  pages_file=$(mktemp) || return 1
  # shellcheck disable=SC2064
  trap "rm -f '$pages_file'" RETURN

  local cursor="" after_clause="" query raw page_info
  local has_next="true"
  local page=0
  local truncated="false"

  while [[ "$has_next" == "true" ]]; do
    if [[ $page -ge $_RESOLVE_SPEC_TEXT_MAX_PAGES ]]; then
      echo "resolve_spec_text: discussion #$disc_num has more than $((_RESOLVE_SPEC_TEXT_MAX_PAGES * 100)) comments — stopping after page $page rather than looping forever. The frozen-Spec comment may be past this point and silently missed; investigate manually if classification looks wrong." >&2
      truncated="true"
      break
    fi

    after_clause=""
    if [[ -n "$cursor" ]]; then
      after_clause=", after:\"$cursor\""
    fi
    query="query { repository(owner:\"$owner\", name:\"$name\") { discussion(number:$disc_num) { body comments(first:100${after_clause}) { pageInfo { hasNextPage endCursor } nodes { databaseId body } } } } }"

    raw=$(gh api graphql -f query="$query" 2>/dev/null)
    if [[ -z "$raw" ]]; then
      echo "resolve_spec_text: gh api graphql returned nothing for discussion #$disc_num in $repo (page $page)" >&2
      return 1
    fi
    echo "$raw" >> "$pages_file"

    page_info=$(RESOLVE_SPEC_TEXT_PAGE_JSON="$raw" python3 -c '
import json
import os
import sys

raw = os.environ["RESOLVE_SPEC_TEXT_PAGE_JSON"]
try:
    data = json.loads(raw)
    disc = data["data"]["repository"]["discussion"]
except (json.JSONDecodeError, TypeError, KeyError):
    print("PARSE_ERROR")
    sys.exit(0)
if disc is None:
    print("NOT_FOUND")
    sys.exit(0)
page_info = disc.get("comments", {}).get("pageInfo") or {}
tag = "NEXT" if page_info.get("hasNextPage") else "STOP"
print(tag + "\t" + (page_info.get("endCursor") or ""))
')
    case "$page_info" in
      PARSE_ERROR)
        echo "resolve_spec_text: could not parse discussion #$disc_num (page $page)" >&2
        return 1
        ;;
      NOT_FOUND)
        echo "resolve_spec_text: discussion #$disc_num not found" >&2
        return 1
        ;;
      NEXT*)
        has_next="true"
        cursor="${page_info#NEXT$'\t'}"
        ;;
      *)
        has_next="false"
        ;;
    esac
    page=$((page + 1))
  done

  RESOLVE_SPEC_TEXT_PAGES_FILE="$pages_file" RESOLVE_SPEC_TEXT_DISC_NUM="$disc_num" RESOLVE_SPEC_TEXT_TRUNCATED="$truncated" python3 -c '
import json
import os
import re
import sys

disc_num = os.environ["RESOLVE_SPEC_TEXT_DISC_NUM"]
pages_file = os.environ["RESOLVE_SPEC_TEXT_PAGES_FILE"]
truncated = os.environ["RESOLVE_SPEC_TEXT_TRUNCATED"] == "true"

body = None
all_comments = []
with open(pages_file, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            disc = data["data"]["repository"]["discussion"]
        except (json.JSONDecodeError, TypeError, KeyError):
            sys.stderr.write(f"resolve_spec_text: could not parse a comment page for discussion #{disc_num}\n")
            sys.exit(1)
        if disc is None:
            sys.stderr.write(f"resolve_spec_text: discussion #{disc_num} not found\n")
            sys.exit(1)
        if body is None:
            body = disc.get("body") or ""
        all_comments.extend(disc.get("comments", {}).get("nodes") or [])

body = body or ""
sys.stdout.write(body)
if not body.endswith("\n"):
    sys.stdout.write("\n")

# Only the first 10 lines are the authoritative freeze pointer — a link
# appearing later in the body is prose referencing something else.
head = "\n".join(body.splitlines()[:10])
m = re.search(r"#discussioncomment-(\d+)", head)
if not m:
    sys.exit(0)

comment_id = int(m.group(1))
for node in all_comments:
    if node.get("databaseId") == comment_id:
        sys.stdout.write("\n<!-- SPEC_TEXT_FROM_COMMENT:%d -->\n" % comment_id)
        sys.stdout.write(node.get("body") or "")
        sys.stdout.write("\n")
        break
else:
    if truncated:
        sys.stderr.write(
            f"resolve_spec_text: comment {comment_id} (linked from discussion #{disc_num}) "
            "was not found in the comments fetched before pagination was stopped early — "
            "see the earlier stderr warning.\n"
        )
'
}

# Allow direct invocation: bash scripts/lib/resolve-spec-text.sh <discussion_number>
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  resolve_spec_text "$@"
fi
