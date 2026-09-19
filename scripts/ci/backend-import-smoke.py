#!/usr/bin/env python3
"""Import-smoke check for backend/*.py and scripts/lib/*.py — CI gate for
D#1608, widened by D#2588.

Imports every top-level backend/*.py module (by dotted module name) and every
top-level scripts/lib/*.py module (by file path — several of those stems,
e.g. cross-file-detector.py, are not legal Python identifiers) and fails
loudly if any of them raise on import. This is deliberately NOT the full
backend/tests suite: that suite is known red (D#1477, ~151 failures from the
unreconciled D#1411 FastAPI migration) and would make the job permanently
red on main. Import health is a much cheaper, green-able signal that still
catches the most common fork-PR regression (a broken import).

scripts/lib/ holds the trust-boundary modules (external_intake_gate.py,
pr_intake_gate.py, trust_id_resolver.py, pr_comment_trust.py, ...) that ruff
parses and lints but never imports — a module-level `raise`, a symbol an
upstream `backend.*` change removed, or a missing third-party dependency all
parse cleanly and fail only once something actually imports the module. On
the code plane, at a gate, nothing did (D#2588).

Run from the repo root: python3 scripts/ci/backend-import-smoke.py
"""
import importlib
import importlib.util
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
SCRIPTS_LIB_DIR = REPO_ROOT / "scripts" / "lib"

# Modules that are intentionally excluded from the backend/ import-smoke
# check, with the reason each one is excluded. Keep this list short — an
# entry here should mean "this module cannot be import-smoke-tested
# standalone", not "this module is currently broken" (a broken module
# belongs in the failure list, not here).
EXCLUDE = {
    # (none today — every top-level backend/*.py module imports cleanly.
    # If a future module needs to be excluded, e.g. a daemon that binds a
    # socket at import time, add it here with a one-line reason.)
}

# Same idea, scoped to scripts/lib/*.py. Unlike backend/*.py, this set is
# NOT empty (D#2588): three modules resolve BOT_ACCOUNT at module import
# time (external_intake_gate.py's `_resolve_bot_account()`, D#1905) via
# AUTONOMOUS_TEAM_BOT_ACCOUNT or .autonomous-team/config.json's
# "bot_account" field only — no git-config-style fallback the way
# backend/_repo.py has for the repo slug. .autonomous-team/config.json is
# gitignored, so a clean checkout (this repo's own CI included) has
# neither, and importing these three raises there every time. Widening the
# check to cover them anyway would turn the required "backend
# (import-smoke)" job red on the code plane's own clean HEAD, so per
# D#2588 they are excluded here instead, with the reason each is excluded
# rather than a silent try/except — see the PR body for the measurement.
SCRIPTS_LIB_EXCLUDE = {
    "external_intake_gate": (
        "resolves BOT_ACCOUNT at module level with no fallback beyond an "
        "env var / .autonomous-team/config.json, both absent on a clean "
        "checkout — see D#2588"
    ),
    "pr_comment_trust": (
        "imports external_intake_gate at module level and inherits its "
        "BOT_ACCOUNT resolution failure on a clean checkout — see D#2588"
    ),
    "pr_intake_gate": (
        "imports external_intake_gate at module level and inherits its "
        "BOT_ACCOUNT resolution failure on a clean checkout — see D#2588"
    ),
}


def discover_modules():
    return sorted(
        p.stem
        for p in BACKEND_DIR.glob("*.py")
        if p.stem != "__init__" and p.stem not in EXCLUDE
    )


def discover_scripts_lib_files():
    if not SCRIPTS_LIB_DIR.is_dir():
        return []
    return sorted(
        p for p in SCRIPTS_LIB_DIR.glob("*.py") if p.stem not in SCRIPTS_LIB_EXCLUDE
    )


def import_by_path(path):
    """Import a single .py file by file path, not by dotted module name.

    scripts/lib/ holds files such as cross-file-detector.py whose stem
    contains a hyphen. `importlib.import_module("scripts.lib.cross-file-"
    "detector")` actually succeeds — the import machinery resolves dotted
    names by looking up files on disk, not by validating identifiers — but
    the plain `import scripts.lib.cross-file-detector` statement a caller
    would otherwise write is a SyntaxError (the parser rejects the hyphen
    before either name ever reaches the import system). Loading by
    spec_from_file_location sidesteps both: the synthetic name below only
    has to be a legal sys.modules key, never something written as literal
    import syntax.
    """
    synthetic_name = "backend_import_smoke__scripts_lib__" + re.sub(
        r"\W", "_", path.stem
    )
    spec = importlib.util.spec_from_file_location(synthetic_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(synthetic_name, None)


def main():
    sys.path.insert(0, str(REPO_ROOT))

    modules = discover_modules()
    failures = []
    for mod in modules:
        qualified = f"backend.{mod}"
        try:
            importlib.import_module(qualified)
        except Exception as exc:  # noqa: BLE001 - we want to catch and report everything
            failures.append((qualified, repr(exc)))

    lib_files = discover_scripts_lib_files()
    for path in lib_files:
        try:
            import_by_path(path)
        except Exception as exc:  # noqa: BLE001 - same as above
            failures.append((f"scripts/lib/{path.name}", repr(exc)))

    total = len(modules) + len(lib_files)
    print(f"backend import-smoke: {total} modules checked, {len(failures)} failed")
    print(f"  backend/*.py: {len(modules)} modules")
    if lib_files:
        print(f"  scripts/lib/*.py: {len(lib_files)} modules")
    else:
        print("  scripts/lib/*.py: subject set was empty (0 modules)")

    if failures:
        print()
        for name, err in failures:
            print(f"FAIL {name}: {err}")
        return 1

    print("backend import-smoke: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
