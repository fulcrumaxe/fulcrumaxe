"""Tests for scripts/lib/pr_head_baseline.py — D#2421, the PR head-SHA
baseline store.

Coverage:
  verdict map    — first observation is "match" (never "absent"); an
                   unchanged head stays "match"; a changed head is "drifted";
                   an unreadable store is "unknown"
  ceiling        — three distinct new heads trips it; a rebaseline does not
                   clear it; recovery below the ceiling does
  once-per-head  — invalidation_count bumps once per distinct drifted head,
                   never once per poll pass
  freshness      — (PR 3) a first observation auto-baselines only while the
                   label is within FIRST_OBSERVATION_GRACE_SECONDS of our own
                   clock; stale, unparseable, skewed and misconfigured all
                   refuse and write nothing
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import intake_baseline as ib  # noqa: E402
import pr_head_baseline as phb  # noqa: E402

KEY = "example-org/example-code#7"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40

#: A fixed instant to compare labels against. Injected everywhere, so nothing
#: in this file depends on the wall clock.
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _label_age(seconds: float) -> str:
    """A label `created_at` *seconds* older than the injected NOW."""
    return _iso(NOW - timedelta(seconds=seconds))


FRESH = _label_age(60)


def _cr(key, head, store, labeled_at=FRESH, now=NOW):
    """`check_and_record` with a fresh label — for the tests below that are
    about SHA drift and the ceiling rather than about freshness.

    The default lives here and only here. `check_and_record` itself takes
    `labeled_at` as a required positional, so no production call site can
    inherit a permissive value by omitting it; the freshness tests further
    down always pass it explicitly.
    """
    return phb.check_and_record(key, head, labeled_at, path=store, now=now)


# ---------------------------------------------------------------------------
# Verdict map
# ---------------------------------------------------------------------------


def test_labeled_at_is_required_not_defaulted():
    """The parameter that gates the auto-baseline must be impossible to omit:
    a future caller that forgets it gets a TypeError, not the old permissive
    behaviour (D#2421 PR 3)."""
    with pytest.raises(TypeError):
        phb.check_and_record(KEY, HEAD_A)  # type: ignore[call-arg]


def test_first_observation_is_match_never_absent(tmp_path):
    store = tmp_path / "store.json"
    assert _cr(KEY, HEAD_A, store) == "match"
    entry = ib.get_entry(KEY, path=store)
    assert entry is not None
    assert entry["content_sha256"] == HEAD_A


def test_unchanged_head_stays_match(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    assert _cr(KEY, HEAD_A, store) == "match"
    assert _cr(KEY, HEAD_A, store) == "match"


def test_head_change_is_drifted(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    assert _cr(KEY, HEAD_B, store) == "drifted"


def test_sha_equality_not_tree_equality(tmp_path):
    """Any SHA change invalidates, even when nothing else about the PR is
    asserted to differ — the store never inspects tree content, only the
    SHA string it is handed."""
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    assert _cr(KEY, HEAD_B, store) == "drifted"


def test_unreadable_store_is_unknown(tmp_path, monkeypatch):
    store = tmp_path / "store.json"

    def _unreadable(path=None):
        return False, {}

    monkeypatch.setattr(ib, "read_baselines", _unreadable)
    assert _cr(KEY, HEAD_A, store) == "unknown"


def test_record_failure_on_first_observation_is_unknown(tmp_path, monkeypatch):
    store = tmp_path / "store.json"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ib, "record_baseline", _boom)
    assert _cr(KEY, HEAD_A, store) == "unknown"


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------


def test_ceiling_trips_on_third_distinct_new_head(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)                      # baseline
    assert _cr(KEY, HEAD_B, store) == "drifted"  # 1st new head
    assert _cr(KEY, HEAD_C, store) == "drifted"  # 2nd new head
    assert _cr(KEY, HEAD_D, store) == "ceiling"  # 3rd new head


def test_ceiling_survives_rebaseline(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    _cr(KEY, HEAD_B, store)
    _cr(KEY, HEAD_C, store)
    _cr(KEY, HEAD_D, store)  # ceiling

    phb.rebaseline(KEY, HEAD_D, path=store)

    # The stored head now matches (rebaselined), but the counter (>= CEILING)
    # was carried forward, so this is still "ceiling", not "match".
    assert _cr(KEY, HEAD_D, store) == "ceiling"


def test_recovery_below_ceiling_returns_to_match(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    _cr(KEY, HEAD_B, store)  # drifted, count=1

    phb.rebaseline(KEY, HEAD_B, path=store)

    assert _cr(KEY, HEAD_B, store) == "match"
    entry = ib.get_entry(KEY, path=store)
    assert entry["invalidation_count"] == 1  # not reset, just not yet at ceiling


# ---------------------------------------------------------------------------
# Once-per-head bump
# ---------------------------------------------------------------------------


def test_invalidation_count_bumps_once_per_distinct_head_not_per_poll(tmp_path):
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)
    for _ in range(10):
        _cr(KEY, HEAD_B, store)

    entry = ib.get_entry(KEY, path=store)
    assert entry is not None
    assert entry["invalidation_count"] == 1


def test_pr_key_matches_intake_baseline_key_shape():
    """Same "{owner}/{name}#{number}" shape a Discussion key has — collision
    with the Discussion store is avoided by file, not by key shape (AC-8,
    see backend/state_paths.py's PR_HEAD_BASELINES docstring)."""
    key = phb.pr_key("example-org/example-code", 7)
    assert key == "example-org/example-code#7"
    ib._validate_key(key)  # must not raise


# ---------------------------------------------------------------------------
# First-observation freshness (D#2421 PR 3)
#
# The auto-baseline arm is the one step that turns "nobody has told us
# anything about this head" into "approved". These drive that arm directly,
# and every one of them asserts the store as well as the verdict: refusing
# must actually not write, not merely return a different string.
# ---------------------------------------------------------------------------


def _row(store):
    return ib.get_entry(KEY, path=store)


def test_fresh_first_observation_auto_baselines(tmp_path):
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(60), path=store, now=NOW) == "match"
    assert _row(store)["content_sha256"] == HEAD_A


def test_stale_first_observation_refuses_and_writes_nothing(tmp_path):
    """AC-2. Pre-fix this returned "match" and wrote the row — which is what
    made a state-dir loss silently re-approve every open external PR at
    whatever head it happened to be showing."""
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(3600), path=store, now=NOW) == "unknown"
    assert _row(store) is None


def test_grace_boundary_is_where_it_is_claimed(tmp_path, monkeypatch):
    """AC-3: the transition itself is the observation, so both sides of the
    boundary are asserted in one test against a known G."""
    grace = 900
    monkeypatch.setattr(phb, "FIRST_OBSERVATION_GRACE_SECONDS", grace)

    at_boundary = tmp_path / "at.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(grace), path=at_boundary, now=NOW) == "match"
    assert ib.get_entry(KEY, path=at_boundary) is not None

    past_boundary = tmp_path / "past.json"
    assert (
        phb.check_and_record(KEY, HEAD_A, _label_age(grace + 1), path=past_boundary, now=NOW)
        == "unknown"
    )
    assert ib.get_entry(KEY, path=past_boundary) is None


@pytest.mark.parametrize(
    "labeled_at",
    [None, "", "not-a-date", "2026-13-45T99:99:99Z", 1757160000, "2026-09-06T12:00:00"],
    ids=["none", "empty", "junk", "impossible-fields", "epoch-int", "naive-no-zone"],
)
def test_unparseable_labeled_at_refuses(tmp_path, labeled_at):
    """AC-4, the store layer. An instant we cannot read is not an instant we
    may treat as now. The naive case is deliberate: guessing a zone for it
    would be a one-hour-wide guess inside a security decision."""
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, labeled_at, path=store, now=NOW) == "unknown"
    assert _row(store) is None


def test_far_future_label_refuses_but_small_forward_skew_admits(tmp_path):
    """AC-5. One `abs()` covers both directions: a label stamped well ahead of
    our clock is an anomaly and blocks, while the few seconds of forward skew
    between GitHub's clock and ours is normal and must not."""
    far = tmp_path / "far.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(-7200), path=far, now=NOW) == "unknown"
    assert ib.get_entry(KEY, path=far) is None

    near = tmp_path / "near.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(-30), path=near, now=NOW) == "match"
    assert ib.get_entry(KEY, path=near) is not None


@pytest.mark.parametrize(
    "bad",
    [None, "900", -1, float("nan"), float("inf"), True],
    ids=["none", "string", "negative", "nan", "inf", "bool"],
)
def test_malformed_grace_constant_refuses_everything(tmp_path, monkeypatch, bad):
    """AC-6. A broken configuration resolves to 0 — blocking every first
    observation — never back to the 900s default. `True` is in here because
    `isinstance(True, int)` is the classic way a bool slips past a numeric
    guard and becomes a one-second grace."""
    monkeypatch.setattr(phb, "FIRST_OBSERVATION_GRACE_SECONDS", bad)
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(60), path=store, now=NOW) == "unknown"
    assert _row(store) is None


def test_deleted_grace_constant_refuses_everything(tmp_path, monkeypatch):
    """AC-6's sixth case: the attribute gone entirely, not merely wrong.
    `_first_observation_grace` reads through sys.modules so this is
    reachable at all."""
    monkeypatch.delattr(phb, "FIRST_OBSERVATION_GRACE_SECONDS")
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, _label_age(60), path=store, now=NOW) == "unknown"
    assert _row(store) is None


def test_freshness_gates_only_the_first_observation(tmp_path):
    """An established row is compared by SHA alone. A stale label must not
    retroactively un-approve a head that was already baselined — otherwise
    every long-lived approved PR would start failing closed at grace + 1."""
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, _label_age(60), path=store, now=NOW)

    assert phb.check_and_record(KEY, HEAD_A, _label_age(999999), path=store, now=NOW) == "match"
    assert phb.check_and_record(KEY, HEAD_B, _label_age(999999), path=store, now=NOW) == "drifted"


def test_default_grace_is_fifteen_minutes():
    """Recorded as a behavioural fact rather than as prose: one cron poll
    interval (10 min) plus 50% slack. Raising it widens the race."""
    assert phb._first_observation_grace() == 900.0


# ---------------------------------------------------------------------------
# invalidation_count — the local read behind rebaseline-pr's still-blocked
# report (AC-9)
# ---------------------------------------------------------------------------


def test_invalidation_count_reads_the_stored_counter(tmp_path):
    store = tmp_path / "store.json"
    assert phb.invalidation_count(KEY, path=store) is None  # no row yet

    _cr(KEY, HEAD_A, store)
    assert phb.invalidation_count(KEY, path=store) == 0

    _cr(KEY, HEAD_B, store)
    assert phb.invalidation_count(KEY, path=store) == 1


def test_invalidation_count_is_none_when_the_store_raises(tmp_path, monkeypatch):
    """Unreadable is not zero. Reporting zero here would tell an operator a
    ceiling-blocked PR is now workable."""
    store = tmp_path / "store.json"
    _cr(KEY, HEAD_A, store)

    def _boom(*_a, **_k):
        raise OSError("store gone")

    monkeypatch.setattr(ib, "get_entry", _boom)
    assert phb.invalidation_count(KEY, path=store) is None
