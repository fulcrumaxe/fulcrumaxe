"""scripts/tests/test_hook_event_init_failure_reporting.py

Proves the D#2105 fix in scripts/lib/hook-event.sh: `hook_event_init`'s exit
status used to be nothing but `_hook_event_write_marker`'s `mv` — it reported
the marker, never the lock. A mode-000 lock file lets `touch` succeed (utime
works for the owner regardless of mode) while `exec 200>` fails EACCES; the
old code never checked that, so it went on to export `HOOK_EVENT_FD=200`,
write the marker (a different, writable path), and return 0 — advertising a
lock descriptor it never held.

Reproduced against current main before writing this file:
  hook_event_id=executor-1803-1787283902
  init_rc=0
  HOOK_EVENT_FD=200
  fd200_usable=no      (flock -n -x 200 fails — the fd was never opened)
  marker=YES

Two independent things are proved here:

  1. Criteria 1-3 (this file's main subject): the mode-000-lock case must
     not return 0, must emit a greppable stderr diagnostic, and must never
     let HOOK_EVENT_FD=200 reach a consumer while unusable. The harness
     below prints an FD marker *after* the hook_event_init call specifically
     so its absence proves hook_event_init exited before advertising it —
     not that the harness merely forgot to print it.

  2. Criterion 4: an event id long enough to blow NAME_MAX (255 on
     ext4/xfs/most Linux filesystems) must never reach touch/exec/mv at all.

  The read-only-directory case the original filing proposed is deliberately
  NOT used here: `init_rc=1` already on unfixed main for that case (the
  marker write fails too, coincidentally masking the real defect), so a
  criterion of "does not return 0" against it would pass unmodified — the
  can't-fail test the Spec explicitly warns against. The mode-000 lock file
  is the cheapest way to fail the lock alone while the marker still writes.

Mutation proof (recorded here, run this session): reverting the checked
touch/exec/flock sequence back to the old unconditional one and re-running
test_lock_failure_returns_nonzero, test_lock_failure_emits_diagnostic, and
test_lock_failure_no_false_descriptor turns all three red — see the PR body
for the exact commands and output.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_EVENT_LIB = REPO_ROOT / "scripts" / "lib" / "hook-event.sh"

# Canonical three-segment id shape used elsewhere in this test suite
# (test_event_id_containment.py's test_canonical_three_segment_id_unaffected).
CANONICAL_EVENT_ID = "executor-1803-1787283900"

# Same id used in the Spec's own reproduction of the masked-lock case.
MODE000_EVENT_ID = "executor-1803-1787283902"

# 320 chars: "executor-" + 300 digits + "-1787349862" — well past NAME_MAX (255).
OVERLONG_EVENT_ID = "executor-" + ("9" * 300) + "-1787349862"


def _harness(tmp_path: Path, event_id: str) -> Path:
    """Write a harness script that calls hook_event_init directly, then
    (only reachable if hook_event_init returned) prints markers proving
    what state it left behind — no false HOOK_EVENT_FD, whether INIT_OK
    would print, and whether a marker file exists."""
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f'source "{HOOK_EVENT_LIB}"\n'
        f'hook_event_init "test-hook" "step1" --event-id "{event_id}"\n'
        'echo "HOOK_EVENT_FD=${HOOK_EVENT_FD:-UNSET}"\n'
        'echo INIT_OK\n'
    )
    return harness


def _run(tmp_path: Path, event_id: str):
    hook_event_dir = tmp_path / "hook-events"
    hook_event_dir.mkdir(parents=True, exist_ok=True)
    harness = _harness(tmp_path, event_id)
    env = dict(os.environ)
    env["HOOK_EVENT_DIR"] = str(hook_event_dir)
    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return hook_event_dir, proc


def _make_mode000_lock(hook_event_dir: Path, event_id: str) -> None:
    lock = hook_event_dir / f"{event_id}.lock"
    lock.touch()
    lock.chmod(0o000)


# ── Criteria 1-3: masked lock failure must not report success ─────────────


def test_lock_failure_returns_nonzero(tmp_path):
    hook_event_dir = tmp_path / "hook-events"
    hook_event_dir.mkdir(parents=True)
    _make_mode000_lock(hook_event_dir, MODE000_EVENT_ID)
    _, proc = _run(tmp_path, MODE000_EVENT_ID)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)


def test_lock_failure_emits_diagnostic_marker(tmp_path):
    hook_event_dir = tmp_path / "hook-events"
    hook_event_dir.mkdir(parents=True)
    _make_mode000_lock(hook_event_dir, MODE000_EVENT_ID)
    _, proc = _run(tmp_path, MODE000_EVENT_ID)
    assert "[hook-event] INIT_FAILED:" in proc.stderr, proc.stderr


def test_lock_failure_no_false_descriptor(tmp_path):
    hook_event_dir = tmp_path / "hook-events"
    hook_event_dir.mkdir(parents=True)
    _make_mode000_lock(hook_event_dir, MODE000_EVENT_ID)
    _, proc = _run(tmp_path, MODE000_EVENT_ID)
    # hook_event_init must have exited before the harness's own post-call
    # echo lines ran — proving no caller ever sees HOOK_EVENT_FD=200 or the
    # "INIT_OK" success shape for this call.
    assert "HOOK_EVENT_FD=200" not in proc.stdout, proc.stdout
    assert "INIT_OK" not in proc.stdout, proc.stdout


# ── Criterion 4: over-long id must never reach touch/exec/mv ──────────────


def test_overlong_id_rejected_before_touch(tmp_path):
    hook_event_dir, proc = _run(tmp_path, OVERLONG_EVENT_ID)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    combined = proc.stdout + proc.stderr
    assert "File name too long" not in combined, combined
    # hook_event_init unconditionally mkdir -p's HOOK_EVENT_DIR/done before
    # the id is even resolved, so only files (not that pre-existing empty
    # dir) prove nothing was created for this id.
    created_files = [p for p in hook_event_dir.rglob("*") if p.is_file()]
    assert created_files == [], created_files


# ── Criterion 6: happy path stays silent ───────────────────────────────────


def test_happy_path_returns_zero_and_silent(tmp_path):
    hook_event_dir, proc = _run(tmp_path, CANONICAL_EVENT_ID)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stderr == "", proc.stderr
    assert (hook_event_dir / f"{CANONICAL_EVENT_ID}.json").exists()


# ── Regression guard: a successful init must genuinely hold the lock ──────
#
# All five tests above pass even if `flock -x 200` at hook-event.sh:185 is
# mutated to `true` -- that reintroduces the exact defect this Discussion is
# about, one level up: HOOK_EVENT_FD=200 exported while no lock is held, in
# its *success* shape rather than the mode-000 failure shape above. None of
# the failure-path tests exercise the happy path's fd at all, so nothing
# here caught it. This test does: it holds hook_event_init's fd 200 open in
# the harness process (no hook_event_finish call) and spawns a genuinely
# independent `flock` process against the same lock file. If the lock is
# real, that child is refused; if `flock -x 200` was replaced with a no-op,
# the child acquires it.


def test_happy_path_actually_holds_the_lock(tmp_path):
    hook_event_dir = tmp_path / "hook-events"
    hook_event_dir.mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "lock_hold_harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f'source "{HOOK_EVENT_LIB}"\n'
        f'hook_event_init "test-hook" "step1" --event-id "{CANONICAL_EVENT_ID}"\n'
        'echo "init_rc=$?"\n'
        # Independent child process, its own fd: a real conflict with the
        # harness's still-open fd 200 iff hook_event_init actually flock'd it.
        'flock -n -x "$HOOK_EVENT_LOCK" -c true\n'
        'echo "child_lock_rc=$?"\n'
    )
    env = dict(os.environ)
    env["HOOK_EVENT_DIR"] = str(hook_event_dir)
    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert "init_rc=0" in proc.stdout, (proc.stdout, proc.stderr)
    assert "child_lock_rc=0" not in proc.stdout, proc.stdout
