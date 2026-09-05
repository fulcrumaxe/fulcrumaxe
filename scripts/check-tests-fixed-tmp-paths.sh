#!/usr/bin/env bash
# scripts/check-tests-fixed-tmp-paths.sh
#
# Guards against a bash suite under tests/ writing to a FIXED /tmp path —
# a name shared across every concurrent invocation of that suite. D#2254:
# three reviewers hit this in one night on three different suites
# (/tmp/_hook_stderr_2, /tmp/test-cold-start, /tmp/test-cold-start-migrate),
# each correctly diagnosing a race, but the default failure mode is
# misattribution — a reviewer sees red on the PR under review and
# reasonably suspects the PR.
#
# Modelled on scripts/check-no-hardcoded-checkout-paths.sh: a file-based
# allowlist for legitimate literals, and stale/dangling-entry detection so
# the allowlist can't silently rot.
#
# Detection is PER-OCCURRENCE, not per-file or per-token: a /tmp/<literal>
# is flagged only when it appears in WRITE POSITION — immediately after a
# redirect (>, >>, 2>, 2>>), a write-verb command (mkdir, touch, cp, rm,
# cd, tee), or as the right-hand side of a bare shell variable assignment
# (VAR=/tmp/... or VAR="/tmp/...). This is deliberately a textual/syntactic
# classifier, not a semantic one: it does not know that a matched
# occurrence sits inside a quoted JSON test payload versus being live
# shell syntax in this file. That is exactly why so many matches need an
# explicit allowlist entry — a JSON payload like
# '{"command":"mkdir /tmp/scratchpad-xyz"}' textually looks identical to a
# real `mkdir /tmp/scratchpad-xyz` write, and the same literal token
# (e.g. "/tmp/x") is a live defect in one file and load-bearing test data
# in another (D#2254's own pre-Spec measurement). A pure token blocklist
# cannot make this distinction; only per-occurrence classification plus a
# reviewed allowlist can.
#
# One occurrence is NEVER flagged, allowlist or not: an argument to
# `mktemp` itself (its own ...XXXXXX template) — that is correct usage,
# not a defect. D#2254 corrected an early theory that ALL such "-XXXXXX"
# literals were misused; they weren't.
#
# A literal fixed prefix immediately followed by a further '$' expansion
# (e.g. /tmp/foo-$$, /tmp/foo-$RANDOM) is DELIBERATELY still flagged, not
# treated as already-safe: D#2254's own Class A list named several suites
# using exactly this $$-suffixed shape (test_hooks_idempotency.sh,
# test_self_observe_transcript_discovery.sh, test_start_the_day_sync_block.sh)
# as needing the mktemp-dir fix anyway — a shared prefix distinguished only
# by PID is judged not safe enough here, and a lint that silently exempted
# the shape would have missed exactly the suites this Discussion named.
#
# Scope: `git ls-files tests/*.sh` only — non-recursive, so tests/lib/*.sh
# helpers are out of scope (this guard is about suites, not shared libs).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL: not a git repository — this guard requires \`git ls-files\` to determine the tracked file set, and cannot run outside one" >&2
  exit 1
fi

ALLOWLIST="scripts/fixtures/allowed_fixed_tmp_literals.txt"

if [ ! -f "$ALLOWLIST" ]; then
  echo "FAIL: allowlist file $ALLOWLIST not found" >&2
  exit 1
fi

python3 - "$ALLOWLIST" <<'PYEOF'
import re
import subprocess
import sys

allowlist_path = sys.argv[1]

# ---------------------------------------------------------------------------
# Load and validate the allowlist. Format: <path>:<literal>:<reason>
# One entry covers every occurrence of that exact literal in that exact
# file — not one entry per line — since the same Class C literal (e.g.
# '/tmp/scratch.txt') commonly recurs many times as JSON test-payload data.
# ---------------------------------------------------------------------------

_BANNED_REASON_SUBSTRINGS = [
    "not a live hardcode",
    "flag for follow-up",
    "owned by",
    "out of scope",
    "parked",
    "lives there, not here",
]

allow_entries = {}  # (path, literal) -> reason
fail = False

with open(allowlist_path, encoding="utf-8") as f:
    for lineno, raw in enumerate(f, start=1):
        stripped = raw.rstrip("\n").strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            print(
                f"FAIL: malformed allowlist entry (need path:literal:reason) "
                f"at {allowlist_path}:{lineno}: {stripped}",
                file=sys.stderr,
            )
            fail = True
            continue
        path, literal, reason = (p.strip() for p in parts)
        reason_lc = reason.lower()
        for bad in _BANNED_REASON_SUBSTRINGS:
            if bad in reason_lc:
                print(
                    f"FAIL: banned reason in allowlist entry '{path}:{literal}' — {reason}",
                    file=sys.stderr,
                )
                fail = True
                break
        key = (path, literal)
        if key in allow_entries:
            print(
                f"FAIL: duplicate allowlist entry '{path}:{literal}' "
                f"at {allowlist_path}:{lineno}",
                file=sys.stderr,
            )
            fail = True
            continue
        allow_entries[key] = reason

allow_seen = {key: False for key in allow_entries}

# ---------------------------------------------------------------------------
# Enumerate tracked test suites (git ls-files only — untracked scratch
# files and .claude/worktrees/ are structurally excluded). One exact file
# is excluded by construction, never via an allowlist entry, the same way
# check-no-hardcoded-checkout-paths.sh excludes its own hermetic test
# harness: tests/test_check_tests_fixed_tmp_paths.sh's synthetic fixtures
# deliberately write things like "echo hi > /tmp/foo-bar" as fixture
# *content* (a string handed to a throwaway repo, never executed by this
# suite itself) — textually identical to a real write, with no way for a
# textual classifier to tell the difference. Scoped to this one exact
# path, never a prefix or glob.
# ---------------------------------------------------------------------------

out = subprocess.run(
    ["git", "ls-files", "tests/*.sh"], capture_output=True, text=True, check=False
)
_SELF_TEST_EXCLUDE = "tests/test_check_tests_fixed_tmp_paths.sh"
files = sorted(
    l for l in out.stdout.splitlines() if l and l != _SELF_TEST_EXCLUDE
)

TMP_RE = re.compile(r"/tmp/[A-Za-z0-9_.\-]+")

# A redirect operator immediately (optional whitespace) before the match.
REDIRECT_BEFORE_RE = re.compile(r"(?:>{1,2}|2>{1,2})\s*[\"']?$")

# A write-verb word, optionally followed by short flags, immediately
# before the match. Requires at least one space after the verb — unlike
# redirects, "mkdir/tmp/x" is not valid shell.
VERB_BEFORE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:tee|mkdir|touch|cp|rm|cd)\s+(?:-[A-Za-z0-9]+\s+)*[\"']?$"
)

# A bare shell variable assignment immediately before the match:
# VAR=/tmp/... or VAR="/tmp/...
ASSIGN_BEFORE_RE = re.compile(r"(?:^|[\s;])[A-Za-z_][A-Za-z0-9_]*=[\"']?$")

MKTEMP_RE = re.compile(r"\bmktemp\b")

total_matches = 0
unlisted = []

for path in files:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        continue
    for lineno, line in enumerate(lines, start=1):
        for m in TMP_RE.finditer(line):
            literal = m.group(0)
            start, end = m.start(), m.end()
            before = line[:start]

            # mktemp's own template argument is always correct usage.
            # Scoped to the current statement (since the last statement
            # separator) so an unrelated earlier `mktemp` on the same line
            # can't blanket-exempt a later, unrelated /tmp/ literal.
            segment = re.split(r"[;&|]", before)[-1]
            if MKTEMP_RE.search(segment):
                continue

            in_write_position = bool(
                REDIRECT_BEFORE_RE.search(before)
                or VERB_BEFORE_RE.search(before)
                or ASSIGN_BEFORE_RE.search(before)
            )
            if not in_write_position:
                continue

            total_matches += 1
            key = (path, literal)
            if key in allow_entries:
                allow_seen[key] = True
                continue
            unlisted.append((path, lineno, literal))

for path, lineno, literal in unlisted:
    print(
        f"FAIL: {path}:{lineno} writes to fixed path '{literal}' — "
        f"not in {allowlist_path}",
        file=sys.stderr,
    )
    fail = True

# ---------------------------------------------------------------------------
# Dangling (path no longer tracked) and stale (literal no longer matched)
# allowlist entries both hard-fail, naming the key.
# ---------------------------------------------------------------------------

tracked = set(
    l
    for l in subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=False
    ).stdout.splitlines()
    if l
)

for (path, literal), seen in allow_seen.items():
    if path not in tracked:
        print(
            f"FAIL: dangling allowlist entry '{path}:{literal}' — "
            f"{path} is not in git ls-files",
            file=sys.stderr,
        )
        fail = True
        continue
    if not seen:
        print(
            f"FAIL: stale allowlist entry '{path}:{literal}' — no current "
            f"write-position match for this literal in {path}",
            file=sys.stderr,
        )
        fail = True

if fail:
    sys.exit(1)

print(
    f"OK: no unlisted fixed /tmp writes in tests/*.sh "
    f"({total_matches} write-position matches, all allowlisted with reasons)"
)
PYEOF
exit $?
