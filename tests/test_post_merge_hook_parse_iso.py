"""
tests/test_post_merge_hook_parse_iso.py — unit tests for the `_parse_iso()`
helper embedded in the stats_metrics Python block of scripts/post-merge-hook.sh
(D#1597).

`_parse_iso()` must always return a tz-AWARE (UTC) datetime, even when the
input timestamp string lacks a `Z`/offset suffix. Before the D#1597 fix,
`datetime.fromisoformat()` silently succeeded on a naive-format string and
returned a tz-naive datetime, which later raised `TypeError: can't subtract
offset-naive and offset-aware datetimes` when subtracted against gh's
always-Z-suffixed `createdAt` timestamps (`now_dt`, `created_dt` and friends).

This test extracts the live `_parse_iso` source straight out of the shell
script (rather than re-implementing it) so the test can never silently drift
from the real implementation.
"""

import datetime
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "scripts" / "post-merge-hook.sh"


def _load_parse_iso():
    """Extract the `_parse_iso` function body from post-merge-hook.sh and
    exec it in an isolated namespace, returning the callable."""
    text = HOOK_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"def _parse_iso\(s\):.*?\n(?=# Phase 1 metrics)",
        text,
        re.DOTALL,
    )
    assert m, "could not locate _parse_iso() in scripts/post-merge-hook.sh"
    namespace = {"datetime": datetime}
    exec(compile(m.group(0), str(HOOK_PATH), "exec"), namespace)
    return namespace["_parse_iso"]


@pytest.fixture(scope="module")
def parse_iso():
    return _load_parse_iso()


class TestParseIsoTzAwareness:

    def test_z_suffixed_timestamp_is_aware(self, parse_iso):
        dt = parse_iso("2026-07-03T07:00:00Z")
        assert dt.tzinfo is not None

    def test_naive_timestamp_is_made_aware_utc(self, parse_iso):
        """D#1597 regression: a timestamp with no Z/offset suffix must come
        back tz-aware (assumed UTC), not silently tz-naive."""
        dt = parse_iso("2026-07-03T07:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == datetime.timedelta(0)

    def test_naive_timestamp_matches_equivalent_z_timestamp(self, parse_iso):
        naive_result = parse_iso("2026-07-03T07:00:00")
        aware_result = parse_iso("2026-07-03T07:00:00Z")
        assert naive_result == aware_result

    def test_offset_suffixed_timestamp_normalized_to_utc(self, parse_iso):
        dt = parse_iso("2026-07-03T09:00:00+02:00")
        assert dt.tzinfo is not None
        assert dt == datetime.datetime(2026, 7, 3, 7, 0, 0, tzinfo=datetime.timezone.utc)

    def test_empty_string_returns_none(self, parse_iso):
        assert parse_iso("") is None
        assert parse_iso("   ") is None

    def test_subtraction_against_aware_datetime_does_not_raise(self, parse_iso):
        """D#1597: the original crash site — subtracting a naive-parsed
        datetime from a tz-aware `now_dt` used to raise TypeError."""
        now_dt = datetime.datetime(2026, 7, 3, 8, 0, 0, tzinfo=datetime.timezone.utc)
        created_dt = parse_iso("2026-07-03T07:00:00")  # no Z suffix
        elapsed = (now_dt - created_dt).total_seconds()
        assert elapsed == 3600.0
