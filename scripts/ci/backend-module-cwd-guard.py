#!/usr/bin/env python3
"""backend-module-cwd-guard.py — every `python3 -m backend...` in scripts/ must
be anchored to its own repo root, not to whatever cwd the caller happened to
have (D#2325).

The defect
----------
`python3 -m backend...` resolves `backend` from ``sys.path[0]``, which is the
process's *working directory*. A shell script that neither anchors PYTHONPATH
to its own tree nor cd's there is therefore correct only when the caller
happens to be standing in the repo root.

Two live sites had this. They failed differently, and the second shape is the
reason this guard is a static scan rather than a runtime smoke test:

* ``scripts/cron/backfill-agent-runs.sh`` computed ``REPO_ROOT`` and never
  applied it. From cron's cwd (``/`` or ``$HOME``) the call raised
  ``ModuleNotFoundError: No module named 'backend'`` — loud, if anyone had
  been reading the stderr it was redirected away from.

* ``scripts/hooks/post-agent.d/anomaly-check.sh`` is *sourced* by
  ``scripts/post-agent-hook.sh``, so it inherits the finishing agent's cwd —
  for an executor, a worktree under ``.claude/worktrees/``. ``backend`` has no
  ``__init__.py``; it is a namespace package. So that import did not fail. It
  **succeeded against the wrong tree** — the finishing agent's own branch, in
  whatever state that agent left it. There is no exception to catch and no
  exit code to check. Nothing anywhere reported it.

A guard that only caught the first shape would have passed the worse of the
two. What both have in common is not their runtime behaviour, it is their
text: no anchor in the file. That is what this checks.

What counts as anchored
-----------------------
A call site passes when at least one of these holds:

1. **Hoisted export** — a line ``export PYTHONPATH=...`` naming a root-ish
   variable, earlier in the same file. This is the preferred form: a call
   added to the file later is correct without anyone remembering.
2. **Hoisted cd** — a real ``cd`` to a root-ish variable, earlier in the same
   file, at statement position (a ``cd`` inside ``$(...)`` sets a variable and
   moves nothing, so those are stripped before looking).
3. **Call-local prefix** — ``PYTHONPATH="$REPO_ROOT" python3 -m backend...``
   on the call's own line.

Form 3 is accepted deliberately, and this is a divergence from a literal
reading of the Spec's "the containing script neither exports PYTHONPATH ...
nor cd's to it". Measured on the code plane at the time of writing: 5 of the 8
files carrying these calls use the prefix form and are correct at runtime.
Rejecting them would put a red required check on ``main`` and block every
merge in the repo — the exact outcome this Discussion's PR ordering existed to
avoid. The prefix is still the weaker form and the failure message says so.

Prefer false negatives here. An over-blocking CI guard gets switched off,
which is worse than the gap it was guarding. Hence: a permissive notion of
"root-ish variable", comments stripped rather than parsed, and an exemption
ledger (``backend-module-cwd-exempt.json``) rather than a hardcoded skip list.

Where it runs
-------------
A step in the ``backend (import-smoke)`` job of ``.github/workflows/ci.yml``.
A non-zero exit fails that job, and that job name is in ``CI_REQUIRED_CHECKS``
(``scripts/lib/ci-status-check.sh``), so a failure blocks the merge. Nothing
lands in ``backend/tests/`` — no CI job runs that directory, so a test there
would gate nothing, which is the very pattern this Discussion is about.

Usage::

    python3 scripts/ci/backend-module-cwd-guard.py           # scan, exit 0/1
    python3 scripts/ci/backend-module-cwd-guard.py --list    # subject files

Exit 0: every call site is anchored or honestly ledgered.
Exit 1: an unanchored call site, a stale/reasonless ledger entry, or a scan
        that discovered no shell scripts at all.
Exit 2: bad usage.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LEDGER = Path(__file__).resolve().parent / "backend-module-cwd-exempt.json"

LEDGER_KEYS = {"note", "exempt"}

# `python3 -m backend`, `python -m backend.x.y`, `python3.11 -m backend`.
# The trailing guard allows a dotted submodule but not `backendfoo`.
CALL_RE = re.compile(r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+backend(?![\w-])")

# Deliberately permissive: any shell variable whose name contains "root".
# Matches $REPO_ROOT, ${REPO_ROOT}, $PROJECT_ROOT, $_acheck_root. Being loose
# here costs a false negative; being tight costs a false positive, and a false
# positive on a required check is the expensive one.
ROOT_REF_RE = re.compile(r"\$\{?[A-Za-z0-9_]*ROOT[A-Za-z0-9_]*\}?", re.IGNORECASE)

EXPORT_RE = re.compile(r"^\s*(?:export|declare\s+-x)\s+PYTHONPATH=")
CD_RE = re.compile(r"(?:^|[;&|(])\s*cd\s+(\S+)")


def strip_comment(line: str) -> str:
    """Drop a `#` comment, honouring quotes.

    A `#` only starts a comment at the start of a line or after whitespace, so
    `${VAR#prefix}` and `$#` survive. Quoted `#` survives too. This is what
    keeps prose out of the scan: `scripts/spawn-agent.sh` and both scripts
    fixed under this Discussion describe `python3 -m backend...` in their
    header comments, and none of those are calls.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(ch)
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def strip_substitutions(line: str) -> str:
    """Remove `$(...)` and backtick spans.

    `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"` contains two
    `cd`s that change nothing about the script's own working directory. They
    are the single most common shape in this repo, and counting one as an
    anchor would pass every unanchored script that computes its own root —
    which is precisely `scripts/cron/backfill-agent-runs.sh` as it was.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if line.startswith("$(", i):
            depth = 1
            i += 2
            while i < n and depth:
                if line.startswith("$(", i):
                    depth += 1
                    i += 2
                    continue
                if line[i] == "(":
                    depth += 1
                elif line[i] == ")":
                    depth -= 1
                i += 1
            continue
        if line[i] == "`":
            i += 1
            while i < n and line[i] != "`":
                i += 1
            i += 1
            continue
        out.append(line[i])
        i += 1
    return "".join(out)


def is_shell_script(path: Path) -> bool:
    if path.suffix in (".sh", ".bash"):
        return True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return bool(re.match(r"^#!.*\b(?:ba|da|z|k)?sh\b", first))


def subjects(scripts_dir: Path) -> list[Path]:
    """Every shell script under scripts/, archives excluded, sorted."""
    found = []
    for path in scripts_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if "archive" in path.relative_to(scripts_dir).parts:
            continue
        if is_shell_script(path):
            found.append(path)
    return sorted(found)


def scan(path: Path) -> list[dict]:
    """Every `python3 -m backend` call in one script, with its anchor verdict."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    export_at: int | None = None
    cd_at: int | None = None
    results: list[dict] = []

    for num, raw in enumerate(lines, start=1):
        code = strip_comment(raw)
        if not code.strip():
            continue

        match = CALL_RE.search(code)
        if match:
            prefix = code[: match.start()]
            local = "PYTHONPATH=" in prefix and ROOT_REF_RE.search(prefix) is not None
            if local:
                anchor = "call-local PYTHONPATH= prefix on this line"
            elif export_at is not None:
                anchor = f"export PYTHONPATH= at line {export_at}"
            elif cd_at is not None:
                anchor = f"cd to repo root at line {cd_at}"
            else:
                anchor = None
            results.append({"line": num, "text": raw.strip(), "anchor": anchor})

        if export_at is None and EXPORT_RE.match(code) and ROOT_REF_RE.search(code):
            export_at = num
        if cd_at is None:
            bare = strip_substitutions(code)
            for arg in CD_RE.findall(bare):
                if ROOT_REF_RE.search(arg):
                    cd_at = num
                    break

    return results


def load_ledger(path: Path) -> tuple[dict[str, str], list[str]]:
    if not path.exists():
        return {}, [f"{path.name} is missing"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.name} is not readable JSON: {exc}"]
    if not isinstance(raw, dict):
        return {}, [f"{path.name} must be a JSON object, got {type(raw).__name__}"]
    unknown = sorted(set(raw) - LEDGER_KEYS)
    if unknown:
        return {}, [f"{path.name} has unknown top-level key(s): {', '.join(unknown)}"]
    if "exempt" not in raw:
        return {}, [f"{path.name} is missing its required 'exempt' object"]
    exempt = raw["exempt"]
    if not isinstance(exempt, dict):
        return {}, [f"{path.name}: 'exempt' must be an object of script path -> reason"]

    errors: list[str] = []
    clean: dict[str, str] = {}
    for name, reason in sorted(exempt.items()):
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{path.name}: '{name}' has an empty or non-string reason")
            continue
        clean[name] = reason.strip()
    return clean, errors


REMEDY = """
Remedy — anchor the script to its own root, once, near the top:

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"          # adjust the depth
    export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

  Prepend, never overwrite: a caller's PYTHONPATH is not yours to discard.

  If the file is SOURCED rather than executed — everything under
  scripts/hooks/post-agent.d/ is — that bare `export` lands in the caller's
  own environment and in every subprocess it starts afterwards, which is a
  new bug in place of the old one. Wrap the file's body in a subshell so the
  export dies with it. scripts/hooks/post-agent.d/anomaly-check.sh is the
  worked example.

  A call-local `PYTHONPATH="$REPO_ROOT" python3 -m backend...` prefix also
  satisfies this guard and several call sites use it, but it is the weaker
  form: it is correct only for the call it is attached to, and the next call
  added to the file is wrong by default.

  If a script genuinely must stay unanchored, add it to
  scripts/ci/backend-module-cwd-exempt.json with a reason a reviewer can
  check.
""".rstrip()


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--list"):
        print(f"usage: {Path(sys.argv[0]).name} [--list]", file=sys.stderr)
        return 2

    if not SCRIPTS_DIR.is_dir():
        print(f"backend-module-cwd-guard: FAIL — {SCRIPTS_DIR} is not a directory", file=sys.stderr)
        return 1

    files = subjects(SCRIPTS_DIR)

    if len(sys.argv) == 2:
        for path in files:
            print(path.relative_to(REPO_ROOT))
        print(f"count: {len(files)}")
        return 0

    # A scan that discovers nothing would report every call site fine. That is
    # the silence this guard exists to remove, so it is a failure — the same
    # reasoning scripts/ci/guard-registry-check.py applies to its own subjects.
    if not files:
        print(
            "backend-module-cwd-guard: FAIL — discovered zero shell scripts under "
            "scripts/; a scan with no subjects cannot vouch for anything",
            file=sys.stderr,
        )
        return 1

    exempt, failures = load_ledger(LEDGER)

    passes: list[str] = []
    unanchored_files: set[str] = set()
    call_count = 0

    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        for call in scan(path):
            call_count += 1
            if call["anchor"]:
                passes.append(f"PASS  {rel}:{call['line']}  {call['anchor']}")
            elif rel in exempt:
                passes.append(f"PASS  {rel}:{call['line']}  ledgered — {exempt[rel]}")
                unanchored_files.add(rel)
            else:
                failures.append(
                    f"{rel}:{call['line']} calls `python3 -m backend` with no anchor "
                    f"to its own repo root — it resolves `backend` from the caller's "
                    f"working directory\n      {call['text']}"
                )

    for name in sorted(set(exempt) - unanchored_files):
        failures.append(
            f"{name} is listed in scripts/ci/backend-module-cwd-exempt.json but has "
            f"no unanchored `python3 -m backend` call — the exemption is stale"
        )

    for line in passes:
        print(line)
    print(
        f"backend-module-cwd-guard: {len(files)} shell script(s) scanned, "
        f"{call_count} call site(s) found"
    )

    if failures:
        print("\nbackend-module-cwd-guard: FAIL", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(REMEDY, file=sys.stderr)
        return 1

    print("backend-module-cwd-guard: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
