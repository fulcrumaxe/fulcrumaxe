#!/usr/bin/env python3
"""guard-registry-check.py — reconcile scripts/ci/ against what actually runs it (D#2339).

Background
----------
Every behavioral guard this team writes used to land as one more step in the
same `backend (import-smoke)` job in .github/workflows/ci.yml. Five guard-adding
PRs in roughly one hour, three of them conflicting on those exact lines. The
mechanical conflict is annoying; the dangerous part is the resolution. The
conflict is two steps competing for one insertion point, so taking one side
drops the other — and nothing fails. The job still runs, the PR still merges,
and that guard is simply gone with no signal anywhere.

That is not hypothetical. scripts/ci/clean-install-check.sh sat in this
directory since D#1617 referenced by nothing at all: a file that prints
PASS/FAIL and that no CI job has ever invoked. It is the purest form of the
defect shape this repo keeps finding — a surface reporting a confident value
it never measured — because it passes by never running.

D#2339 PR-b removed the shared insertion point: scripts/ci/run-guards.sh now
runs the guards, so adding one is adding a file and touching no YAML. That
moves the failure mode rather than removing it. There is no per-guard step
left to drop, but the runner itself is now a single step whose loss would
take every guard with it, and a file the runner does not discover is as unrun
as an unwired step ever was. This file is the signal for both.

What this checks
----------------
Every regular file in scripts/ci/ must be exactly one of

  1. discovered by `bash scripts/ci/run-guards.sh --list` (the normal case —
     this is what "adding a guard is a one-file change" means), or
  2. listed under "own_step" in scripts/ci/guard-ledger.json with a non-empty
     reason AND actually referenced by a `run:` command in some workflow, or
  3. listed under "exempt" in scripts/ci/guard-ledger.json with a non-empty
     reason AND referenced by no workflow at all.

Anything else fails the build, naming the file. The two ledger sections are
checked in opposite directions on purpose: "own_step" claims a workflow runs
the file, so an entry no workflow references is a lie the build catches;
"exempt" claims nothing runs it, so an entry a workflow does reference is a
stale claim the build also catches. A ledger entry naming a file that no
longer exists fails for the same reason.

run-guards.sh itself is not a guard and cannot run itself. It gets the one
rule that matters for it: some workflow must reference it. A runner that runs
nowhere gates nothing, and it would take every guard it discovers down with it
silently — which is the D#2339 failure mode with a larger blast radius than
the one it replaced.

The subject set is a plain directory listing, deliberately NOT a mode-bit
filter. Only some files here carry the executable bit while all of them are
invoked as `python3 <path>` or `bash <path>`, so a mode-based subject set
would find a handful of guards and silently miss the rest — reintroducing the
exact silence this file exists to remove.

Discovering zero files is a failure, not a pass, for the same reason.

Run from anywhere:

    python3 scripts/ci/guard-registry-check.py          # reconcile, exit 0/1
    python3 scripts/ci/guard-registry-check.py --list   # print the subject set

Exit 0: every file in scripts/ci/ is run by the runner or honestly ledgered.
Exit 1: a file is none of the three, a ledger entry is stale or reasonless,
        the runner is unwired or unusable, or the directory turned up empty.
"""

from __future__ import annotations

import json
import re
import subprocess
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
RUNNER = CI_DIR / "run-guards.sh"

# The ledger is the ledger, not a candidate for it.
LEDGER_NAME = LEDGER.name
RUNNER_NAME = RUNNER.name

# Top-level keys guard-ledger.json may carry. Strict on purpose: a typo'd
# structure that exempts nothing must fail loudly rather than quietly
# reconciling an empty exemption set.
LEDGER_KEYS = {"note", "exempt", "own_step"}
LEDGER_SECTIONS = ("exempt", "own_step")


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


def load_ledger(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Return ({section: {name: reason}}, hard errors). Errors mean it is unusable."""
    empty = {section: {} for section in LEDGER_SECTIONS}
    if not path.exists():
        return empty, [f"{path.relative_to(REPO_ROOT)} is missing"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return empty, [f"{path.relative_to(REPO_ROOT)} is not readable JSON: {exc}"]

    rel = path.relative_to(REPO_ROOT)
    if not isinstance(raw, dict):
        return empty, [f"{rel} must be a JSON object, got {type(raw).__name__}"]
    unknown = sorted(set(raw) - LEDGER_KEYS)
    if unknown:
        return empty, [f"{rel} has unknown top-level key(s): {', '.join(unknown)}"]

    errors: list[str] = []
    clean: dict[str, dict[str, str]] = {section: {} for section in LEDGER_SECTIONS}
    for section in LEDGER_SECTIONS:
        if section not in raw:
            errors.append(f"{rel} is missing its required '{section}' object")
            continue
        body = raw[section]
        if not isinstance(body, dict):
            errors.append(f"{rel}: '{section}' must be an object of filename -> reason")
            continue
        for name, reason in sorted(body.items()):
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{rel}: '{section}.{name}' has an empty or non-string reason")
                continue
            clean[section][name] = reason.strip()

    both = sorted(set(clean["exempt"]) & set(clean["own_step"]))
    for name in both:
        errors.append(f"{rel}: '{name}' is in both 'exempt' and 'own_step' — it cannot be both")
    if errors:
        return empty, errors
    return clean, []


def runner_set(runner: Path) -> tuple[set[str], list[str]]:
    """What `run-guards.sh --list` says it will run.

    Asking the runner rather than reimplementing its discovery is the point:
    a reimplementation that drifts from the runner would reconcile against a
    set nothing actually runs, which is this Discussion's defect wearing a
    checker's hat.
    """
    if not runner.exists():
        return set(), [f"{runner.relative_to(REPO_ROOT)} is missing — nothing runs the guards"]
    try:
        proc = subprocess.run(
            ["bash", str(runner), "--list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), [f"could not run {runner.relative_to(REPO_ROOT)} --list: {exc}"]
    if proc.returncode != 0:
        return set(), [
            f"{runner.relative_to(REPO_ROOT)} --list exited {proc.returncode}: "
            f"{proc.stderr.strip() or '(no stderr)'}"
        ]

    names = set()
    counted = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("count:"):
            counted = line.split(":", 1)[1].strip()
            continue
        names.add(line)
    if counted != str(len(names)):
        return set(), [
            f"{runner.relative_to(REPO_ROOT)} --list printed {len(names)} name(s) "
            f"but reported count: {counted}"
        ]
    return names, []


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

    ledger, failures = load_ledger(LEDGER)
    exempt, own_step = ledger["exempt"], ledger["own_step"]
    discovered, runner_errors = runner_set(RUNNER)
    failures.extend(runner_errors)
    texts = {p.name: command_text(p) for p in workflows}

    def wired_in(name: str) -> list[str]:
        """Workflow filenames that reference scripts/ci/<name> in a run: command."""
        return [wf for wf, text in texts.items() if is_referenced(name, text)]

    passes = []
    for name in files:
        hits = wired_in(name)

        if name == RUNNER_NAME:
            # The runner is not a guard; the only question is whether it runs.
            if hits:
                passes.append(f"PASS  {name}  the guard runner, wired: {', '.join(hits)}")
            else:
                failures.append(
                    f"{name} is the guard runner but is referenced by none of "
                    f"{', '.join(sorted(texts))} — it runs nowhere and gates nothing, "
                    f"and every guard it discovers goes with it"
                )
            continue

        in_runner = name in discovered
        claims = [s for s in LEDGER_SECTIONS if name in ledger[s]]

        if len(claims) == 1 and in_runner:
            failures.append(
                f"{name} is ledgered under '{claims[0]}' AND is in "
                f"{RUNNER_NAME}'s discovered set — one of the two is stale"
            )
        elif "own_step" in claims:
            if hits:
                passes.append(
                    f"PASS  {name}  own step in {', '.join(hits)} — {own_step[name]}"
                )
            else:
                failures.append(
                    f"{name} is ledgered under 'own_step', which claims a workflow "
                    f"invokes it directly, but it is referenced by none of "
                    f"{', '.join(sorted(texts))} — it runs nowhere and gates nothing"
                )
        elif "exempt" in claims:
            if hits:
                failures.append(
                    f"{name} is ledgered under 'exempt', which claims nothing runs it, "
                    f"but {', '.join(hits)} references it — one of the two is stale"
                )
            else:
                passes.append(f"PASS  {name}  ledgered — {exempt[name]}")
        elif in_runner and hits:
            # The residue a bad conflict resolution leaves. Before PR-b every
            # guard was a hand-written step; a resolution that keeps one of
            # those steps AND lets the runner discover the same file runs it
            # twice, and without this it passes in silence — the guard works,
            # the job is green, and the only symptom is a duplicated block in
            # a log nobody reads. That is this Discussion's own defect shape
            # arriving through the fix for it, so it fails by name.
            failures.append(
                f"{name} is BOTH discovered by {RUNNER_NAME} and invoked directly by "
                f"{', '.join(hits)} — it would run twice. Delete the workflow step "
                f"(the runner already covers it), or ledger the file under 'own_step' "
                f"if the direct invocation is the one that has to stay"
            )
        elif in_runner:
            passes.append(f"PASS  {name}  run by {RUNNER_NAME}")
        else:
            failures.append(
                f"{name} is present in scripts/ci/ but is not in {RUNNER_NAME}'s "
                f"discovered set and is not listed in scripts/ci/guard-ledger.json "
                f"— it runs nowhere and gates nothing"
            )

    for name in sorted((set(exempt) | set(own_step)) - set(files)):
        failures.append(
            f"{name} is listed in scripts/ci/guard-ledger.json but no such file "
            f"exists in scripts/ci/ — remove the stale ledger entry"
        )

    for name in sorted(discovered - set(files)):
        failures.append(
            f"{RUNNER_NAME} would run {name}, which is not a file in scripts/ci/ "
            f"— the runner and this check disagree about the subject set"
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
        f"({len(discovered)} run by {RUNNER_NAME}, {len(own_step)} own steps, "
        f"{len(exempt)} ledgered as unrun)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
