#!/usr/bin/env python3
"""stats-reader-honesty-guard.py — behavioral guard for the stats readers behind
the Runs and Stats pages (D#2316 PR-c, findings 2 and 6).

Background
----------
Two readers were each reporting a confident value they had never measured.

**`backend/stats/sdk_vs_cc.py`** computed its pass rate as
``AVG(CASE WHEN verdict IN ('done','pass') THEN 1.0 ELSE 0.0 END)`` over every
row the route filter let through. Measured on the operator host: every
``routed_via='cc'`` row is dispatcher bookkeeping that stopped on 2026-08-19 and
never recorded a verdict (5526 rows, 0 verdicts), and every verdict-bearing row
has ``routed_via IS NULL`` (1949 rows, 1949 verdicts). The populations are
disjoint, so the expression averaged thousands of NULLs as zeros and all 22
roles read ``0.0%`` — a zero-denominator rate rendered as a measurement. The
denominator is now ``COUNT(verdict)`` and an empty one yields ``None``, which
the tile renders as an em-dash. The response also reports how many rows the
route filter dropped, so a caller can tell "1949 runs are not attributed to a
route" from "there are no runs".

**`backend/stats_freshness_watchdog.py`** branched on
``hasattr(last_ts, "astimezone")`` before normalising ``metric_event.ts``. Every
``datetime`` has that attribute, so the naive branch — the correct one, whose
comment already said "assume UTC" — was unreachable, and
``naive.astimezone(utc)`` reinterpreted a UTC wall-clock value as local time.
Measured on the operator host (America/New_York): 16 of 17 metrics came back
with negative ``age_seconds``, down to -8141s. On a UTC host the offset is zero,
which is why it survived. Separately, ``bootstrap_ping`` — written once at
bootstrap, never again — had been asserting 1243h of staleness at the top of
every page for 51 days. It is now labelled unmonitored rather than muted, so a
genuinely stale monitored metric still flags.

This is a behavioral probe, not a lint over source text: it builds a fixture
``stats.duckdb`` in a tmpdir (via ``STATS_DB_PATH``, which both readers resolve
through and which bypasses the pytest state-dir guard — see
``backend/state_paths.py``) and calls the real ``sdk_vs_cc_by_role()`` and the
real ``check()`` against it, with ``metric_event`` rows written by the real
``stats_writer.record()``. Every expectation is derived from the fixture
construction below it — no literal population counts anywhere in this file, per
D#2316's constraint (the filing's own 77% reading was 64% four hours later).

Run from the repo root:

    python3 scripts/ci/stats-reader-honesty-guard.py

Exit 0: every check passes.
Exit 1: a check failed — prints one `FAIL <detail>` line per failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# A non-UTC zone, so a reader that confuses naive-UTC with local time produces
# a visibly wrong answer. On a UTC host the bug's offset is zero and the
# freshness checks below would pass against the broken implementation.
FIXTURE_TZ = "America/New_York"

FAILURES: list[str] = []


def _fail(detail: str) -> None:
    FAILURES.append(detail)
    print(f"FAIL {detail}")


# ---------------------------------------------------------------------------
# Fixture: agent_run rows for the sdk_vs_cc reader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRow:
    """One synthetic agent_run row."""

    agent_id: str
    role: str
    routed_via: str | None
    verdict: str | None


_PASS_VERDICTS = frozenset({"done", "pass"})


def _build_run_fixture() -> list[RunRow]:
    """Construct the agent_run rows the sdk_vs_cc checks are derived from.

    Three deliberate populations:
      * ``no-verdict-role`` — routed, but nothing ever recorded a verdict.
        This is the live shape of every ``routed_via='cc'`` row.
      * ``mixed-verdict-role`` — routed, with a constructed pass/fail mix.
      * unrouted rows — what the route filter drops, and what the response
        must now report a count for.
    """
    rows: list[RunRow] = []

    # Routed, zero verdicts. pass_rate must be None, not 0.0.
    for i in range(4):
        rows.append(RunRow(f"fixture-noverdict-{i}", "no-verdict-role", "cc", None))

    # Routed, constructed verdict mix. Includes a non-pass verdict that is a
    # real measurement ('needs-fix') — it belongs in the denominator — and a
    # NULL that does not.
    for i, verdict in enumerate(["done", "pass", "needs-fix", "fail", None, None]):
        rows.append(RunRow(f"fixture-mixed-{i}", "mixed-verdict-role", "cc", verdict))

    # Unrouted. Dropped by the route filter; the count must be reported.
    for i, verdict in enumerate(["done", "pass", "needs-fix"]):
        rows.append(RunRow(f"fixture-unrouted-{i}", "executor", None, verdict))

    return rows


def _write_run_fixture(db_path: Path, rows: list[RunRow]) -> None:
    import duckdb  # noqa: PLC0415

    from backend.agent_run_tracker import _ensure_schema  # noqa: PLC0415

    base = datetime(2026, 9, 4, 3, 0, 0, tzinfo=timezone.utc)
    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        for i, r in enumerate(rows):
            conn.execute(
                "INSERT INTO agent_run"
                " (agent_id, role, start_ts, end_ts, verdict, routed_via,"
                "  input_tok, output_tok)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    r.agent_id,
                    r.role,
                    base + timedelta(seconds=i),
                    base + timedelta(seconds=i + 10),
                    r.verdict,
                    r.routed_via,
                    1000,
                    100,
                ],
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture: metric_event rows for the freshness reader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricRow:
    """One synthetic metric_event row.

    ``age_s`` is how far in the past this metric was last written, measured
    from the moment the fixture is built.
    """

    metric: str
    age_s: int
    monitored: bool


def _build_metric_fixture() -> list[MetricRow]:
    """Construct the metric_event rows the freshness checks are derived from.

    Metric names are chosen from ``stats_writer.registered_metrics()`` (and one
    name deliberately absent from it), so "monitored" here is the real
    registry's answer rather than something this file asserts on its own.
    """
    from backend import stats_writer  # noqa: PLC0415
    from backend.stats_freshness_watchdog import WARN_AGE_SECONDS  # noqa: PLC0415

    registered = sorted(stats_writer.registered_metrics())
    if len(registered) < 2:
        _fail(
            "metric-fixture: stats_writer.registered_metrics() returned fewer than "
            "two names — cannot construct a fresh/stale monitored pair"
        )
        return []

    one_shot = "fixture_one_shot_marker"
    if one_shot in registered:
        _fail(f"metric-fixture: {one_shot!r} unexpectedly has a registered writer")

    return [
        # Monitored and fresh — must not flag.
        MetricRow(registered[0], 60, monitored=True),
        # Monitored and genuinely stale — must still flag. This is what stops
        # the one-shot exclusion from being implemented as a blanket mute.
        MetricRow(registered[1], WARN_AGE_SECONDS * 3, monitored=True),
        # One-shot marker, absurdly old — bootstrap_ping's shape. Must not
        # flag, but must keep its real age in the payload.
        MetricRow(one_shot, 51 * 86400, monitored=False),
    ]


def _write_metric_fixture(rows: list[MetricRow]) -> datetime:
    """Write the metric rows through the REAL stats writer. Returns the instant
    the ages are measured from."""
    from backend import stats_writer  # noqa: PLC0415

    written_at = datetime.now(timezone.utc)
    for r in rows:
        stats_writer.record(
            metric=r.metric,
            value=1.0,
            unit="count",
            source="stats-reader-honesty-guard",
            ts=written_at - timedelta(seconds=r.age_s),
        )
    return written_at


# ---------------------------------------------------------------------------
# Checks — sdk_vs_cc
# ---------------------------------------------------------------------------

def _rows_by_role(result: dict) -> dict[str, dict]:
    return {r["role"]: r for r in result["rows"]}


def check_zero_denominator_is_none(result: dict, fixture: list[RunRow]) -> None:
    """Item 16 (first half): a role whose rows all have ``verdict IS NULL``
    gets ``pass_rate: None`` — not ``0.0``."""
    by_role = _rows_by_role(result)
    roles_with_no_verdict = {
        r.role
        for r in fixture
        if r.routed_via is not None
    } - {
        r.role
        for r in fixture
        if r.routed_via is not None and r.verdict is not None
    }

    if not roles_with_no_verdict:
        _fail("zero-denominator: fixture constructed no verdict-free routed role")

    for role in sorted(roles_with_no_verdict):
        row = by_role.get(role)
        if row is None:
            _fail(f"zero-denominator: reader returned no row for role {role!r}")
            continue
        if row["pass_rate"] is not None:
            _fail(
                f"zero-denominator: role {role!r} has no verdict-bearing runs but "
                f"pass_rate={row['pass_rate']!r} — expected None. A rate with a "
                "zero denominator is not a measurement; 0.0 renders as '0.0%' and "
                "reads as one."
            )
        if row.get("verdict_count") != 0:
            _fail(
                f"zero-denominator: role {role!r} expected verdict_count=0, "
                f"got {row.get('verdict_count')!r}"
            )


def check_mixed_verdicts_match_fixture(result: dict, fixture: list[RunRow]) -> None:
    """Item 16 (second half): a role with a constructed verdict mix gets the
    fraction the fixture constructed, over verdict-bearing rows only."""
    by_role = _rows_by_role(result)

    routed = [r for r in fixture if r.routed_via is not None]
    roles_with_verdicts = {r.role for r in routed if r.verdict is not None}

    if not roles_with_verdicts:
        _fail("mixed-verdicts: fixture constructed no routed role carrying verdicts")

    for role in sorted(roles_with_verdicts):
        group = [r for r in routed if r.role == role]
        verdict_bearing = [r for r in group if r.verdict is not None]
        passes = [r for r in verdict_bearing if r.verdict in _PASS_VERDICTS]
        expected = len(passes) / len(verdict_bearing)

        row = by_role.get(role)
        if row is None:
            _fail(f"mixed-verdicts: reader returned no row for role {role!r}")
            continue
        actual = row["pass_rate"]
        if actual is None or abs(actual - expected) > 1e-4:
            _fail(
                f"mixed-verdicts: role {role!r} fixture has {len(passes)} passes over "
                f"{len(verdict_bearing)} verdict-bearing runs (of {len(group)} total) "
                f"=> pass_rate should be ~{expected:.4f}, got {actual!r}"
            )
        if row.get("verdict_count") != len(verdict_bearing):
            _fail(
                f"mixed-verdicts: role {role!r} expected "
                f"verdict_count={len(verdict_bearing)}, got {row.get('verdict_count')!r}"
            )


def check_excluded_rows_are_reported(result: dict, fixture: list[RunRow]) -> None:
    """Item 17: the response states how many rows the route filter dropped, so
    "N runs are unattributed" is distinguishable from "there are no runs"."""
    expected = len([r for r in fixture if r.routed_via is None])
    if expected == 0:
        _fail("excluded-rows: fixture constructed no unrouted rows to exclude")

    actual = result.get("excluded_unrouted_runs")
    if actual != expected:
        _fail(
            f"excluded-rows: fixture constructed {expected} row(s) with "
            f"routed_via IS NULL, response reports excluded_unrouted_runs={actual!r}. "
            "Silently dropping them makes an unattributed population look like an "
            "empty one."
        )

    # And those rows must not have leaked into the per-role table.
    unrouted_only_roles = {r.role for r in fixture if r.routed_via is None} - {
        r.role for r in fixture if r.routed_via is not None
    }
    leaked = unrouted_only_roles & set(_rows_by_role(result))
    if leaked:
        _fail(f"excluded-rows: unrouted-only role(s) {sorted(leaked)} appeared in rows")


# ---------------------------------------------------------------------------
# Checks — freshness
# ---------------------------------------------------------------------------

def check_freshness_ages_are_honest(
    rows: list[dict], fixture: list[MetricRow], written_at: datetime
) -> None:
    """Item 19: ages match the fixture's own construction and no row written in
    the past reports a negative age. Fails on main under a non-UTC TZ."""
    by_metric = {r["metric_name"]: r for r in rows}

    negative = [r for r in rows if r["age_seconds"] < 0]
    if negative:
        _fail(
            "freshness-age: "
            + ", ".join(f"{r['metric_name']} age={r['age_seconds']}s" for r in negative)
            + f" — negative age under TZ={FIXTURE_TZ}. Every fixture row was written "
            "in the past; a watchdog whose ages point backwards cannot detect "
            "staleness at all."
        )

    # Tolerance covers the wall-clock gap between writing the fixture and
    # reading it back, plus the reader's second-truncation.
    tolerance = max(30, int((datetime.now(timezone.utc) - written_at).total_seconds()) + 5)
    for f in fixture:
        row = by_metric.get(f.metric)
        if row is None:
            _fail(f"freshness-age: reader returned no row for metric {f.metric!r}")
            continue
        if abs(row["age_seconds"] - f.age_s) > tolerance:
            _fail(
                f"freshness-age: metric {f.metric!r} was written {f.age_s}s before the "
                f"read, reader reports age_seconds={row['age_seconds']} "
                f"(off by {row['age_seconds'] - f.age_s}s, tolerance {tolerance}s)"
            )


def check_one_shot_labelled_not_muted(rows: list[dict], fixture: list[MetricRow]) -> None:
    """Item 20: a one-shot metric with no registered writer is labelled rather
    than asserted stale — while a genuinely stale monitored metric still flags,
    so the exclusion cannot be a blanket mute."""
    from backend import stats_freshness_watchdog as wd  # noqa: PLC0415

    warn_age = wd.WARN_AGE_SECONDS
    monitored_rows = getattr(wd, "monitored_rows", None)
    if monitored_rows is None:
        _fail(
            "one-shot: stats_freshness_watchdog has no monitored_rows() — nothing "
            "narrows the stale set to metrics that still have a live writer, so a "
            "one-shot marker drives the banner forever"
        )
        monitored_rows = list

    by_metric = {r["metric_name"]: r for r in rows}
    stale_names = {
        r["metric_name"]
        for r in monitored_rows(rows)
        if r["age_seconds"] >= warn_age
    }

    expected_stale = {
        f.metric for f in fixture if f.monitored and f.age_s >= warn_age
    }
    expected_excluded = {f.metric for f in fixture if not f.monitored}

    if not expected_stale:
        _fail("one-shot: fixture constructed no stale monitored metric to flag")
    if not expected_excluded:
        _fail("one-shot: fixture constructed no unmonitored metric to exclude")

    missed = expected_stale - stale_names
    if missed:
        _fail(
            f"one-shot: monitored metric(s) {sorted(missed)} are past the warn "
            "threshold but were not flagged — the one-shot exclusion must not be a "
            "blanket mute"
        )

    wrongly_flagged = expected_excluded & stale_names
    if wrongly_flagged:
        _fail(
            f"one-shot: metric(s) {sorted(wrongly_flagged)} have no registered writer "
            "but still drive a staleness warning. Nobody can act on the staleness of "
            "a metric that was never going to be written again."
        )

    for f in fixture:
        row = by_metric.get(f.metric)
        if row is None:
            _fail(f"one-shot: reader returned no row for metric {f.metric!r}")
            continue
        if "monitored" not in row:
            _fail(
                f"one-shot: row for {f.metric!r} carries no 'monitored' marker — an "
                "excluded metric must be labelled, not silently dropped"
            )
        elif row["monitored"] is not f.monitored:
            _fail(
                f"one-shot: metric {f.metric!r} expected monitored={f.monitored}, "
                f"got {row['monitored']!r}"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    os.environ["TZ"] = FIXTURE_TZ
    if hasattr(time, "tzset"):
        time.tzset()
    else:  # pragma: no cover — CI runs on Linux
        print(
            "stats-reader-honesty-guard: time.tzset() unavailable on this platform — "
            "the freshness checks cannot exercise the non-UTC path, failing closed"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="stats-reader-honesty-guard-") as td:
        db_path = Path(td) / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db_path)

        try:
            import duckdb  # noqa: F401, PLC0415
        except ImportError:
            print("stats-reader-honesty-guard: duckdb not installed — cannot run, failing closed")
            return 1

        from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role  # noqa: PLC0415
        from backend.stats_freshness_watchdog import check  # noqa: PLC0415

        run_fixture = _build_run_fixture()
        _write_run_fixture(db_path, run_fixture)

        metric_fixture = _build_metric_fixture()
        written_at = _write_metric_fixture(metric_fixture)

        sdk_result = sdk_vs_cc_by_role(db_path=db_path)
        if sdk_result["error"]:
            _fail(f"sdk_vs_cc_by_role returned error: {sdk_result['error']}")
        else:
            check_zero_denominator_is_none(sdk_result, run_fixture)
            check_mixed_verdicts_match_fixture(sdk_result, run_fixture)
            check_excluded_rows_are_reported(sdk_result, run_fixture)

        freshness_rows = check()
        if not freshness_rows:
            _fail("freshness check() returned no rows against a fixture that has some")
        elif metric_fixture:
            check_freshness_ages_are_honest(freshness_rows, metric_fixture, written_at)
            check_one_shot_labelled_not_muted(freshness_rows, metric_fixture)

    if FAILURES:
        print(f"stats-reader-honesty-guard: {len(FAILURES)} check(s) failed")
        return 1

    print("stats-reader-honesty-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
