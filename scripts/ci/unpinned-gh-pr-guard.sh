#!/usr/bin/env bash
# scripts/ci/unpinned-gh-pr-guard.sh — fail the build on an unpinned `gh pr` or
# `gh api repos/` statement in a role card (D#2523).
#
# WHY THIS EXISTS
# ----------------
# Six role cards under .claude/agents/ made `gh pr` calls with zero reference
# to the code plane. A browser-tester spawned to verify PR #134 ran
# `gh pr view 134` under the card's old blanket rule ("all gh CLI calls use
# --repo autonomous-agent-7/fulcrumaxe"), which resolved against the
# Discussion plane and returned a completely different PR — both planes
# number PRs independently, so every low number exists on both. The agent
# caught the mismatch and refused; the next one might not.
#
# `tests/test_card_code_plane_pins.py` already asserts this property for
# `.claude/agents/*.md` and `CLAUDE.md` as a pytest suite. This file is a
# second, independent check that matters for D#2523's acceptance criteria for
# a different reason than duplication: it is discovered by
# scripts/ci/run-guards.sh, so it runs even on a PR route that skips pytest
# entirely (see scripts/ci/repo-plane-cutover-guard.py's docstring for a case
# where exactly that happened), and it additionally covers `gh api repos/`
# alongside `gh pr`.
#
# WHAT THIS CHECKS
# -----------------
# Every real `gh pr <verb>` and `gh api repos/...` statement in
# `.claude/agents/*.md` — anchored to a statement-start position (line start,
# after `; `, right inside `(` / `$(`, or after `&&`/`||`) so an inline aside
# like "Do NOT use `gh pr review`" or a markdown bullet ("- gh pr list ...
# (via release_manager)") is never counted — must be guarded with
# `${CODE_REPO:?...}` (the guarded form; `$CODE_REPO` or `${CODE_REPO}` alone
# is NOT accepted — `gh --repo ""` exits 0 and silently resolves from the
# checkout's git remote, so an unguarded pin is the bare call it replaced and
# still greps as pinned), with the resolve assignment in the SAME statement.
# "Same statement" spans a trailing-backslash line continuation in EITHER
# direction: the resolve assignment may sit on the line before the `gh`
# invocation (`CODE_REPO=...; gh pr view ...`, the common case), or the `gh`
# invocation's own line may end in `\` with `--repo "${CODE_REPO:?...}"` on
# the line it continues onto (`docs-writer.md`'s and `release-manager.md`'s
# `gh pr comment ... \` / `  --repo "${CODE_REPO:?...}"` shape) — two lines
# would invite two tool calls, and an agent's shell state does not survive
# between them, but a backslash-continued pair is one statement either way.
#
# A statement carrying the Discussion-plane literal
# (`autonomous-agent-7/fulcrumaxe`) is accepted for `gh api repos/` (never for
# `gh pr` — PRs do not exist on the Discussion plane) since that plane is
# permanently private and intentionally never resolved.
#
# A human-typed placeholder standing in for a real value (`<the value ...>`)
# is exempt — that line is copied and edited by hand before it is ever run,
# so it has no `--repo` to be wrong about yet.
#
# WHAT THIS DOES NOT CHECK
# -------------------------
# Nothing about `gh issue`, `gh api graphql`, or any other `gh` subcommand —
# those legitimately target the Discussion plane in several of these cards,
# and a blanket rewrite pinning every `gh` call to the code plane would be a
# regression (D#2523 Spec item 3). Not `CLAUDE.md` — already covered by
# `tests/test_card_code_plane_pins.py`; duplicating it here would mean two
# checkers to keep in sync for the one file that already has one.
#
# Not `backend/spawn_templates/*.tmpl`, despite those files also emitting
# `gh pr` statements: they resolve `{{CODE_REPO}}` / `{{REPO}}` by build-time
# template substitution (backend/spawn_templates.py), a different mechanism
# from the runtime shell resolution this guard checks for — and running this
# check against them today finds real, pre-existing `{{REPO}}` (the stale
# placeholder — see PR #132 / D#2521, which fixed six other occurrences of
# the same thing) in accessibility-reviewer.tmpl, docs-writer.tmpl, and
# runbook-writer.tmpl. Folding that in here would fail this guard on files
# D#2523 never touched. Reported to the Team Lead in the D#2523 PR body as a
# separate, pre-existing finding rather than silently absorbed or silently
# dropped.
#
# Usage:
#   bash scripts/ci/unpinned-gh-pr-guard.sh              # scan the real repo
#   bash scripts/ci/unpinned-gh-pr-guard.sh --root DIR    # scan DIR instead —
#     changes exactly one thing, which directory is treated as repo root
#     (matches run-guards.sh's own --dir contract), so a --root run is
#     evidence about the real check (D#2149). Used by the mutation test.
#
# Exit 0: every real gh pr / gh api repos/ statement found in
#         .claude/agents/*.md is guarded (or exempt for one of the reasons
#         above).
# Exit 1: at least one is not — printed as FILE:LINE: TEXT.
# Exit 2: usage error.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="$(basename "${BASH_SOURCE[0]}")"

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      if [ $# -lt 2 ]; then
        echo "$NAME: --root needs a directory" >&2
        exit 2
      fi
      ROOT="$2"; shift 2 ;;
    *)
      echo "usage: $NAME [--root DIR]" >&2
      exit 2 ;;
  esac
done

if [ ! -d "$ROOT" ]; then
  echo "$NAME: FAIL — $ROOT is not a directory" >&2
  exit 1
fi
ROOT="$(cd "$ROOT" && pwd)"

python3 - "$ROOT" "$NAME" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
name = sys.argv[2]

agents_dir = root / ".claude" / "agents"
targets = sorted(agents_dir.glob("*.md"))

DISCUSSION_PLANE_LITERAL = "autonomous-agent-7/fulcrumaxe"
RESOLVE = 'CODE_REPO="$(source scripts/lib/repo-resolve.sh && _resolve_code_repo)"'
GUARDED = re.compile(r"\$\{CODE_REPO:\?[^}]*\}")

# Anchored to a statement-start position: line start, after "; ", right
# inside "(" / "$(", or after "&&"/"||". A markdown bullet ("- gh pr ...") or
# a mid-sentence mention ("Do NOT use `gh pr review`") never matches this —
# neither is ever executed verbatim.
_ANCHOR = r"(?:^\s*|; |\(|&&\s|\|\|\s)"
GH_PR_VERB = re.compile(_ANCHOR + r"gh pr (view|edit|create|comment|diff|list|merge|review) ")
GH_API_REPOS = re.compile(_ANCHOR + r"gh api [\"']?repos/")

# A human-typed placeholder in place of a real argument, e.g. CLAUDE.md's
# documented fallback "gh pr list --repo <the value the command above
# printed> ...". Never run verbatim — copied and edited by hand first.
PLACEHOLDER_VALUE = re.compile(r"<[^<>]+>")


def _continues_from(prev: str) -> bool:
    return prev.rstrip().endswith("\\")


offenders = []

for path in targets:
    if not path.is_file():
        continue
    lines = path.read_text(encoding="utf-8").split("\n")
    for n, line in enumerate(lines, 1):
        is_pr = GH_PR_VERB.search(line)
        is_api = GH_API_REPOS.search(line)
        if not (is_pr or is_api):
            continue
        if PLACEHOLDER_VALUE.search(line):
            continue
        # The Discussion-plane literal is only ever a legitimate target for
        # `gh api repos/` (e.g. a Discussion-plane label/collaborator read) —
        # never for `gh pr`, since PRs do not exist on that plane.
        if is_api and DISCUSSION_PLANE_LITERAL in line:
            continue

        prev_line = lines[n - 2] if n >= 2 else ""
        next_line = lines[n] if n < len(lines) else ""

        # The logical statement this "gh" line belongs to: itself, plus the
        # previous line if IT continues into this one, plus the next line if
        # THIS one continues into it. Backslash continuation joins exactly
        # adjacent lines, so one line of lookaround each way is sufficient —
        # no card in this repo chains three.
        statement = line
        if _continues_from(prev_line):
            statement = prev_line + "\n" + statement
        if _continues_from(line):
            statement = statement + "\n" + next_line

        if GUARDED.search(statement) and RESOLVE in statement:
            continue
        if is_api and DISCUSSION_PLANE_LITERAL in statement:
            continue

        offenders.append((path, n, line.strip()))

if offenders:
    for path, n, line in offenders:
        rel = path.relative_to(root)
        print(f"{name}: FAIL — {rel}:{n}: {line}", file=sys.stderr)
    print(
        f"{name}: FAIL — {len(offenders)} unpinned gh pr / gh api repos/ "
        f"statement(s) across {len(targets)} file(s) scanned",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    f"{name}: OK — {len(targets)} file(s) scanned under .claude/agents/*.md, "
    "no unpinned gh pr / gh api repos/ statement found"
)
PY
