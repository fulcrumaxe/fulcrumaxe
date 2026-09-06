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

A fourth reader was added later (D#2331): `backend/kpi_engine.compute_idle_rate`
carried the same missing guard one line away from a second, worse bug -- it
substituted the *current time* for any timestamp it could not parse
(`(_parse_iso(...) or _now_utc()) >= cutoff`), so a row whose age was unknown
was counted as maximally recent and `last_24h_pct` was inflated by precisely
the rows with the least evidence behind them. The two are entangled: fixing
the parse alone makes the substitution worse, because a row that used to be
dropped before parsing now reaches the `or _now_utc()`. Both fixes are probed
together below.

Exit 0: the shared parser is in place and this fixture proves it matters,
        across all four affected readers.
Exit 1: any reader still raises, mis-counts, leaks a non-string, dates an
        unreadable row to now, or is silent about a skip -- or defeating a fix
        produced no observable difference.
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


def _write_kpi_fixture(path: Path, *, good_rows: int, good_age_hours: float, bad: list) -> None:
    """Write a loop-metrics fixture for the kpi_engine checks.

    *good_rows* rows carrying real ISO timestamps (``datetime.isoformat()``
    form, i.e. a "+00:00" offset -- the shape the existing
    backend/tests/test_kpi_engine.py fixtures use), aged *good_age_hours* back
    from now, alternating idle True/False. Then one row per entry in *bad*,
    each carrying that raw value under "timestamp" and ``idle: true``.

    Ages are relative to now, never literals, so the 24h cutoff can't make this
    guard pass vacuously as the fixture ages.
    """
    now = datetime.now(timezone.utc)
    lines = []
    for i in range(good_rows):
        ts = (now - timedelta(hours=good_age_hours) - timedelta(minutes=10 * i)).isoformat()
        lines.append(json.dumps({"timestamp": ts, "idle": i % 2 == 0}))
    for raw in bad:
        lines.append(json.dumps({"timestamp": raw, "idle": True}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prefix_idle_rate(kpi_engine, metrics: list) -> dict:
    """The pre-D#2331 body of compute_idle_rate, verbatim in behaviour.

    Used as the canary: run over the SAME fixture as the real function, it must
    produce an observably different answer, otherwise the fixture proves
    nothing about whether the fix is present (the D#1984 trap).
    """
    def _naive_parse(ts):
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    if not metrics:
        return {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0}
    now = kpi_engine._now_utc()
    cutoff = now - timedelta(hours=24)
    recent = [m for m in metrics if (_naive_parse(m.get("timestamp")) or now) >= cutoff]

    def _pct(rows):
        if not rows:
            return None
        return round(sum(1 for r in rows if r.get("idle") is True) / len(rows) * 100, 1)

    return {"last_24h_pct": _pct(recent), "all_time_pct": _pct(metrics), "total_iterations": len(metrics)}


def _load_and_compute(kpi_engine, fixture_path: Path):
    """Drive the real loader and the real compute_idle_rate over a real file.

    Not a stub returning what we expect: kpi_engine.METRICS is pointed at the
    fixture and load_loop_metrics() reads it off disk, exactly as
    compute_all() does.
    """
    orig = kpi_engine.METRICS
    kpi_engine.METRICS = fixture_path
    try:
        rows = kpi_engine.load_loop_metrics()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            result = kpi_engine.compute_idle_rate(rows)
        return rows, result, stderr_buf.getvalue()
    finally:
        kpi_engine.METRICS = orig


def check_kpi_idle_rate(tmp_dir: Path, kpi_engine) -> list[str]:
    """D#2331: kpi_engine.compute_idle_rate excludes rows it cannot date,
    says how many it excluded, and never dates one to now."""
    failures: list[str] = []

    # --- Fixture 1: 8 readable rows inside the window (4 idle) plus two
    # unreadable ones -- an epoch int and a malformed string.
    f1 = tmp_dir / "kpi-mixed.jsonl"
    _write_kpi_fixture(f1, good_rows=8, good_age_hours=0, bad=[1784925063, "not-a-timestamp"])

    try:
        rows, result, stderr_text = _load_and_compute(kpi_engine, f1)
    except Exception as exc:  # noqa: BLE001 -- report ANY exception
        failures.append(
            f"compute_idle_rate raised {type(exc).__name__}: {exc!r} on the mixed "
            f"fixture (expected: no raise)"
        )
        return failures

    print(f"  compute_idle_rate(8 good + 2 unreadable) = {result}")

    if len(rows) != 10:
        failures.append(f"fixture 1: expected 10 rows off disk, loader returned {len(rows)}")
    if result.get("malformed_lines") != 2:
        failures.append(
            f"fixture 1: malformed_lines expected 2 (the epoch int and the bad "
            f"string), got {result.get('malformed_lines')!r}"
        )
    if result.get("last_24h_pct") != 50.0:
        failures.append(
            f"fixture 1: last_24h_pct expected 50.0 (4 idle of the 8 readable "
            f"rows -- the 2 unreadable ones must not be in the window), got "
            f"{result.get('last_24h_pct')!r}"
        )
    if result.get("total_iterations") != 10:
        failures.append(
            f"fixture 1: total_iterations expected 10 (all-time counts need no "
            f"timestamp), got {result.get('total_iterations')!r}"
        )
    if "skipping malformed row" not in stderr_text:
        failures.append(
            f"fixture 1: expected a stderr diagnostic for each skipped row (skip "
            f"must be loud) -- stderr was {stderr_text!r}"
        )
    else:
        print("  stderr diagnostics present for the skipped rows")

    # Canary for finding 1: the pre-fix body raises on the epoch int, so this
    # fixture discriminates a fixed implementation from the original crash.
    raised = False
    try:
        _prefix_idle_rate(kpi_engine, rows)
    except AttributeError:
        raised = True
    if not raised:
        failures.append(
            "canary 1: the pre-fix compute_idle_rate body did NOT raise on this "
            "fixture -- it cannot discriminate a fixed implementation from the "
            "original crash"
        )
    else:
        print("  canary 1: pre-fix body reproduces the AttributeError, as expected")

    # --- Fixture 2: the window contains nothing readable. Eight readable rows,
    # all older than 24h, plus one unreadable string row. The ONLY row the
    # pre-fix code put in the window is the one it could not date.
    f2 = tmp_dir / "kpi-window-unreadable.jsonl"
    _write_kpi_fixture(f2, good_rows=8, good_age_hours=48, bad=["not-a-timestamp"])

    try:
        rows2, result2, _ = _load_and_compute(kpi_engine, f2)
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"compute_idle_rate raised {type(exc).__name__}: {exc!r} on the "
            f"empty-window fixture (expected: no raise)"
        )
        return failures

    print(f"  compute_idle_rate(nothing readable in window) = {result2}")

    if result2.get("last_24h_pct") is not None:
        failures.append(
            f"fixture 2: last_24h_pct expected None (nothing readable in the "
            f"window -- a confident number here is the whole defect), got "
            f"{result2.get('last_24h_pct')!r}"
        )
    if result2.get("malformed_lines") != 1:
        failures.append(
            f"fixture 2: malformed_lines expected 1, got "
            f"{result2.get('malformed_lines')!r}"
        )

    # Canary for finding 2: over the SAME fixture, the pre-fix body dates the
    # unreadable row to now and reports a confident 100% idle.
    prefix_result = _prefix_idle_rate(kpi_engine, rows2)
    if prefix_result.get("last_24h_pct") != 100.0:
        failures.append(
            f"canary 2: the pre-fix body was expected to inflate last_24h_pct to "
            f"100.0 on this fixture (dating the unreadable row to now), but "
            f"returned {prefix_result.get('last_24h_pct')!r} -- the fixture no "
            f"longer demonstrates the bug it guards against"
        )
    else:
        print(
            "  canary 2: pre-fix body reports last_24h_pct=100.0 on the same "
            "fixture (unreadable row dated to now), as expected"
        )

    return failures


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    import backend.kpi_engine as kpi_engine  # noqa: PLC0415
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

        print("kpi_engine.compute_idle_rate:")
        all_failures += check_kpi_idle_rate(Path(tmp), kpi_engine)

    if all_failures:
        print("\nFAIL loop-idle-ratio-timestamp-guard:")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("\nloop-idle-ratio-timestamp-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
