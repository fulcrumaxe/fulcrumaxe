#!/usr/bin/env python3
"""guard-registry-check.py — reconcile scripts/ci/ against the workflow that runs it (D#2339).

Background
----------
Every behavioral guard this team writes lands as one more step in the same
`backend (import-smoke)` job in .github/workflows/ci.yml. Five guard-adding
PRs in roughly one hour, three of them conflicting on those exact lines. The
mechanical conflict is annoying; the dangerous part is the resolution. The
conflict is two steps competing for one insertion point, so taking one side
drops the other — and nothing fails. The job still runs, the PR still merges,
and that guard is simply gone with no signal anywhere.

That is not hypothetical. scripts/ci/clean-install-check.sh has sat in this
directory since D#1617 referenced by nothing at all: a file that prints
PASS/FAIL and that no CI job has ever invoked. It is the purest form of the
defect shape this repo keeps finding — a surface reporting a confident value
it never measured — because it passes by never running.

What this checks
----------------
Every regular file in scripts/ci/ must be either

  1. referenced by a `run:` command in .github/workflows/ci.yml, or
  2. listed in scripts/ci/guard-ledger.json with a non-empty reason
     explaining why it is deliberately not a workflow step.

Anything else fails the build, naming the file. A file that is both wired and
ledgered also fails: the ledger claims the file is exempt from being a step
while it is one, so one of the two is stale. A ledger entry naming a file that
no longer exists fails for the same reason.

Discovery is a plain directory listing, deliberately NOT a mode-bit filter.
Only 3 of the 10 files here carry the executable bit today while all 10 are
invoked as `python3 <path>` or `bash <path>`, so a mode-based subject set
would find 3 guards and silently miss 6 — reintroducing the exact silence
this file exists to remove.

Discovering zero files is a failure, not a pass, for the same reason.

Run from anywhere:

    python3 scripts/ci/guard-registry-check.py          # reconcile, exit 0/1
    python3 scripts/ci/guard-registry-check.py --list   # print the subject set

Exit 0: every file in scripts/ci/ is wired or honestly ledgered.
Exit 1: a file is neither, a ledger entry is stale or reasonless, or the
        directory turned up empty.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CI_DIR = REPO_ROOT / "scripts" / "ci"
# Every workflow, not just ci.yml. This used to be a single hard-coded path,
# which was right while ci.yml was the only workflow — but it makes "wired"
# mean "wired to that one file" rather than "wired to a runner", so a guard
# invoked from any other workflow reads as unreferenced and fails the build
# for being registered in the wrong place. D#2348 PR-h added
# pr-gates.yml for two jobs that need a different trigger set, and hit
# exactly that.
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
LEDGER = CI_DIR / "guard-ledger.json"

# The ledger is the ledger, not a candidate for it.
LEDGER_NAME = LEDGER.name

# Top-level keys guard-ledger.json may carry. Strict on purpose: a typo'd
# structure that exempts nothing must fail loudly rather than quietly
# reconciling an empty exemption set.
LEDGER_KEYS = {"note", "exempt"}


def discover(ci_dir: Path) -> list[str]:
    """Every regular file in ci_dir except the ledger itself, sorted."""
    return sorted(p.name for p in ci_dir.iterdir() if p.is_file() and p.name != LEDGER_NAME)


def command_text(workflow: Path) -> str:
    """The workflow's text with whole-line comments removed.

    Guard paths appear in prose comments as well as in `run:` lines. Counting
    a comment as a reference would let a guard be "wired" by nothing but its
    own explanatory paragraph — which is the failure mode, not the fix.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def is_referenced(name: str, text: str) -> bool:
    """True when scripts/ci/<name> appears as a whole path in text."""
    return re.search(re.escape(f"scripts/ci/{name}") + r"(?![\w.-])", text) is not None


def load_ledger(path: Path) -> tuple[dict[str, str], list[str]]:
    """Return (exempt mapping, hard errors). Errors mean the ledger is unusable."""
    if not path.exists():
        return {}, [f"{path.relative_to(REPO_ROOT)} is missing"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.relative_to(REPO_ROOT)} is not readable JSON: {exc}"]

    rel = path.relative_to(REPO_ROOT)
    if not isinstance(raw, dict):
        return {}, [f"{rel} must be a JSON object, got {type(raw).__name__}"]
    unknown = sorted(set(raw) - LEDGER_KEYS)
    if unknown:
        return {}, [f"{rel} has unknown top-level key(s): {', '.join(unknown)}"]
    if "exempt" not in raw:
        return {}, [f"{rel} is missing its required 'exempt' object"]
    exempt = raw["exempt"]
    if not isinstance(exempt, dict):
        return {}, [f"{rel}: 'exempt' must be an object of filename -> reason"]

    errors = []
    clean = {}
    for name, reason in sorted(exempt.items()):
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{rel}: '{name}' has an empty or non-string reason")
            continue
        clean[name] = reason.strip()
    return clean, errors


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--list"):
        print(f"usage: {Path(sys.argv[0]).name} [--list]", file=sys.stderr)
        return 2

    if not CI_DIR.is_dir():
        print(f"guard-registry-check: FAIL — {CI_DIR} is not a directory", file=sys.stderr)
        return 1

    files = discover(CI_DIR)

    if len(sys.argv) == 2:
        for name in files:
            print(name)
        print(f"count: {len(files)}")
        return 0

    # An empty subject set is the silent-skip this file exists to prevent: a
    # reconciliation that discovers nothing would report every guard fine.
    if not files:
        print(
            "guard-registry-check: FAIL — discovered zero files in scripts/ci/; "
            "a reconciliation with no subjects cannot vouch for anything",
            file=sys.stderr,
        )
        return 1

    workflows = sorted(
        p for p in WORKFLOW_DIR.glob("*.y*ml") if p.is_file()
    ) if WORKFLOW_DIR.is_dir() else []
    if not workflows:
        print(
            f"guard-registry-check: FAIL — no workflow files under {WORKFLOW_DIR}; "
            "with nothing to reconcile against, every guard would read as unwired",
            file=sys.stderr,
        )
        return 1

    exempt, failures = load_ledger(LEDGER)
    texts = {p.name: command_text(p) for p in workflows}

    def wired_in(name: str) -> list[str]:
        """Workflow filenames that reference scripts/ci/<name> in a run: command."""
        return [wf for wf, text in texts.items() if is_referenced(name, text)]

    passes = []
    wired_count = 0
    for name in files:
        hits = wired_in(name)
        ledgered = name in exempt
        if hits and ledgered:
            failures.append(
                f"{name} is run by {', '.join(hits)} AND ledgered as exempt "
                f"from being a step — one of the two is stale"
            )
        elif hits:
            wired_count += 1
            passes.append(f"PASS  {name}  wired: {', '.join(hits)}")
        elif ledgered:
            passes.append(f"PASS  {name}  ledgered — {exempt[name]}")
        else:
            failures.append(
                f"{name} is present in scripts/ci/ but is referenced by none of "
                f"{', '.join(sorted(texts))} and is not listed in "
                f"scripts/ci/guard-ledger.json — it runs nowhere and gates nothing"
            )

    for name in sorted(set(exempt) - set(files)):
        failures.append(
            f"{name} is listed in scripts/ci/guard-ledger.json but no such file "
            f"exists in scripts/ci/ — remove the stale ledger entry"
        )

    if failures:
        for line in failures:
            print(f"guard-registry-check: FAIL — {line}", file=sys.stderr)
        print(
            f"guard-registry-check: {len(failures)} problem(s) across "
            f"{len(files)} file(s) in scripts/ci/",
            file=sys.stderr,
        )
        return 1

    for line in passes:
        print(line)
    print(
        f"guard-registry-check: OK — {len(files)} files "
        f"({wired_count} wired, {len(files) - wired_count} ledgered)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
