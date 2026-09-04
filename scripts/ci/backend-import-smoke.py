#!/usr/bin/env python3
"""Import-smoke check for backend/*.py — CI gate for D#1608.

Imports every top-level backend/*.py module and fails loudly if any of them
raise on import. This is deliberately NOT the full backend/tests suite:
that suite is known red (D#1477, ~151 failures from the unreconciled D#1411
FastAPI migration) and would make the job permanently red on main. Import
health is a much cheaper, green-able signal that still catches the most
common fork-PR regression (a broken import).

Run from the repo root: python3 scripts/ci/backend-import-smoke.py
"""
import importlib
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

# Modules that are intentionally excluded from the import-smoke check,
# with the reason each one is excluded. Keep this list short — an entry
# here should mean "this module cannot be import-smoke-tested standalone",
# not "this module is currently broken" (a broken module belongs in the
# failure list, not here).
EXCLUDE = {
    # (none today — every top-level backend/*.py module imports cleanly.
    # If a future module needs to be excluded, e.g. a daemon that binds a
    # socket at import time, add it here with a one-line reason.)
}


def discover_modules():
    return sorted(
        p.stem
        for p in BACKEND_DIR.glob("*.py")
        if p.stem != "__init__" and p.stem not in EXCLUDE
    )


def main():
    sys.path.insert(0, str(REPO_ROOT))
    modules = discover_modules()
    failures = []
    for mod in modules:
        qualified = f"backend.{mod}"
        try:
            importlib.import_module(qualified)
        except Exception as exc:  # noqa: BLE001 - we want to catch and report everything
            failures.append((mod, repr(exc)))

    print(f"backend import-smoke: {len(modules)} modules checked, {len(failures)} failed")
    if failures:
        print()
        for mod, err in failures:
            print(f"FAIL backend.{mod}: {err}")
        return 1

    print("backend import-smoke: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
