#!/usr/bin/env python3
"""CI guard: Python lint as a fixed, named quantity, not an unmonitored class (D#2411).

Background
----------
`Makefile:lint` has run `ruff check backend/ tests/ scripts/ || exit 1` for a
while. It is red — 1,071 findings on ruff 0.15.22 as of this guard landing —
and nothing in CI, preflight, or any hook ever invokes it. A reviewer found a
genuinely dead local (`scripts/audit_repo_plane.py:962`, an `idents` list
built and never read) by running ruff by hand on a prior PR, flagged it as
non-blocking, and it merged anyway: found, reported, and shipped, because
nothing obliged anyone to look again.

Turning `make lint` into a blocking gate outright fails on the 1,071
pre-existing findings — the over-blocking failure CLAUDE.md warns about for
guardrails, and a gate in the way of real work gets disabled. Running it
`--exit-zero` as pure advisory produces a number nobody is obliged to act on.

So: a ratchet, the same shape as scripts/repo-plane-known-defects.txt plus
scripts/ci/repo-plane-cutover-guard.py, which this file mirrors on purpose.
A new finding not present in the baseline fails the build, naming
`file:line:rule`. A baselined finding that still reproduces does not. A
baselined finding that stops reproducing ALSO fails — the ratchet only ever
lowers, never quietly keeps an allowance nobody re-earned.

Scope
-----
Exactly `backend/ tests/ scripts/`, matching the Makefile's `lint` target, so
there is one number rather than two that can drift apart. Python outside that
scope (`hooks/`, `dashboard/`, `testsupport/`, root `conftest.py`,
`loop-bootstrap/`, `ts-backend/`'s Python test generators) is NOT covered by
this ratchet and remains unmonitored — see the `.github/workflows/ci.yml`
comment this guard is referenced from.

The rule set is pinned in `ruff.toml` (today's ruff defaults, made explicit)
rather than left to float on whatever a future ruff version happens to
default to — see that file's comment.

Baseline format
---------------
`scripts/ruff-known-findings.txt`, one line per unique
`(relative path, rule code, message)` key: `path<TAB>rule<TAB>count<TAB>message`.
Keyed by path + rule + message rather than by line number, so an ordinary
edit elsewhere in a file does not produce a spurious failure — see that
file's own header for the full rationale. A `count` column exists because the
same rule+message combination (most often E402, "Module level import not at
top of file") can legitimately repeat several times in one file.

Exit codes
----------
Exit 0 = safe: every current finding is covered by the baseline, and every
         baselined finding still reproduces.
Exit 1 = a real finding: a new (or newly-grown) lint defect not covered by
         the baseline, OR a baselined defect that no longer reproduces
         (over-allowance — the direction that keeps the ratchet honest).
Exit 2 = the guard could not tell, which is a failure and never a pass: the
         baseline is missing, truncated, unreadable, or malformed; its
         recorded ruff version does not match the ruff on PATH; or ruff
         itself could not be invoked or produced something this guard could
         not parse.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUFF_TOML = REPO_ROOT / "ruff.toml"
BASELINE_PATH = REPO_ROOT / "scripts" / "ruff-known-findings.txt"

# Must match Makefile:lint's `ruff check backend/ tests/ scripts/` exactly —
# see the module docstring on why one scope, not two.
SCOPE = ("backend/", "tests/", "scripts/")

LEDGER_MARKER = "RUFF-RATCHET-V1"

Key = tuple[str, str, str]  # (relative path, rule code, message)


class RatchetError(RuntimeError):
    """The guard could not produce a trustworthy comparison.

    Never means "zero findings" — every caller of load_baseline()/run_ruff()
    must treat this as exit 2, not as a clean baseline.
    """


def _ruff_version() -> str:
    try:
        proc = subprocess.run(
            ["ruff", "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RatchetError(f"could not run `ruff --version`: {exc}") from exc
    if proc.returncode != 0:
        raise RatchetError(
            f"`ruff --version` exited {proc.returncode}: {proc.stderr.strip()}"
        )
    parts = proc.stdout.strip().split()
    if len(parts) != 2 or parts[0] != "ruff":
        raise RatchetError(f"unexpected `ruff --version` output: {proc.stdout!r}")
    return parts[1]


def load_baseline(path: Path = BASELINE_PATH) -> tuple[str, dict[Key, int]]:
    """Parse the baseline ledger. Raises RatchetError rather than returning {}.

    A missing, truncated, or unreadable file must never read as "no known
    findings" — that would forgive every regression the ratchet exists to
    catch. See scripts/ruff-known-findings.txt's own header.
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

    version: str | None = None
    counts: dict[Key, int] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("# ruff-version:"):
            version = stripped.split(":", 1)[1].strip()
            continue
        if not stripped or stripped.startswith("#"):
            continue
        parts = raw_line.split("\t")
        if len(parts) != 4:
            raise RatchetError(
                f"{path}:{lineno}: malformed line — expected 4 tab-separated "
                f"fields (path, rule, count, message), got {len(parts)}: "
                f"{raw_line!r}"
            )
        rel_path, rule, count_s, message = parts
        if not count_s.isdigit():
            raise RatchetError(
                f"{path}:{lineno}: non-numeric count field {count_s!r} in "
                f"line: {raw_line!r}"
            )
        key = (rel_path, rule, message)
        counts[key] = counts.get(key, 0) + int(count_s)

    if version is None:
        raise RatchetError(
            f"{path} has no '# ruff-version:' header line — the guard cannot "
            f"tell whether this baseline was generated by the ruff on PATH."
        )

    return version, counts


def run_ruff() -> list[dict]:
    cmd = [
        "ruff", "check",
        "--config", str(RUFF_TOML),
        "--output-format=json",
        *SCOPE,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RatchetError(f"could not run ruff ({' '.join(cmd)}): {exc}") from exc
    # ruff check exits 0 clean, 1 with findings — both are real lint results.
    # Anything else (2, typically) is a ruff usage/config error and must not
    # be read as "zero findings".
    if proc.returncode not in (0, 1):
        raise RatchetError(
            f"`{' '.join(cmd)}` exited {proc.returncode}, which ruff uses for "
            f"a usage/config error, not a lint result:\n{proc.stderr}"
        )
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RatchetError(
            f"ruff produced output this guard could not parse as JSON: {exc}"
        ) from exc


def _key(finding: dict) -> Key:
    rel = Path(finding["filename"]).resolve().relative_to(REPO_ROOT).as_posix()
    return rel, finding["code"], finding["message"]


def _write_baseline(counts: Counter, version: str) -> None:
    """Regenerate the baseline from the current ruff run. Never called
    automatically — see the module docstring's exit-2 section: a version
    mismatch fails loudly rather than triggering this."""
    lines = [
        f"# {LEDGER_MARKER}\n",
        "#\n",
        "# Known Python-lint findings, keyed by (path, rule, message) rather\n",
        "# than by line number so an unrelated edit elsewhere in a file does\n",
        "# not produce a spurious failure. `count` covers the (rare) case of\n",
        "# the same rule+message repeating in one file, most often E402.\n",
        "#\n",
        "# Enforced by scripts/ci/ruff-ratchet.py. A finding here that stops\n",
        "# reproducing must be removed in the same commit as the fix, or the\n",
        "# guard fails in the opposite direction (over-allowance) — see D#2411\n",
        "# item 4. A finding NOT here fails the build the first time it's\n",
        "# introduced.\n",
        "#\n",
        f"# ruff-version: {version}\n",
        f"# scope: {' '.join(SCOPE)} (must match Makefile's `lint` target)\n",
        "# select: pinned in ruff.toml (today's ruff defaults, made explicit)\n",
        "#\n",
        "# Regenerate with: python3 scripts/ci/ruff-ratchet.py --write-baseline\n",
        "# then diff it by hand before committing — this writer does not\n",
        "# judge whether a change is a fix or a regression, only count.\n",
        "#\n",
        "# Format: <path>\\t<rule>\\t<count>\\t<message>\n",
    ]
    for (rel_path, rule, message), n in sorted(counts.items()):
        lines.append(f"{rel_path}\t{rule}\t{n}\t{message}\n")
    BASELINE_PATH.write_text("".join(lines))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        current_version = _ruff_version()
    except RatchetError as exc:
        print(f"RATCHET ERROR: {exc}", file=sys.stderr)
        return 2

    # --write-baseline bootstraps or replaces the file wholesale, so it must
    # not first require an existing, version-matched baseline to read — that
    # would make it impossible to create the baseline in the first place.
    if "--write-baseline" in argv:
        try:
            findings = run_ruff()
        except RatchetError as exc:
            print(f"RATCHET ERROR: {exc}", file=sys.stderr)
            return 2
        counts: Counter = Counter(_key(f) for f in findings)
        _write_baseline(counts, current_version)
        print(
            f"wrote {BASELINE_PATH} with {sum(counts.values())} finding(s) "
            f"across {len(counts)} key(s), ruff {current_version}"
        )
        return 0

    try:
        baseline_version, baseline = load_baseline()
    except RatchetError as exc:
        print(f"RATCHET ERROR: {exc}", file=sys.stderr)
        return 2

    if baseline_version != current_version:
        print(
            f"RATCHET ERROR: {BASELINE_PATH} was generated by ruff "
            f"{baseline_version}, but this is ruff {current_version}. A "
            f"version bump can silently change what ruff reports for the "
            f"same tree, so this guard refuses to compare across versions "
            f"rather than regenerate itself. Review the diff by hand on "
            f"ruff {current_version}, then re-run with --write-baseline.",
            file=sys.stderr,
        )
        return 2

    try:
        findings = run_ruff()
    except RatchetError as exc:
        print(f"RATCHET ERROR: {exc}", file=sys.stderr)
        return 2

    counts: Counter = Counter(_key(f) for f in findings)

    lines_by_key: dict[Key, dict] = {}
    for f in findings:
        lines_by_key.setdefault(_key(f), f)

    problems: list[str] = []

    for key, n in sorted(counts.items()):
        allowed = baseline.get(key, 0)
        if n > allowed:
            rel_path, rule, message = key
            row = lines_by_key[key]["location"]["row"]
            why = "NEW" if allowed == 0 else f"count grew {allowed}->{n}"
            problems.append(f"{rel_path}:{row}: {rule} {message} ({why})")

    for key, allowed in sorted(baseline.items()):
        n = counts.get(key, 0)
        if n < allowed:
            rel_path, rule, message = key
            problems.append(
                f"{rel_path}: {rule} {message!r} — baseline allows {allowed}, "
                f"ruff reports {n}. Fixed without lowering "
                f"{BASELINE_PATH.name} (this is a good failure — lower the "
                f"count or remove the line in the same commit as the fix)."
            )

    if problems:
        print(
            f"FAIL: {len(problems)} ruff-ratchet regression(s) against "
            f"{BASELINE_PATH.name}:",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(
        f"ok: {sum(counts.values())} known ruff finding(s) across "
        f"{len(counts)} key(s) in {' '.join(SCOPE)}, ruff {current_version}, "
        f"no regressions against {BASELINE_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
