#!/usr/bin/env bash
# scripts/check-tests-live-state-paths.sh
#
# Guards against a bash suite under tests/ reading or writing the LIVE,
# checked-out `.autonomous-team/` tree — `agent-feed.jsonl`, `hook-events/`,
# `stats/`, `loop-metrics.jsonl`, `scheduled-jobs/` — instead of a per-run
# fixture. D#2267: three reviewers independently hit this in one night on
# three different suites. The clearest case: `test_sandbox_hook.sh` appended
# every run's block events to the SAME file every running agent's own
# sandbox hook was also appending to
# (`$REPO_ROOT/.autonomous-team/hook-events/blocks-<date>.jsonl`), so its
# pass/fail count depended on whether other agents happened to be active on
# the host — not on the diff under review (D#2267 body, findings 3-4).
#
# Modelled on scripts/check-tests-fixed-tmp-paths.sh (D#2254, PR #2269):
# same file-based allowlist shape (`<path>:<literal>:<reason>`), same
# stale/dangling-entry detection, same banned-reason validation. Read that
# script's header in full before touching this one. Three of its named
# false-positive problems recur here in the same shape; two NEW ones
# surfaced while running this lint against the 9 already-converted
# telemetry-family suites (PR #2304), which the /tmp/ lint's domain never
# had to deal with because it never needed a "this occurrence is fine
# on purpose, in this exact file" case within its OWN enforcement target.
#
# Carried over from the /tmp/ lint, in the same shape:
#
#   1. JSON-payload false positive — a live-path literal embedded in a
#      quoted test fixture (e.g. `PAYLOAD="{\"path\": \"$REPO_ROOT/...\"}"`)
#      reads identically, as plain text, to a live reference. A
#      colon-then-quote before the match satisfies none of the write/assert
#      categories below, so it is never flagged — no per-file exemption
#      needed, same mechanism as the /tmp/ lint's Test 5.
#
#   2. The "mktemp XXXXXX is never a defect" exemption has a structural
#      analogue here, not a regex one: once a suite is converted, its scratch
#      root lives under ITS OWN variable name (`$FIXTURE_ROOT`, `$TMP_REPO`,
#      `$ws`, `$SCRATCH_STATE_DIR`, ...), never `$REPO_ROOT` /
#      `$REAL_REPO_ROOT` / `$MAIN_REPO_ROOT`. Those three names are the
#      entire match surface (ROOT_VARS below) — a suite that materialises
#      its own scratch dir under a differently-named variable is invisible
#      to this lint by construction.
#
#   3. A `$(date +%F)`-suffixed literal (e.g. `.../blocks-$(date +%F).jsonl`)
#      is DELIBERATELY still flagged — the character class stops at `$`, so
#      the captured literal is the fixed prefix up to it
#      (`.../hook-events/blocks-`), and that prefix alone identifies a
#      live-tree write. A dynamic filename suffix doesn't change which
#      directory tree gets written to.
#
# NEW, found while writing this lint (both real, both in already-converted
# D#2267 suites — neither is a hypothetical):
#
#   4. Variable SHADOWING. `hooks/*.py` has no env override for its own
#      telemetry dir (D#2267 finding #6), so the sanctioned fixture pattern
#      (tests/lib/repo-root-fixture.sh) has a converted suite REUSE the same
#      variable name — `MAIN_REPO_ROOT`, `REPO_ROOT` — but reassign its
#      VALUE to a fixture root before using it:
#          MAIN_REPO_ROOT="$FIXTURE_ROOT"
#          BLOCKS_FILE="$MAIN_REPO_ROOT/.autonomous-team/hook-events/..."
#      (test_subagent_stop_dial_audit.sh). Matching on variable NAME alone
#      would flag this even though it is provably not the live root at that
#      point in the script. This lint tracks, per file and top-to-bottom,
#      whether each of the three names currently holds a live value or has
#      been reassigned to a recognizable scratch/fixture one (SHADOW_RHS_RE:
#      `mktemp`, or a name like `$FIXTURE_ROOT` / `$TMP_REPO*` /
#      `$TMPDIR*` / `$SCRATCH_*` / `$WORKSPACE*` / `$ws*`) — only the latter
#      is "shadowed"; everything else, including an indirect
#      still-live derivation (`REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"`,
#      which mentions neither `dirname` nor `BASH_SOURCE` on that line even
#      though `$SCRIPT_DIR` itself was), defaults to "live" and stays in
#      scope. That default is deliberately the safe direction: this lint
#      would rather over-flag an indirect-but-real derivation (forcing an
#      allowlist entry outside the 17, if any) than silently stop watching a
#      variable it merely failed to recognize as still live. Simplification,
#      stated rather than hidden: a reassignment is treated as shadowing the
#      variable for the REST OF THE FILE, even when it was really scoped to
#      one subshell invocation (an env-var prefix on a single command). That
#      is the conservative direction for THIS lint's purpose (it can miss a
#      later real violation after a narrower override than it recognizes;
#      it cannot manufacture a false one), and matches every real case in
#      this repo's corpus at time of writing.
#
#   5. Embedded-script DATA, not code. A suite that tests a block of shell
#      source by holding it in a variable and exec'ing it elsewhere with a
#      redefined environment (test_quality_gate_errors.sh: `QUALITY_GATE_
#      BLOCK='...'`, later run via `REPO_ROOT="$TMPDIR_TEST" bash -c
#      "$QUALITY_GATE_BLOCK"`) contains a live-path-shaped literal
#      (`"${REPO_ROOT}/.autonomous-team/hook-events"`) that is never
#      evaluated against the real REPO_ROOT at all — the same "this looks
#      like code but is actually data" problem as #1, just bash-shaped
#      instead of JSON-shaped, so the existing write/assert classifier
#      alone can't tell them apart (an assignment is an assignment).
#      Handled structurally: this lint tracks open/close regions for (a) a
#      bare `VAR='` ... `'` multi-line quoted block and (b) heredoc bodies
#      (`<<EOF` ... `EOF`), and skips every line strictly INSIDE such a
#      region for both matching and shadow-tracking. A live path on the
#      OPENING line itself (e.g. `cat > "$REPO_ROOT/.autonomous-team/x"
#      <<EOF`) is not exempt — only the body between the markers is.
#
# Deliberate live-state guard, disclosed as ONE allowlist entry, NOT a
# marker (D#2267 Spec item 2, "class (c)"; corrected after code review —
# see below): `test_pre_spawn_check_block_events.sh` AC8 intentionally
# READS the real live feed's line count before running, specifically to
# prove afterward that nothing from THIS suite landed in it — the opposite
# of the defect this lint exists to catch. An earlier version of this
# script exempted it via a comment-marker convention ("a comment containing
# 'live-state guard' suppresses the match"). Code review on PR #2320 caught
# that the marker was checked before, not as part of, the write/assert
# classification: it exempted ANY match near such a comment, including a
# real `>>` append, not just a read — a comment could silence the exact
# defect this lint exists to catch, invisibly, with none of the allowlist's
# stale/dangling-entry detection or banned-reason validation protecting it.
# Removed entirely rather than narrowed, because narrowing a marker down to
# "read-only" still leaves a second, differently-shaped suppression
# mechanism running outside the allowlist's protections, for the sake of
# satisfying the LETTER of "no allowlist entry for any of the 17" rather
# than its intent ("no entries papering over an unconverted suite" — this
# is a converted, correct suite whose one intentional read doesn't fit that
# intent at all). This is now a single, disclosed, ordinary allowlist entry
# for `test_pre_spawn_check_block_events.sh`'s `agent-feed.jsonl` read — the
# one deliberate exception to that Spec item, stated here and in the PR
# body rather than engineered around. `tests/test_check_tests_live_state_
# paths.sh` asserts directly that a live-tree WRITE is never exempted by
# any nearby comment, guarding against this exact defect recurring.
#
# Two literal subpaths are excluded STRUCTURALLY, never via an allowlist
# entry: `.autonomous-team/blackboard/...` and `.autonomous-team/audit.jsonl`.
# Both look in-repo but are symlinks into `~/.autonomous-forever-state/`
# (Team Lead's boundary comment on D#2267, 2026-09-03T10:34:32Z) — they are
# D#2283's exclusive territory, fixed there via `AUTONOMOUS_TEAM_STATE_DIR`
# + `tests/lib/blackboard-fixture.sh`, a completely different mechanism than
# the fixture-root approach this lint checks for. Flagging them here would
# put a permanent, unfixable false positive in front of every suite that
# legitimately references `.autonomous-team/audit.jsonl` by design (e.g.
# `test_scheduler_dispatcher.sh`'s `AUDIT_LOG`, left alone on purpose — see
# that file's own comment). An allowlist entry could not paper over this
# either: banned-reason validation below rejects reasons like "owned by
# D#2283" or "out of scope" by design (same banned list as the /tmp/ lint),
# so the only correct fix is scope, not an allowlist line.
#
# Known textual-classifier gap, stated rather than silently missed: a live
# path passed to `grep`/`diff`/`cmp` as a NON-adjacent argument (i.e. after
# an intervening search pattern, as in `grep -q TOKEN "$PATH"`) is not
# caught directly. Nothing in this repo's corpus does that inline — every
# real occurrence routes the literal through an intermediate variable
# assignment first (`BLOCKS_FILE="$REPO_ROOT/.autonomous-team/..."`, THEN
# `grep -q TOKEN "$BLOCKS_FILE"`), and the assignment line is what this lint
# catches — the later `grep` line has no live-path LITERAL left in it to
# match. A future suite that inlines the literal straight into such a call
# without an intermediate variable would slip past this gap; it is recorded
# here rather than papered over with a false sense of coverage.
#
# Detection is PER-OCCURRENCE, not per-file or per-token, exactly as in the
# /tmp/ lint, and classifies a match as WRITE OR ASSERT POSITION when it is
# immediately preceded by one of:
#   - a redirect operator: >, >>, 2>, 2>>, or < (input redirect)
#   - a write verb: mkdir, touch, cp, rm, cd, tee, mv, ln
#   - a bare shell variable assignment: VAR=... or VAR="...
#   - a test/comparison context: `[[`, `[`, a `-f`/`-e`/`-d`/`-s`/`-r`/`-w`
#     test flag, or `==`/`!=` — covers the direct-comparison assert shape
#     (`[[ "$X" == "$REPO_ROOT/.autonomous-team/..." ]]`) alongside the
#     `test -f "$PATH"` / `[[ -f "$PATH" ]]` existence-check shape.
#
# Scope: `git ls-files tests/*.sh` only — non-recursive, so tests/lib/*.sh
# helpers (including tests/lib/repo-root-fixture.sh and
# tests/lib/blackboard-fixture.sh themselves) are out of scope, matching the
# /tmp/ lint's "this guard is about suites, not shared libs" scope note.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL: not a git repository — this guard requires \`git ls-files\` to determine the tracked file set, and cannot run outside one" >&2
  exit 1
fi

ALLOWLIST="scripts/fixtures/allowed_live_state_literals.txt"

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
# Same shape and same banned-reason list as
# scripts/fixtures/allowed_fixed_tmp_literals.txt — one entry covers every
# occurrence of that exact literal in that exact file.
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
# Enumerate tracked test suites (git ls-files only). One exact file is
# excluded by construction, never via an allowlist entry, the same way the
# /tmp/ lint excludes its own hermetic self-test: this lint's own self-test
# builds synthetic fixture SUITES whose CONTENT deliberately contains
# `$REPO_ROOT/.autonomous-team/...` text as fixture data, never executed —
# textually identical to a real write, with no way for a textual classifier
# to tell the difference.
# ---------------------------------------------------------------------------

out = subprocess.run(
    ["git", "ls-files", "tests/*.sh"], capture_output=True, text=True, check=False
)
_SELF_TEST_EXCLUDE = "tests/test_check_tests_live_state_paths.sh"
files = sorted(
    l for l in out.stdout.splitlines() if l and l != _SELF_TEST_EXCLUDE
)

ROOT_VARS = ("REPO_ROOT", "REAL_REPO_ROOT", "MAIN_REPO_ROOT")
STATE_RE = re.compile(
    r"\$\{?(?:" + "|".join(ROOT_VARS) + r")\}?/\.autonomous-team/[A-Za-z0-9_./\-]+"
)

# Which root variable a match used (for shadow-state lookup).
MATCH_VAR_RE = re.compile(r"\$\{?(" + "|".join(ROOT_VARS) + r")\}?")

# A bare/exported reassignment of one of the three root variables, anywhere
# on the line: (export )?VAR=<rhs to end of line>.
ROOT_ASSIGN_RE = re.compile(
    r"(?:^|[\s;])(?:export\s+)?(" + "|".join(ROOT_VARS) + r")=(.*)$"
)
# An RHS that clearly points at a scratch/fixture root — the only shape
# that shadows a root variable away from "live". This is a whitelist of
# recognizable shadow indicators (mktemp, or a name conventionally used for
# a scratch/fixture root elsewhere in this file's own suite), not a
# blacklist of "self-referential" shapes — the self-referential shape
# varies too much to enumerate safely (`$(cd "$(dirname "${BASH_SOURCE[0]}")/.."
# && pwd)` directly, or indirectly via an already-derived `$SCRIPT_DIR/..`,
# as in test_state_symlinks_in_worktree.sh and
# test_subagent_stop_hook_unknown_role.sh — neither mentions "dirname" on
# the REPO_ROOT= line itself). Defaulting to "live" unless the RHS
# positively looks like a fixture keeps the lint's failure mode in the safe
# direction: an indirect but still-live derivation stays flagged (correct,
# if occasionally in files outside D#2267's 17 that then need an allowlist
# entry), while only a clearly-recognizable fixture reassignment shadows it.
SHADOW_RHS_RE = re.compile(
    r"\bmktemp\b|\$\{?(?:FIXTURE_ROOT|TMP_REPO\w*|TMPDIR\w*|SCRATCH_\w*|WORKSPACE_?\w*|ws\d*)\b",
    re.IGNORECASE,
)

# D#2283's exclusive, symlinked-into-production subpaths — excluded
# structurally, never via allowlist. See header comment above.
def _is_d2283_territory(literal):
    m = re.search(r"\.autonomous-team/(.+)$", literal)
    if not m:
        return False
    leaf = m.group(1)
    return leaf == "blackboard" or leaf.startswith("blackboard/") or leaf.startswith("audit.jsonl")

# A redirect operator, or an input redirect, immediately before the match.
REDIRECT_BEFORE_RE = re.compile(r"(?:>{1,2}|2>{1,2}|<)\s*[\"']?$")

# A write verb, optionally followed by short flags, immediately before.
VERB_BEFORE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:tee|mkdir|touch|cp|rm|cd|mv|ln)\s+(?:-[A-Za-z0-9]+\s+)*[\"']?$"
)

# A bare shell variable assignment immediately before: VAR=/... or VAR="/...
ASSIGN_BEFORE_RE = re.compile(r"(?:^|[\s;])[A-Za-z_][A-Za-z0-9_]*=[\"']?$")

# Test/comparison context immediately before: [[, [, a -f/-e/-d/-s/-r/-w
# flag, or ==/!=.
TEST_BEFORE_RE = re.compile(r"(?:\[\[|\[|-[fedsrw]|==|!=)\s+[\"']?$")

# No comment-marker exemption exists here. An earlier version of this
# script had one ("a comment containing 'live-state guard' suppresses the
# match") and it was removed after code review on PR #2320 showed it
# exempted ANY match near such a comment — including a real write — not
# just a read. See the header comment above. The one legitimate deliberate
# read this lint's own corpus contains
# (test_pre_spawn_check_block_events.sh's AC8) is handled with an ordinary,
# disclosed allowlist entry instead, so it stays inside the allowlist's
# stale/dangling-entry detection and banned-reason validation like every
# other entry.

# Multi-line quoted-block / heredoc body tracking (embedded-script DATA,
# not code — see finding #5 in the header comment).
BLOCK_OPEN_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*='\s*$")
BLOCK_CLOSE_RE = re.compile(r"^\s*'\s*$")
HEREDOC_OPEN_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*$")

total_matches = 0
unlisted = []

for path in files:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        continue

    shadow_state = {v: "live" for v in ROOT_VARS}
    in_block = False
    block_close = None  # None while not in a block; "" sentinel for bare-quote blocks; else heredoc delimiter

    for idx, line in enumerate(lines):
        lineno = idx + 1
        stripped_line = line.rstrip("\n")

        if in_block:
            if block_close == "":
                if BLOCK_CLOSE_RE.match(stripped_line):
                    in_block = False
            else:
                if stripped_line == block_close:
                    in_block = False
            continue

        # A full-line comment (optional leading whitespace, then '#') is
        # skipped entirely — for BOTH shadow-tracking and match-flagging.
        # Needed because a comment can legitimately quote the shape of a
        # real assignment as documentation (e.g.
        # `# The hook computes: STATS_FILE="$REPO_ROOT/.autonomous-team/..."`
        # in test_subagent_stop_hook_unknown_role.sh, explaining what the
        # CODE UNDER TEST does internally, not what this suite itself
        # writes) — textually indistinguishable from a live assignment
        # without this check. Deliberately narrow: only a comment that
        # starts the line is exempt; a trailing `# ...` after real code on
        # the same line is not detected as a comment here and any live-path
        # match earlier on that same line is still classified normally.
        if stripped_line.lstrip().startswith("#"):
            continue

        # Update shadow-tracking state for any root-var reassignment on
        # this (non-exempt) line, before checking matches on the same line.
        for am in ROOT_ASSIGN_RE.finditer(stripped_line):
            var, rhs = am.group(1), am.group(2)
            shadow_state[var] = "shadowed" if SHADOW_RHS_RE.search(rhs) else "live"

        for m in STATE_RE.finditer(line):
            literal = m.group(0)
            if _is_d2283_territory(literal):
                continue

            var_m = MATCH_VAR_RE.match(literal)
            if var_m and shadow_state.get(var_m.group(1)) == "shadowed":
                continue

            start = m.start()
            before = line[:start]

            in_write_or_assert_position = bool(
                REDIRECT_BEFORE_RE.search(before)
                or VERB_BEFORE_RE.search(before)
                or ASSIGN_BEFORE_RE.search(before)
                or TEST_BEFORE_RE.search(before)
            )
            if not in_write_or_assert_position:
                continue

            total_matches += 1
            key = (path, literal)
            if key in allow_entries:
                allow_seen[key] = True
                continue
            unlisted.append((path, lineno, literal))

        # A block/heredoc opening on this line takes effect starting NEXT
        # line — the opening line itself (which may itself contain a live
        # path, e.g. `cat > "$REPO_ROOT/.autonomous-team/x" <<EOF`) is
        # scanned normally above.
        if BLOCK_OPEN_RE.match(stripped_line):
            in_block = True
            block_close = ""
            continue
        here_m = HEREDOC_OPEN_RE.search(stripped_line)
        if here_m:
            in_block = True
            block_close = here_m.group(1)
            continue

for path, lineno, literal in unlisted:
    print(
        f"FAIL: {path}:{lineno} touches live-state path '{literal}' — "
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
            f"write-or-assert-position match for this literal in {path}",
            file=sys.stderr,
        )
        fail = True

if fail:
    sys.exit(1)

print(
    f"OK: no unlisted live-state-path touches in tests/*.sh "
    f"({total_matches} write-or-assert-position matches, all allowlisted with reasons)"
)
PYEOF
exit $?
