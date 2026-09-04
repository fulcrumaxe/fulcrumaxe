#!/usr/bin/env python3
"""project-liveness-guard.py — behavioral guard for the Projects-page liveness
signal (D#2314).

Background
----------
The dashboard's per-project liveness probe (`backend.api._probe_liveness`)
used to read three cron-adjacent signals (`.autonomous-team/active-loops.json`,
`/tmp/af-trigger.fifo`, `.autonomous-team/now.md`) — none of which were
written on the operator's host while the team merged 43 PRs in a day. The
probe read `idle` through the team's highest-volume day on record.

The fix replaces those signals with live rows in `fleet.db`. This guard is
not a lint over source text — it is a behavioral probe: it registers a real
process (a genuine child PID, not a hand-inserted row) into a throwaway
fleet.db, asserts the probe answers `active`, kills that process, and
asserts the probe flips to `idle` on the exact same row with no other
change. It also asserts the fix everyone should be most suspicious of —
that two projects never bleed into each other — and that a project whose
fleet key cannot be resolved reads `unknown`, never `idle`.

Per D#2314 Spec item 13: stdlib + sqlite3 only, no network, no GH_TOKEN,
runs against a temp fleet dir via AUTONOMOUS_FLEET_STATE_DIR set before
import (backend.fleet.concurrency reads it into module-level constants at
import time), and never touches the real ~/.autonomous-fleet-state/fleet.db.

Run from the repo root:

    python3 scripts/ci/project-liveness-guard.py

Exit 0: every check passes.
Exit 1: a check failed — prints one `FAIL <detail>` line per failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAILURES: list[str] = []


def _fail(detail: str) -> None:
    FAILURES.append(detail)
    print(f"FAIL {detail}")


def _fresh_fleet_module(fleet_dir: Path):
    """Import backend.fleet.concurrency pointed at a throwaway fleet dir.

    AUTONOMOUS_FLEET_STATE_DIR is read into module-level constants at
    import time (concurrency.py:64-67), so it must be set before the first
    import — and since this guard may run in a process where the module is
    already cached (e.g. re-run in the same interpreter), the constants are
    also monkeypatched directly as a belt-and-braces measure.
    """
    os.environ["AUTONOMOUS_FLEET_STATE_DIR"] = str(fleet_dir)
    if "backend.fleet.concurrency" in sys.modules:
        import importlib
        m = importlib.reload(sys.modules["backend.fleet.concurrency"])
    else:
        import backend.fleet.concurrency as m  # noqa: PLC0415
    m.FLEET_STATE_DIR = fleet_dir
    m.FLEET_DB_PATH = fleet_dir / "fleet.db"
    m.FLEET_CONFIG_PATH = fleet_dir / "config.json"
    return m


def check_real_process_positive_negative(fleet_dir: Path) -> None:
    """Spec item 5: real-process positive -> negative, one variable."""
    fc = _fresh_fleet_module(fleet_dir)
    from backend.api import _probe_liveness  # noqa: PLC0415

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fc.register("proj-a", "guard-agent", "executor", pid=proc.pid)

        result_active = _probe_liveness("proj-a")
        if result_active != "active":
            _fail(f"real-process-positive: expected 'active' with a live PID, got {result_active!r}")

        proc.kill()
        proc.wait(timeout=10)
        # No reap_stale() call here — the read path must filter dead PIDs
        # on its own, without taking a write lock.
        result_idle = _probe_liveness("proj-a")
        if result_idle != "idle":
            _fail(f"real-process-negative: expected 'idle' after kill, got {result_idle!r}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def check_cross_project_isolation(fleet_dir: Path) -> None:
    """Spec item 6: registering project A never lights up project B."""
    fc = _fresh_fleet_module(fleet_dir)
    from backend.api import _probe_liveness  # noqa: PLC0415

    fc.register("proj-a", "guard-agent-2", "executor", pid=os.getpid())
    try:
        result_a = _probe_liveness("proj-a")
        result_b = _probe_liveness("proj-b")
        if result_a != "active":
            _fail(f"cross-project-isolation: expected proj-a 'active', got {result_a!r}")
        if result_b != "idle":
            _fail(f"cross-project-isolation: expected proj-b 'idle', got {result_b!r}")
    finally:
        fc.unregister("proj-a", "guard-agent-2")


def check_no_signal_path(fleet_dir: Path) -> None:
    """Spec item 9: unresolvable / failed read -> 'unknown', never 'idle'."""
    fc = _fresh_fleet_module(fleet_dir)
    from backend.api import _probe_liveness  # noqa: PLC0415

    result_empty = _probe_liveness("")
    if result_empty != "unknown":
        _fail(f"no-signal-empty-name: expected 'unknown' for an unresolvable project, got {result_empty!r}")

    # Corrupt the db file so the read itself raises rather than finding zero rows.
    fleet_dir.mkdir(parents=True, exist_ok=True)
    fc.FLEET_DB_PATH.write_bytes(b"not a sqlite database")
    result_corrupt = _probe_liveness("proj-a")
    if result_corrupt != "unknown":
        _fail(f"no-signal-corrupt-db: expected 'unknown' when the read raises, got {result_corrupt!r}")
    fc.FLEET_DB_PATH.unlink()


def check_name_mismatch_regression() -> None:
    """Spec item 8: the CLI (bash callers) and the Python import (api.py)
    resolve to the byte-identical string from the one resolver."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".autonomous-team").mkdir()
        (root / ".autonomous-team" / "config.json").write_text(json.dumps({
            "repo": "someowner/somerepo",
            "project_name": "somerepo",
        }))

        from backend.fleet.project_name import resolve_project_name  # noqa: PLC0415
        python_side = resolve_project_name(root)

        cli_result = subprocess.run(
            [sys.executable, "-m", "backend.fleet.project_name", str(root)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
        if cli_result.returncode != 0:
            _fail(f"name-mismatch-regression: CLI resolver failed: {cli_result.stderr.strip()}")
            return
        cli_side = cli_result.stdout.strip()

        if python_side != cli_side:
            _fail(
                f"name-mismatch-regression: python import resolved {python_side!r}, "
                f"CLI resolved {cli_side!r} — these must be byte-identical"
            )
        elif python_side != "somerepo":
            _fail(f"name-mismatch-regression: expected 'somerepo', both sides resolved {python_side!r}")


def check_active_agents_never_mutates(fleet_dir: Path) -> None:
    """Spec item 3: active_agents() connects mode=ro, runs no DDL, performs no write."""
    fc = _fresh_fleet_module(fleet_dir)

    fc.register("proj-ro", "guard-agent-3", "executor", pid=os.getpid())
    try:
        before_mtime = fc.FLEET_DB_PATH.stat().st_mtime_ns
        rows = fc.active_agents("proj-ro")
        after_mtime = fc.FLEET_DB_PATH.stat().st_mtime_ns
        if not rows:
            _fail("active-agents-no-mutate: expected at least one row for proj-ro")
        if before_mtime != after_mtime:
            _fail("active-agents-no-mutate: fleet.db mtime changed after a read-only call")

        # Spec item 3 assertion: run against a fleet dir made read-only — it
        # returns rows without raising.
        os.chmod(fleet_dir, 0o555)
        try:
            ro_rows = fc.active_agents("proj-ro")
            if not ro_rows:
                _fail("active-agents-readonly-dir: expected rows to be returned from a chmod a-w fleet dir")
        except Exception as exc:  # noqa: BLE001
            _fail(f"active-agents-readonly-dir: raised {exc!r} against a read-only fleet dir")
        finally:
            os.chmod(fleet_dir, 0o755)
    finally:
        fc.unregister("proj-ro", "guard-agent-3")


def check_never_touches_real_state() -> None:
    """Verify this guard's own runs never touch the real fleet.db."""
    real_fleet_db = Path.home() / ".autonomous-fleet-state" / "fleet.db"
    if not real_fleet_db.exists():
        return  # nothing to compare against on this host — not a failure
    before = real_fleet_db.stat().st_mtime_ns
    # Run one more full cycle after taking the baseline, to catch a regression
    # that only shows up once the module has already been imported once.
    with tempfile.TemporaryDirectory() as td:
        check_cross_project_isolation(Path(td) / "fleet-state")
    after = real_fleet_db.stat().st_mtime_ns
    if before != after:
        _fail("real-state-touched: ~/.autonomous-fleet-state/fleet.db mtime changed during this guard's run")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="project-liveness-guard-") as td:
        fleet_dir = Path(td) / "fleet-state"
        fleet_dir.mkdir()

        check_real_process_positive_negative(fleet_dir)
        check_cross_project_isolation(fleet_dir)
        check_no_signal_path(fleet_dir)
        check_active_agents_never_mutates(fleet_dir)

    check_name_mismatch_regression()
    check_never_touches_real_state()

    if FAILURES:
        print(f"project-liveness-guard: {len(FAILURES)} check(s) failed")
        return 1

    print("project-liveness-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
