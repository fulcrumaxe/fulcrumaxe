#!/usr/bin/env python3
"""coldstart-state-dir-guard.py — a test may not coldstart into the operator's
home directory (D#2317 PR-c).

Background
----------
`scripts/coldstart-project.sh` and `scripts/coldstart.sh` create a state dir
per project. Both used to hardcode `$HOME/.<name>-state` with no override, so
a test that coldstarted a throwaway project wrote a permanent directory under
the operator's `$HOME` and had no supported way to redirect it.

`tests/test_loop_metrics_path.sh` did exactly that once per run, naming the
project `test-proj-$$`, and then "cleaned up" `/tmp/${PROJECT_NAME}-state` —
the wrong root, and missing the leading dot — so it never once removed what it
created. Over about eight weeks that produced 44 of the 75 dead
`~/.test-proj-*-state` fixtures the Fleet page was listing as live projects
(D#2317). A manual quarantine sweep was needed to undo it.

The fix is `COLDSTART_STATE_ROOT` (see `scripts/lib/coldstart-state-root.sh`):
an absolute path that both entry points resolve their state dir under,
defaulting to `$HOME` so operator behaviour is unchanged. This guard is what
stops the class coming back — it fails when a file under `tests/` invokes a
coldstart script without setting that override on that invocation.

What counts as an invocation
----------------------------
A line that references a coldstart script — either by path literal
(`.../coldstart-project.sh`) or through a variable previously assigned exactly
such a path (`COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"`, then
`bash "$COLDSTART_SH" ...`) — *and* carries a `bash` / `sh` / `env` execution
token. A bare reference with no execution token (`grep -n foo
"$REPO_ROOT/scripts/coldstart-project.sh"`, `assert_contains "$COLDSTART_SH"`,
`open('.../coldstart-project.sh')`) is a read, not a run, and is not flagged.

Invocations that provably cannot reach the state-dir creation are not flagged:
a line carrying one of the non-mutating flags below (`--help`, `--dry-run`,
`--self-test`, `-n`, `--version`), and any `coldstart.sh` invocation that
passes no `--name` (it exits on the missing-argument path long before
`STATE_DIR` is computed).

What counts as containment
--------------------------
An assignment or export of `COLDSTART_STATE_ROOT` within a small window around
the invocation: the 20 lines before it through the 8 lines after. The window
exists because the override is not always writable as a same-line env prefix —
a shell suite exports it once at the top for the whole file, and a Python test
puts it in the `env=` dict a few lines below the `subprocess.run` argument
list. Only a line that *assigns* the name (`COLDSTART_STATE_ROOT=` or
`"COLDSTART_STATE_ROOT":`) counts; a bare mention in a comment does not.

A `HOME=` env prefix on the invocation line itself also counts. The state root
resolves as `${COLDSTART_STATE_ROOT:-$HOME}`, so pointing HOME at a fixture
redirects it just as effectively — and one test has to invoke the script with
the variable genuinely unset, to prove the default is unchanged. That shape is
accepted same-line only, where the redirect provably covers the invocation.

Stated gap: this is a proximity rule, not dataflow analysis. An assignment
inside a branch that does not actually cover the invocation would satisfy it.
That direction was chosen deliberately — the alternative (same-line only)
cannot express the two containment shapes above and would force allowlist
entries for correct code, and an allowlist entry is a worse outcome than a
coarse-but-honest window.

Allowlist
---------
`scripts/fixtures/allowed_coldstart_unscoped_invocations.txt`, same shape as
`scripts/check-tests-live-state-paths.sh` (PR #2320): `<path>:<script>:<reason>`,
with the same banned-reason substrings and the same stale/dangling-entry
detection. It ships empty on purpose — every real invocation in the tree is
contained rather than excused.

Self-test
---------
Runs first, on every invocation, against synthetic in-memory fixtures: a
positive case (an offending line is flagged), a negative case (a contained line
is not), and an allowlisted case (the same offending line is suppressed by an
entry). A guard with no failing case is not a guard.

Run from the repo root:

    python3 scripts/ci/coldstart-state-dir-guard.py

Exit 0: self-test behaved and no unlisted uncontained invocation exists.
Exit 1: something failed — one `FAIL ...` line per failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "fixtures" / "allowed_coldstart_unscoped_invocations.txt"

# Reasons that describe a deferral rather than a justification. Same list as
# scripts/check-tests-live-state-paths.sh — an allowlist whose entries may say
# "out of scope" is a backlog, not an allowlist.
BANNED_REASON_SUBSTRINGS = [
    "not a live hardcode",
    "flag for follow-up",
    "owned by",
    "out of scope",
    "parked",
    "lives there, not here",
]

# The coldstart ENTRY POINTS — the scripts that create a state dir, or
# delegate to one that does. Deliberately an explicit list rather than a
# `coldstart*.sh` wildcard: scripts/lib/coldstart-preflight.sh and
# scripts/lib/coldstart-halt-flow.sh match that shape, create no state dir,
# and would be permanent false positives.
ENTRY_POINTS = ("coldstart-project.sh", "coldstart-unified.sh", "coldstart.sh")
_ENTRY_ALT = "|".join(re.escape(e) for e in ENTRY_POINTS)

SCRIPT_LITERAL_RE = re.compile(_ENTRY_ALT)

# VAR="....coldstart-project.sh"  /  VAR = REPO_ROOT / "scripts" / "coldstart.sh"
# Only an assignment whose right-hand side ENDS at an entry point counts, so a
# variable holding a list of many script names is not mistaken for one.
SCRIPT_VAR_ASSIGN_RE = re.compile(
    r"""(?:^|[\s;])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^;]*(?:""" + _ENTRY_ALT + r""")["']?\s*$"""
)

# A bash/sh/env execution token, as a whole word.
EXEC_TOKEN_RE = re.compile(r"""(?:^|[\s;&|(\["',])(?:bash|sh|env)(?:["'\s,)\]]|$)""")

# Flags that provably exit before any state dir is created.
NON_MUTATING_FLAGS = ("--help", "-h", "--dry-run", "--self-test", "--version")
SYNTAX_ONLY_RE = re.compile(r"\bbash\s+-n\b")

# An assignment (not a bare mention) of the override.
CONTAINMENT_RE = re.compile(r"""(?:COLDSTART_STATE_ROOT\s*=|["']COLDSTART_STATE_ROOT["']\s*:)""")

# The state root is `${COLDSTART_STATE_ROOT:-$HOME}`, so an explicit HOME
# override on the invocation itself redirects it just as effectively. Accepted
# as containment, but SAME LINE ONLY — an env prefix is the only shape where
# the redirect provably covers this invocation, and one test deliberately
# exercises the unset-variable default path with exactly that shape.
HOME_SAME_LINE_RE = re.compile(r"""(?:^|[\s;&|(])HOME\s*=""")

WINDOW_BEFORE = 20
WINDOW_AFTER = 8


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued physical lines into one logical line.

    A shell env prefix is routinely written on the line above the command it
    applies to (`env -u COLDSTART_STATE_ROOT HOME="$H2" \\` / newline /
    `bash .../coldstart-project.sh ...`). Those are one command, so they have
    to be one line here — otherwise the same-line containment rule below reads
    a correctly-scoped invocation as unscoped.
    """
    out: list[tuple[int, str]] = []
    buf = ""
    start = 1
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = lineno
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((start, buf + raw))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def scan(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Return (path, lineno, script, line) for each uncontained invocation."""
    joined = _logical_lines(text)
    linenos = [n for n, _ in joined]
    lines = [t for _, t in joined]
    script_vars: set[str] = set()
    findings: list[tuple[str, int, str, str]] = []

    for idx, line in enumerate(lines):
        stripped = line.strip()

        m = SCRIPT_VAR_ASSIGN_RE.search(line)
        if m:
            script_vars.add(m.group(1))

        if stripped.startswith("#"):
            continue

        literal = SCRIPT_LITERAL_RE.search(line)
        var_hit = None
        if not literal and script_vars:
            for var in script_vars:
                if re.search(r"[$\s(\[\"']" + re.escape(var) + r"\b", line):
                    var_hit = var
                    break
        if not literal and not var_hit:
            continue

        if not EXEC_TOKEN_RE.search(line):
            continue  # a read of the script, not a run of it

        if SYNTAX_ONLY_RE.search(line):
            continue
        if any(f in line for f in NON_MUTATING_FLAGS):
            continue

        script = literal.group(0) if literal else var_hit
        # coldstart.sh derives its project name from --name; without one it
        # exits on the missing-argument path before computing STATE_DIR.
        if script.endswith("coldstart.sh") and "--name" not in line:
            continue

        if HOME_SAME_LINE_RE.search(line):
            continue
        lo = max(0, idx - WINDOW_BEFORE)
        hi = min(len(lines), idx + WINDOW_AFTER + 1)
        if any(CONTAINMENT_RE.search(lines[j]) for j in range(lo, hi)):
            continue

        findings.append((path, linenos[idx], script, stripped))

    return findings


# ---------------------------------------------------------------------------
# Self-test — hermetic, no filesystem, no git.
# ---------------------------------------------------------------------------

_OFFENDING = """#!/usr/bin/env bash
TMP_REPO=$(mktemp -d)
bash "$REPO_ROOT/scripts/coldstart-project.sh" "$TMP_REPO" "test-proj-$$"
"""

_CONTAINED = """#!/usr/bin/env bash
COLDSTART_STATE_ROOT="$(mktemp -d)"
export COLDSTART_STATE_ROOT
TMP_REPO=$(mktemp -d)
bash "$REPO_ROOT/scripts/coldstart-project.sh" "$TMP_REPO" "test-proj-$$"
"""

_READ_ONLY = """#!/usr/bin/env bash
COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"
grep -n 'loop-metrics.jsonl' "$COLDSTART_SH"
assert_contains "$COLDSTART_SH" "import duckdb" "duckdb python init"
"""

_VAR_INVOCATION = """#!/usr/bin/env bash
COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"
bash "$COLDSTART_SH" "$TMP_REPO" leaky
"""

_HOME_PREFIXED = """#!/usr/bin/env bash
HOME="$FIXTURE_HOME" bash "$REPO_ROOT/scripts/coldstart-project.sh" "$TMP_REPO" defaulted
"""

# The env prefix on the line above the command it applies to — one logical
# line, and it must be read as one.
_CONTINUED_PREFIX = """#!/usr/bin/env bash
env -u COLDSTART_STATE_ROOT HOME="$FIXTURE_HOME" \\
  bash "$REPO_ROOT/scripts/coldstart-project.sh" "$TMP_REPO" defaulted \\
  > "$WORK/cs.log" 2>&1
"""


def run_self_test(fail) -> None:
    cases = [
        ("positive/offending", _OFFENDING, True),
        ("negative/contained", _CONTAINED, False),
        ("negative/read-only-reference", _READ_ONLY, False),
        ("positive/via-variable", _VAR_INVOCATION, True),
        ("negative/home-prefixed", _HOME_PREFIXED, False),
        ("negative/continued-env-prefix", _CONTINUED_PREFIX, False),
    ]
    for name, text, want_findings in cases:
        got = scan(f"tests/self-test-{name}.sh", text)
        if want_findings and not got:
            fail(f"self-test: {name} — expected a finding, got none")
        if not want_findings and got:
            fail(f"self-test: {name} — expected no finding, got {got}")

    # An allowlist entry suppresses an otherwise-real finding.
    findings = scan("tests/self-test-allowlisted.sh", _OFFENDING)
    if not findings:
        fail("self-test: allowlist case produced no finding to suppress")
        return
    allow = {("tests/self-test-allowlisted.sh", findings[0][2]): "self-test fixture"}
    remaining = [f for f in findings if (f[0], f[2]) not in allow]
    if remaining:
        fail(f"self-test: allowlist entry did not suppress {remaining}")


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def load_allowlist(fail) -> dict[tuple[str, str], str]:
    entries: dict[tuple[str, str], str] = {}
    if not ALLOWLIST_PATH.exists():
        fail(f"allowlist file {ALLOWLIST_PATH} not found")
        return entries

    for lineno, raw in enumerate(ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            fail(f"malformed allowlist entry (need path:script:reason) at line {lineno}: {stripped}")
            continue
        path, script, reason = (p.strip() for p in parts)
        reason_lc = reason.lower()
        banned = next((b for b in BANNED_REASON_SUBSTRINGS if b in reason_lc), None)
        if banned:
            fail(f"banned reason in allowlist entry '{path}:{script}' — {reason}")
            continue
        if (path, script) in entries:
            fail(f"duplicate allowlist entry '{path}:{script}' at line {lineno}")
            continue
        entries[(path, script)] = reason
    return entries


def main() -> int:
    failures: list[str] = []

    def fail(detail: str) -> None:
        failures.append(detail)
        print(f"FAIL {detail}")

    run_self_test(fail)
    allow = load_allowlist(fail)

    listed = subprocess.run(
        ["git", "ls-files", "tests/"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if listed.returncode != 0:
        fail("git ls-files tests/ failed — this guard needs a git checkout to determine its file set")
        return 1

    files = [f for f in listed.stdout.splitlines() if f]
    if not files:
        fail("git ls-files tests/ returned nothing — refusing to report a clean scan of an empty set")
        return 1

    seen: set[tuple[str, str]] = set()
    scanned = 0
    for rel in files:
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for path, lineno, script, line in scan(rel, text):
            key = (path, script)
            if key in allow:
                seen.add(key)
                continue
            fail(
                f"{path}:{lineno} runs {script} without setting COLDSTART_STATE_ROOT — "
                f"this writes a permanent state dir under the operator's $HOME. Set "
                f"COLDSTART_STATE_ROOT to a scratch dir on that invocation.\n      {line}"
            )

    tracked = set(
        f
        for f in subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=False, cwd=REPO_ROOT
        ).stdout.splitlines()
        if f
    )
    for (path, script), _reason in allow.items():
        if path not in tracked:
            fail(f"dangling allowlist entry '{path}:{script}' — {path} is not in git ls-files")
        elif (path, script) not in seen:
            fail(f"stale allowlist entry '{path}:{script}' — no uncontained invocation matches it")

    if failures:
        print(f"coldstart-state-dir-guard: {len(failures)} check(s) failed")
        return 1

    print(f"coldstart-state-dir-guard: all clear ({scanned} tracked files under tests/ scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
