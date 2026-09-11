#!/usr/bin/env python3
"""CI guard: subshell-mutation findings as a fixed, named quantity (D#2512 fix round).

Background
----------
scripts/lib/subshell-mutation-scan.sh flags `x="$(f)"` / `` x=`f` `` call
sites where f is a shell function that mutates a variable or array without
`local` — the mutation is lost the instant the subshell command substitution
forks and exits. A real-tree run at PR #172's head (417 *.sh files) found 14
call-site findings: 13 genuine and pre-existing, 1 a documented false
positive (see scripts/subshell-mutation-known-findings.txt).

The scanner used to be wired straight into scripts/ci/run-guards.sh's
unconditional auto-discovery, with no ledger entry and no baseline. That
makes the required 'backend (import-smoke)' check permanently red the moment
it merges — the over-blocking failure CLAUDE.md warns guardrails against,
and exactly the shape scripts/ci/ruff-ratchet.py's own docstring warns about
for the same reason. This file is that same ratchet shape, applied here:
a new finding not present in the baseline fails the build, naming the call
site. A baselined finding that still reproduces does not. A baselined
finding that stops reproducing ALSO fails — the ratchet only ever lowers,
never quietly keeps an allowance nobody re-earned (D#2411 item 4, the
lesson PR #164 paid for).

Why the scanner moved to scripts/lib/
--------------------------------------
scripts/ci/run-guards.sh discovers every top-level file in scripts/ci/
(maxdepth 1) and runs each one as its own unconditional guard. `ruff` (the
tool scripts/ci/ruff-ratchet.py wraps) is an installed binary, never a file
in scripts/ci/, so it was never at risk of also being auto-discovered and
run standalone. scripts/lib/subshell-mutation-scan.sh is a file in this
repo, not an installed tool — if it stayed in scripts/ci/ alongside this
ratchet, run-guards.sh would discover and run BOTH: this ratchet (which
would pass, baseline-covered) and the raw scanner (which would still exit 1
unconditionally on its own 14 findings), reproducing exactly the failure
this file exists to fix. Moving the scanner to scripts/lib/ — a plain
directory move, maxdepth-1 discovery does not reach it there — makes this
ratchet the sole CI-facing guard for this check, the same one-guard-per-check
shape every other guard in scripts/ci/ has.

Baseline format
---------------
scripts/subshell-mutation-known-findings.txt, one line per unique
(relative path, mutating function, message) key: `path<TAB>function<TAB>count<TAB>message`.
Keyed by path + function + message rather than by line number, so an
ordinary edit elsewhere in a file does not produce a spurious failure — see
that file's own header. A `count` column exists because the same function
is often captured via $(...) at several call sites in one file with the
same loss shape (e.g. `classify_open_prs`, 5 times in one test file).

Exit codes
----------
Exit 0 = safe: every current finding is covered by the baseline, and every
         baselined finding still reproduces.
Exit 1 = a real finding: a new (or newly-grown) finding not covered by the
         baseline, OR a baselined finding that no longer reproduces
         (over-allowance — the direction that keeps the ratchet honest).
Exit 2 = the guard could not tell, which is a failure and never a pass: the
         baseline is missing, truncated, unreadable, or malformed, or the
         scanner itself could not be invoked or produced something this
         guard could not parse.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER_PATH = REPO_ROOT / "scripts" / "lib" / "subshell-mutation-scan.sh"
BASELINE_PATH = REPO_ROOT / "scripts" / "subshell-mutation-known-findings.txt"

LEDGER_MARKER = "SUBSHELL-MUTATION-RATCHET-V1"

Key = tuple[str, str, str]  # (relative path, mutating function, message)

# Matches scripts/lib/subshell-mutation-scan.sh's one finding-line shape:
#   FAIL: <path>:<line>: `<stmt>` captures $(<call_name> ...) in a subshell,
#   but <call_name> (<def_path>:<def_line>) mutates '<def_var>' —
#   <def_reason> — that mutation is lost when the subshell exits
FAIL_RE = re.compile(
    r"^FAIL: (?P<path>[^:]+):(?P<line>\d+): `(?P<stmt>.*?)` captures "
    r"\$\((?P<call_name>[A-Za-z_][A-Za-z0-9_]*) \.\.\.\) in a subshell, but "
    r"[A-Za-z_][A-Za-z0-9_]* \((?P<def_path>[^:]+):(?P<def_line>\d+)\) "
    r"mutates '(?P<def_var>[A-Za-z_][A-Za-z0-9_]*)' — (?P<def_reason>.+?) — "
    r"that mutation is lost when the subshell exits$"
)

SUMMARY_RE = re.compile(
    r"^subshell-mutation-guard: scanned (?P<files>\d+) \*\.sh file\(s\) ",
    re.MULTILINE,
)


class RatchetError(RuntimeError):
    """The guard could not produce a trustworthy comparison.

    Never means "zero findings" — every caller of load_baseline()/run_scan()
    must treat this as exit 2, not as a clean baseline.
    """


def load_baseline(path: Path = BASELINE_PATH) -> dict[Key, int]:
    """Parse the baseline ledger. Raises RatchetError rather than returning {}.

    A missing, truncated, or unreadable file must never read as "no known
    findings" — that would forgive every regression the ratchet exists to
    catch. See scripts/subshell-mutation-known-findings.txt's own header.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise RatchetError(
            f"{path} could not be read ({exc}). This is a failure, not an "
            f"empty finding list — an unreadable baseline must never be "
            f"treated as 'nothing to compare against'."
        ) from exc

    if LEDGER_MARKER not in text:
        raise RatchetError(
            f"{path} does not carry the {LEDGER_MARKER} marker. The file was "
            f"truncated, emptied, or replaced. An entry-free baseline is only "
            f"meaningful when the marker proves the file is intact."
        )

    counts: dict[Key, int] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = raw_line.split("\t")
        if len(parts) != 4:
            raise RatchetError(
                f"{path}:{lineno}: malformed line — expected 4 tab-separated "
                f"fields (path, function, count, message), got {len(parts)}: "
                f"{raw_line!r}"
            )
        rel_path, call_name, count_s, message = parts
        if not count_s.isdigit():
            raise RatchetError(
                f"{path}:{lineno}: non-numeric count field {count_s!r} in "
                f"line: {raw_line!r}"
            )
        key = (rel_path, call_name, message)
        counts[key] = counts.get(key, 0) + int(count_s)

    return counts


def run_scan() -> list[dict]:
    if not SCANNER_PATH.is_file():
        raise RatchetError(f"scanner not found: {SCANNER_PATH}")
    try:
        proc = subprocess.run(
            ["bash", str(SCANNER_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RatchetError(f"could not run {SCANNER_PATH}: {exc}") from exc
    # The scanner exits 0 clean, 1 with findings — both are real scan
    # results. Anything else (2, a usage/discovery error per its own header)
    # is not a scan result and must not be read as "zero findings".
    if proc.returncode not in (0, 1):
        raise RatchetError(
            f"{SCANNER_PATH} exited {proc.returncode}, which it documents as "
            f"a usage/discovery error, not a scan result:\n{proc.stderr}"
        )
    if not SUMMARY_RE.search(proc.stdout):
        raise RatchetError(
            f"{SCANNER_PATH} produced output with no recognizable summary "
            f"line — this guard cannot tell whether the scan actually ran:\n"
            f"{proc.stdout[-2000:]}"
        )

    findings = []
    for line in proc.stdout.splitlines():
        if not line.startswith("FAIL: "):
            continue
        m = FAIL_RE.match(line)
        if not m:
            raise RatchetError(
                f"{SCANNER_PATH} produced a FAIL line this guard could not "
                f"parse (its output shape may have changed): {line!r}"
            )
        findings.append(m.groupdict())
    return findings


def _key(finding: dict) -> Key:
    message = f"{finding['call_name']} mutates '{finding['def_var']}' — {finding['def_reason']}"
    return finding["path"], finding["call_name"], message


def _write_baseline(counts: Counter) -> None:
    """Regenerate the baseline from the current scan. Never called
    automatically. Does NOT preserve the false-positive comment in the
    existing file — see that file's own header on restoring it by hand."""
    lines = [
        f"# {LEDGER_MARKER}\n",
        "#\n",
        "# Known findings from scripts/lib/subshell-mutation-scan.sh, keyed by\n",
        "# (relative path, mutating function, message) rather than by line\n",
        "# number so an unrelated edit elsewhere in a file does not produce a\n",
        "# spurious failure. `count` covers the same function being captured\n",
        "# via $(...) at several call sites in one file with the same shape.\n",
        "#\n",
        "# Enforced by scripts/ci/subshell-mutation-ratchet.py. A finding here\n",
        "# that stops reproducing must be removed (or its count lowered) in the\n",
        "# same commit as the fix, or the guard fails in the opposite direction\n",
        "# (over-allowance). A finding NOT here fails the build the first time\n",
        "# it's introduced.\n",
        "#\n",
        "# Regenerate with: python3 scripts/ci/subshell-mutation-ratchet.py --write-baseline\n",
        "# then diff it by hand before committing — this writer does not judge\n",
        "# genuine vs. false positive, only count.\n",
        "#\n",
        "# Format: <path>\\t<mutating function>\\t<count>\\t<message>\n",
    ]
    for (rel_path, call_name, message), n in sorted(counts.items()):
        lines.append(f"{rel_path}\t{call_name}\t{n}\t{message}\n")
    BASELINE_PATH.write_text("".join(lines))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # --write-baseline bootstraps or replaces the file wholesale, so it must
    # not first require an existing baseline to read — that would make it
    # impossible to create the baseline in the first place.
    if "--write-baseline" in argv:
        try:
            findings = run_scan()
        except RatchetError as exc:
            print(f"RATCHET ERROR: {exc}", file=sys.stderr)
            return 2
        counts: Counter = Counter(_key(f) for f in findings)
        _write_baseline(counts)
        print(
            f"wrote {BASELINE_PATH} with {sum(counts.values())} finding(s) "
            f"across {len(counts)} key(s)"
        )
        return 0

    try:
        baseline = load_baseline()
    except RatchetError as exc:
        print(f"RATCHET ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        findings = run_scan()
    except RatchetError as exc:
        print(f"RATCHET ERROR: {exc}", file=sys.stderr)
        return 2

    counts: Counter = Counter(_key(f) for f in findings)

    problems: list[str] = []

    for key, n in sorted(counts.items()):
        allowed = baseline.get(key, 0)
        if n > allowed:
            rel_path, call_name, message = key
            why = "NEW" if allowed == 0 else f"count grew {allowed}->{n}"
            problems.append(f"{rel_path}: {call_name}: {message} ({why})")

    for key, allowed in sorted(baseline.items()):
        n = counts.get(key, 0)
        if n < allowed:
            rel_path, call_name, message = key
            problems.append(
                f"{rel_path}: {call_name}: {message!r} — baseline allows "
                f"{allowed}, scan reports {n}. Fixed without lowering "
                f"{BASELINE_PATH.name} (this is a good failure — lower the "
                f"count or remove the line in the same commit as the fix)."
            )

    if problems:
        print(
            f"FAIL: {len(problems)} subshell-mutation-ratchet regression(s) "
            f"against {BASELINE_PATH.name}:",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(
        f"ok: {sum(counts.values())} known subshell-mutation finding(s) "
        f"across {len(counts)} key(s), no regressions against "
        f"{BASELINE_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
