#!/usr/bin/env python3
"""loop-idle-ratio-timestamp-guard.py -- behavioral guard for the loop-metrics
int-timestamp crash (D#2315).

Background
----------
`.autonomous-team/loop-metrics.jsonl` on the operator host holds one row of
unknown, non-live-writer provenance whose "ts" field is a raw epoch int
rather than an ISO-8601 string, with no "timestamp" key and an unused "iso"
sibling field:

    {"ts": 1784925063, "iso": "2026-07-24T20:31:00Z", "event_count": 3, ...}

`backend/stats_writer.py:loop_idle_ratio_24h` used to call
`datetime.fromisoformat(ts_str.replace("Z", "+00:00"))` on that value
unconditionally, raising `AttributeError: 'int' object has no attribute
'replace'` straight into its RPC caller (surfaced as JSON-RPC `-32000`,
which the Stats-page tile then rendered as "N/A -- no data yet" -- a
separate bug, fixed on the dashboard side; see the tile test under
dashboard/src/pages/stats/__tests__/).

`backend/server.py`'s `loop.timeline` RPC and `backend/health_monitor.py`
each mishandled the same row differently: one passed the raw int straight
through into a payload typed as a string, the other silently zeroed a
freshness comparison with no signal. The fix is one shared parser
(`backend/loop_metrics_ts.py`) with *skip, don't recover* semantics: an
unparseable timestamp value is skipped and reported on stderr, never
raised, and never repaired from a row's `iso` sibling field (no live writer
produces this row shape, so `ts` and `iso` agreeing here is a guess about a
row of unknown provenance -- and recovering it would make this reader
disagree with `run_analyst.load_loop_metrics`'s D#1753 skip semantics about
which rows exist in the same file).

This is a behavioral probe, not a lint over source text: it builds a real
fixture file, calls the real functions, and asserts on return values and
stderr output. The final section per check defeats the fix (reverting to
the pre-fix, naive implementation) and confirms the SAME fixture then
fails -- proving the fixture is load-bearing rather than something that
would pass regardless of whether the fix is present (the D#1984 trap).

Why this lives here and not backend/tests/: no CI job runs that directory
today (ci.yml, D#1477, ~151 known failures) -- a test placed there would
never execute in CI. This script runs as a new step in the existing
"backend (import-smoke)" job instead (do not rename that job -- its name is
string-matched by scripts/lib/ci-status-check.sh's CI_REQUIRED_CHECKS).

Run from the repo root:

    python3 scripts/ci/loop-idle-ratio-timestamp-guard.py

Exit 0: the shared parser is in place and this fixture proves it matters,
        across all three affected readers.
Exit 1: any reader still raises, mis-counts, leaks a non-string, or is
        silent about a skip -- or defeating a fix produced no observable
        difference.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Number of good rows -- must clear loop_idle_ratio_24h's own min-sample
# floor (< 5 --> ratio is None) so the fixture exercises the ratio math,
# not just the "too few samples" early return.
GOOD_ROW_COUNT = 8

# The exact malformed row shape from the operator's real file (D#2315 Spec):
# an epoch int "ts", no "timestamp" key, and a sibling "iso" field the fix
# must NOT fall back to.
BAD_ROW = {
    "ts": 1784925063,
    "iso": "2026-07-24T20:31:00Z",
    "event_count": 3,
    "discussion_count": 20,
    "queue_depth": 0,
    "agents_spawned": 4,
    "prs_merged": 0,
}


def build_fixture(path: Path) -> None:
    """Write GOOD_ROW_COUNT recent ISO-timestamp rows plus one BAD_ROW.

    Timestamps are computed relative to datetime.now(timezone.utc) at guard
    run time, not written as literals -- otherwise the 24h cutoff makes
    this guard pass vacuously as the fixture ages (D#2315 Spec item 6).
    """
    now = datetime.now(timezone.utc)
    lines = []
    for i in range(GOOD_ROW_COUNT):
        ts = (now - timedelta(minutes=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(json.dumps({
            "timestamp": ts,
            "event_count": 1,
            "agents_spawned": 1,
            "idle": False,
        }))
    lines.append(json.dumps(BAD_ROW))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_loop_idle_ratio(fixture_path: Path, loop_metrics_ts, loop_idle_ratio_24h) -> list[str]:
    """AC A/B: stats_writer.loop_idle_ratio_24h doesn't raise, counts only
    the good rows, and reports the skip loudly."""
    failures: list[str] = []

    stderr_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buf):
            result = loop_idle_ratio_24h(str(fixture_path))
    except Exception as exc:  # noqa: BLE001 -- report ANY exception, not just AttributeError
        failures.append(f"loop_idle_ratio_24h raised {type(exc).__name__}: {exc!r} (expected: no raise)")
        return failures

    if not isinstance(result, dict):
        failures.append(f"loop_idle_ratio_24h: expected a dict, got {type(result).__name__}")
    elif set(result.keys()) != {"ratio", "idle_count", "sample_size"}:
        failures.append(f"loop_idle_ratio_24h: unexpected keys {sorted(result.keys())!r}")
    elif result["sample_size"] != GOOD_ROW_COUNT:
        failures.append(
            f"loop_idle_ratio_24h: sample_size expected {GOOD_ROW_COUNT} "
            f"(bad row must be skipped, not counted), got {result['sample_size']!r}"
        )
    else:
        print(f"  loop_idle_ratio_24h(fixture) = {result}")

    stderr_text = stderr_buf.getvalue()
    expected_diagnostic = f"{fixture_path.name}:{GOOD_ROW_COUNT + 1}"
    if expected_diagnostic not in stderr_text:
        failures.append(
            f"loop_idle_ratio_24h: expected a stderr diagnostic naming "
            f"{expected_diagnostic!r} (skip must be loud) -- stderr was {stderr_text!r}"
        )
    else:
        print(f"  stderr diagnostic present: {expected_diagnostic!r}")

    # Canary: revert the shared parser to the pre-fix, naive implementation
    # and confirm the SAME fixture then raises.
    def _naive_parse(ts_value):
        return datetime.fromisoformat(ts_value.replace("Z", "+00:00"))

    orig_parse = loop_metrics_ts.parse_loop_metrics_ts
    loop_metrics_ts.parse_loop_metrics_ts = _naive_parse
    try:
        raised = False
        try:
            loop_idle_ratio_24h(str(fixture_path))
        except AttributeError:
            raised = True
    finally:
        loop_metrics_ts.parse_loop_metrics_ts = orig_parse

    if not raised:
        failures.append(
            "canary: reverting to the naive pre-fix parser did NOT raise on "
            "this fixture -- it cannot discriminate a fixed implementation "
            "from the original crash"
        )
    else:
        print("  canary: naive pre-fix parser reproduces the AttributeError, as expected")

    return failures


def check_loop_timeline(fixture_path: Path) -> list[str]:
    """AC D.9: loop.timeline emits no non-string in a row's timestamp field."""
    failures: list[str] = []
    import backend.server as server  # noqa: PLC0415

    tmp_repo_root = fixture_path.parent / "timeline-repo-root"
    (tmp_repo_root / ".autonomous-team").mkdir(parents=True, exist_ok=True)
    fixture_copy = tmp_repo_root / ".autonomous-team" / "loop-metrics.jsonl"
    fixture_copy.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")

    orig_repo_root = server._REPO_ROOT
    server._REPO_ROOT = tmp_repo_root
    try:
        rows = server._rpc_loop_timeline({"include_test": True})
    except Exception as exc:  # noqa: BLE001
        failures.append(f"loop.timeline raised {type(exc).__name__}: {exc!r} (expected: no raise)")
        server._REPO_ROOT = orig_repo_root
        return failures
    finally:
        server._REPO_ROOT = orig_repo_root

    non_str = [r for r in rows if not isinstance(r.get("timestamp"), str)]
    if non_str:
        failures.append(
            f"loop.timeline: {len(non_str)} row(s) have a non-string timestamp "
            f"(e.g. {non_str[0]!r})"
        )
    else:
        print(f"  loop.timeline: all {len(rows)} rows have a str timestamp")

    return failures


def check_health_monitor(fixture_path: Path) -> list[str]:
    """AC D.10: health_monitor.get_loop_metrics()'s loop_last_run is never a
    non-string, and its skip is reported, not silent."""
    failures: list[str] = []
    import backend.health_monitor as health_monitor  # noqa: PLC0415

    # BAD_ROW must be the LAST row for get_loop_metrics(), which reads
    # last_entry = parsed[-1] -- already true of build_fixture()'s output.
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        result = health_monitor.get_loop_metrics(metrics_path=fixture_path)

    last_run = result.get("loop_last_run")
    if last_run is not None and not isinstance(last_run, str):
        failures.append(
            f"health_monitor.get_loop_metrics(): loop_last_run is "
            f"{type(last_run).__name__} {last_run!r}, expected str | None"
        )
    else:
        print(f"  health_monitor.get_loop_metrics(): loop_last_run = {last_run!r}")

    stderr_text = stderr_buf.getvalue()
    if "skipping malformed row" not in stderr_text:
        failures.append(
            f"health_monitor.get_loop_metrics(): expected a stderr diagnostic "
            f"for the unparseable last-row timestamp -- stderr was {stderr_text!r}"
        )
    else:
        print("  health_monitor.get_loop_metrics(): skip reported on stderr")

    # get_loop_health_dashboard() must not raise either, over the same fixture.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            health_monitor.get_loop_health_dashboard(metrics_path=fixture_path)
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"health_monitor.get_loop_health_dashboard() raised "
            f"{type(exc).__name__}: {exc!r} (expected: no raise)"
        )
    else:
        print("  health_monitor.get_loop_health_dashboard(): did not raise")

    return failures


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    import backend.loop_metrics_ts as loop_metrics_ts  # noqa: PLC0415
    from backend.stats_writer import loop_idle_ratio_24h  # noqa: PLC0415

    all_failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="loop-idle-ratio-guard-") as tmp:
        fixture_path = Path(tmp) / "loop-metrics.jsonl"
        build_fixture(fixture_path)

        print("stats_writer.loop_idle_ratio_24h:")
        all_failures += check_loop_idle_ratio(fixture_path, loop_metrics_ts, loop_idle_ratio_24h)

        print("server.loop.timeline:")
        all_failures += check_loop_timeline(fixture_path)

        print("health_monitor:")
        all_failures += check_health_monitor(fixture_path)

    if all_failures:
        print("\nFAIL loop-idle-ratio-timestamp-guard:")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("\nloop-idle-ratio-timestamp-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
