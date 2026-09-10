#!/usr/bin/env python3
"""hook-registry-check.py — reconcile hooks/ against what actually registers it (D#2362 PR-a).

Background
----------
D#2362 measured that the tracked settings files register 4 of hooks/'s 16
files. Nothing reconciles hooks/ against the settings that would invoke it,
so a new file can sit there fully wired to nothing and nobody would know
until a filed Discussion goes and looks by hand — as happened with
hooks/repo_scope_warn.py, whose test suite and review history never once
asked whether it runs.

Unlike scripts/ci/ (see guard-registry-check.py, the template for this file),
hooks/ has no auto-discovery runner: every Claude Code hook is registered by
naming its path explicitly in a settings file's `command`. So this file's
three-way reconciliation collapses guard-registry-check.py's "discovered by
the runner OR own_step OR exempt" into "referenced by a settings file OR
own_step OR exempt" — own_step is kept, structurally identical to the
template, for a hook that IS registered but whose registration is worth a
recorded reason beyond the default pass.

What this checks
-----------------
Every regular file in hooks/ is exactly one of

  1. referenced by a `command` in some settings file (.claude/settings.json,
     .claude/settings.local.json, or ~/.claude/settings.json) — the normal
     case, no ledger entry required, or
  2. listed under "own_step" in hooks/hook-ledger.json with a non-empty
     reason AND actually referenced by a settings file, or
  3. listed under "exempt" in hooks/hook-ledger.json with a non-empty reason
     AND referenced by no settings file at all.

The two ledger sections are checked in opposite directions on purpose, exactly
as guard-registry-check.py does: "own_step" claims a settings file registers
the hook, so an entry no settings file references is a lie the check catches;
"exempt" claims nothing registers it, so an entry a settings file does
reference is a stale claim the check also catches. A ledger entry naming a
file that no longer exists fails for the same reason.

A missing ~/.claude/settings.json (operator-local, absent in CI) or a missing
.claude/settings.local.json (gitignored, absent unless an operator ran an
installer) contributes zero registrations rather than erroring — a
contributor without either file must still get a clean run.

Library verification — the second, more consequential defect this file
avoids
------------------------------------------------------------------------
D#2362's filing named 4 "library" modules (hooks/_retry_common.py,
hooks/repo_root.py, hooks/sandbox_rules.py, hooks/__init__.py) that should be
ledgered exempt because another hook imports them, not because they are
independently unregistered entry points. Measuring which files are actually
imported by another hooks/*.py module — by executing the import and reading
sys.modules, not by matching a filename pattern like a leading underscore or
a "_rules.py" suffix — turned up three more: hooks/background_rules.py,
hooks/payload_shape.py and hooks/spawn_tag_redaction.py are each imported
only by hooks/sandbox.py, so nothing about their name would have flagged
them; only running the import does.

That measurement runs on every check, not just once at authoring time: any
hooks/*.py file this check discovers to be imported by another hooks/*.py
file (via a real, fresh `python3 -c "import hooks.<x>"` subprocess, not a
heuristic) MUST have a ledger 'exempt' entry, or the check fails naming it.
A ledger entry claiming a file is a library while nothing imports it is not
specially penalized here — the ordinary exempt-vs-referenced check already
disproves any such claim if that file is ever registered directly.

Run from anywhere:

    python3 scripts/ci/hook-registry-check.py          # reconcile, exit 0/1
    python3 scripts/ci/hook-registry-check.py --list    # print the subject set

Exit 0: every file in hooks/ is registered, honestly ledgered, or a verified
        library import target with a ledger entry.
Exit 1: a file is none of those, a ledger entry is stale, reasonless, or
        names a deleted file, an import could not be verified, or hooks/
        turned up empty.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
LEDGER = HOOKS_DIR / "hook-ledger.json"
LEDGER_NAME = LEDGER.name

# Every settings file that can register a Claude Code hook for this repo.
# The user-level and repo-local files are optional by design (see module
# docstring) — a missing one contributes no registrations, not an error.
SETTINGS_FILES = [
    REPO_ROOT / ".claude" / "settings.json",
    REPO_ROOT / ".claude" / "settings.local.json",
    Path.home() / ".claude" / "settings.json",
]

LEDGER_KEYS = {"note", "exempt", "own_step"}
LEDGER_SECTIONS = ("exempt", "own_step")


def discover(hooks_dir: Path) -> list[str]:
    """Every regular file in hooks_dir except the ledger itself, sorted."""
    return sorted(p.name for p in hooks_dir.iterdir() if p.is_file() and p.name != LEDGER_NAME)


def collect_commands(path: Path) -> list[str]:
    """Every 'command' string value under a settings file's "hooks" tree.

    Parses the actual JSON structure rather than grepping raw text, so a
    hook path mentioned in some unrelated string value (there is no comment
    syntax in JSON, but nothing stops a stray description field) is not
    mistaken for wiring.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []

    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "command" and isinstance(value, str):
                    out.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw.get("hooks", {}))
    return out


def is_referenced(name: str, commands: list[str]) -> bool:
    """True when hooks/<name> appears as a whole path in some command string.

    Matching only the trailing "hooks/<name>" suffix (not the literal
    "$CLAUDE_PROJECT_DIR/hooks/<name>" prefix settings files actually use)
    means this is correct whether a command spells the hook path via the
    $CLAUDE_PROJECT_DIR variable or an absolute path — no variable
    resolution needed either way.
    """
    pattern = re.compile(re.escape(f"hooks/{name}") + r"(?![\w.-])")
    return any(pattern.search(cmd) for cmd in commands)


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


def modname(fname: str) -> str:
    """hooks/<fname> -> the module name importing it would use."""
    stem = fname[:-3] if fname.endswith(".py") else fname
    return "hooks" if stem == "__init__" else f"hooks.{stem}"


def import_closure(fname: str) -> tuple[set[str], list[str]]:
    """The hooks.* module names (plus bare "hooks") loaded by a fresh import
    of fname's module, executed in a clean subprocess so nothing leaks
    between files. Returns (loaded, errors) — errors means the import itself
    could not be verified, which is reported as a check failure rather than
    silently skipped.

    A stem that is not a valid Python identifier (a hyphen, say) can never be
    reached via `import hooks.<stem>` in the first place, so no other
    hooks/*.py file could import it as a library either — there is nothing
    to verify, and attempting it would only produce a SyntaxError that reads
    as an import failure rather than the "not a library" fact it actually is.
    """
    mod = modname(fname)
    stem = fname[:-3] if fname.endswith(".py") else fname
    if stem != "__init__" and not stem.isidentifier():
        return set(), []
    code = (
        "import sys\n"
        f"import {mod}\n"
        "print('\\n'.join(sorted(k for k in sys.modules "
        "if k == 'hooks' or k.startswith('hooks.'))))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), [f"could not import {mod} to verify library status: {exc}"]
    if proc.returncode != 0:
        last = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "(no stderr)"
        return set(), [f"importing {mod} failed, so its library status could not be verified: {last}"]
    return {line for line in proc.stdout.split() if line}, []


def compute_library_files(files: list[str]) -> tuple[set[str], list[str]]:
    """Files whose module is loaded as a side effect of importing some OTHER
    hooks/*.py file, verified by actually running the import — never by a
    filename heuristic or an extension rule.
    """
    name_of = {modname(f): f for f in files}
    per_file_loaded: dict[str, set[str]] = {}
    errors: list[str] = []
    for f in files:
        loaded, errs = import_closure(f)
        if errs:
            errors.extend(errs)
            continue
        per_file_loaded[f] = loaded

    library_files: set[str] = set()
    for f, loaded in per_file_loaded.items():
        for modkey in loaded:
            other = name_of.get(modkey)
            if other is None or other == f:
                continue
            library_files.add(other)
    return library_files, errors


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--list"):
        print(f"usage: {Path(sys.argv[0]).name} [--list]", file=sys.stderr)
        return 2

    if not HOOKS_DIR.is_dir():
        print(f"hook-registry-check: FAIL — {HOOKS_DIR} is not a directory", file=sys.stderr)
        return 1

    files = discover(HOOKS_DIR)

    if len(sys.argv) == 2:
        for name in files:
            print(name)
        print(f"count: {len(files)}")
        return 0

    # An empty subject set is the silent-skip this file exists to prevent.
    if not files:
        print(
            "hook-registry-check: FAIL — discovered zero files in hooks/; "
            "a reconciliation with no subjects cannot vouch for anything",
            file=sys.stderr,
        )
        return 1

    ledger, failures = load_ledger(LEDGER)
    exempt, own_step = ledger["exempt"], ledger["own_step"]

    present_settings = [p for p in SETTINGS_FILES if p.is_file()]
    commands: list[str] = []
    for p in present_settings:
        commands.extend(collect_commands(p))
    settings_labels = ", ".join(
        str(p) if p.is_relative_to(REPO_ROOT) else "~/.claude/settings.json" for p in present_settings
    ) or "(no settings files present)"

    def registered(name: str) -> bool:
        return is_referenced(name, commands)

    library_files, import_errors = compute_library_files(files)
    failures.extend(import_errors)

    passes = []
    for name in files:
        hits = registered(name)
        claims = [s for s in LEDGER_SECTIONS if name in ledger[s]]

        if "own_step" in claims:
            if hits:
                passes.append(f"PASS  {name}  own_step, registered — {own_step[name]}")
            else:
                failures.append(
                    f"{name} is ledgered under 'own_step', which claims a settings file "
                    f"registers it, but it is referenced by none of {settings_labels} "
                    f"— it runs nowhere and gates nothing"
                )
        elif "exempt" in claims:
            if hits:
                failures.append(
                    f"{name} is ledgered under 'exempt', which claims no settings file "
                    f"references it, but it IS referenced ({settings_labels}) — one of "
                    f"the two is stale"
                )
            else:
                note = " (verified: imported by another hooks/*.py module)" if name in library_files else ""
                passes.append(f"PASS  {name}  ledgered{note} — {exempt[name]}")
        elif hits:
            passes.append(f"PASS  {name}  registered in settings")
        elif name in library_files:
            failures.append(
                f"{name} is imported by another hooks/*.py module (verified by an actual "
                f"import, not a filename heuristic) but has no entry in "
                f"hooks/hook-ledger.json — add it under 'exempt' naming the importer(s)"
            )
        else:
            failures.append(
                f"{name} is present in hooks/ but is not referenced by any of "
                f"{settings_labels} and is not listed in hooks/hook-ledger.json "
                f"— it runs nowhere and gates nothing"
            )

    for name in sorted((set(exempt) | set(own_step)) - set(files)):
        failures.append(
            f"{name} is listed in hooks/hook-ledger.json but no such file exists in "
            f"hooks/ — remove the stale ledger entry"
        )

    if failures:
        for line in failures:
            print(f"hook-registry-check: FAIL — {line}", file=sys.stderr)
        print(
            f"hook-registry-check: {len(failures)} problem(s) across {len(files)} file(s) in hooks/",
            file=sys.stderr,
        )
        return 1

    for line in passes:
        print(line)
    print(
        f"hook-registry-check: OK — {len(files)} files "
        f"({sum(1 for f in files if registered(f) and f not in exempt and f not in own_step)} "
        f"registered, {len(own_step)} own_step, {len(exempt)} exempt "
        f"[{len(library_files)} verified library imports])"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
