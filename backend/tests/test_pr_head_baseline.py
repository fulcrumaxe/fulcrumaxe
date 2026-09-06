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
"""

from __future__ import annotations

import sys
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Verdict map
# ---------------------------------------------------------------------------


def test_first_observation_is_match_never_absent(tmp_path):
    store = tmp_path / "store.json"
    assert phb.check_and_record(KEY, HEAD_A, path=store) == "match"
    entry = ib.get_entry(KEY, path=store)
    assert entry is not None
    assert entry["content_sha256"] == HEAD_A


def test_unchanged_head_stays_match(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    assert phb.check_and_record(KEY, HEAD_A, path=store) == "match"
    assert phb.check_and_record(KEY, HEAD_A, path=store) == "match"


def test_head_change_is_drifted(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    assert phb.check_and_record(KEY, HEAD_B, path=store) == "drifted"


def test_sha_equality_not_tree_equality(tmp_path):
    """Any SHA change invalidates, even when nothing else about the PR is
    asserted to differ — the store never inspects tree content, only the
    SHA string it is handed."""
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    assert phb.check_and_record(KEY, HEAD_B, path=store) == "drifted"


def test_unreadable_store_is_unknown(tmp_path, monkeypatch):
    store = tmp_path / "store.json"

    def _unreadable(path=None):
        return False, {}

    monkeypatch.setattr(ib, "read_baselines", _unreadable)
    assert phb.check_and_record(KEY, HEAD_A, path=store) == "unknown"


def test_record_failure_on_first_observation_is_unknown(tmp_path, monkeypatch):
    store = tmp_path / "store.json"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ib, "record_baseline", _boom)
    assert phb.check_and_record(KEY, HEAD_A, path=store) == "unknown"


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------


def test_ceiling_trips_on_third_distinct_new_head(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)                      # baseline
    assert phb.check_and_record(KEY, HEAD_B, path=store) == "drifted"  # 1st new head
    assert phb.check_and_record(KEY, HEAD_C, path=store) == "drifted"  # 2nd new head
    assert phb.check_and_record(KEY, HEAD_D, path=store) == "ceiling"  # 3rd new head


def test_ceiling_survives_rebaseline(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    phb.check_and_record(KEY, HEAD_B, path=store)
    phb.check_and_record(KEY, HEAD_C, path=store)
    phb.check_and_record(KEY, HEAD_D, path=store)  # ceiling

    phb.rebaseline(KEY, HEAD_D, path=store)

    # The stored head now matches (rebaselined), but the counter (>= CEILING)
    # was carried forward, so this is still "ceiling", not "match".
    assert phb.check_and_record(KEY, HEAD_D, path=store) == "ceiling"


def test_recovery_below_ceiling_returns_to_match(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    phb.check_and_record(KEY, HEAD_B, path=store)  # drifted, count=1

    phb.rebaseline(KEY, HEAD_B, path=store)

    assert phb.check_and_record(KEY, HEAD_B, path=store) == "match"
    entry = ib.get_entry(KEY, path=store)
    assert entry["invalidation_count"] == 1  # not reset, just not yet at ceiling


# ---------------------------------------------------------------------------
# Once-per-head bump
# ---------------------------------------------------------------------------


def test_invalidation_count_bumps_once_per_distinct_head_not_per_poll(tmp_path):
    store = tmp_path / "store.json"
    phb.check_and_record(KEY, HEAD_A, path=store)
    for _ in range(10):
        phb.check_and_record(KEY, HEAD_B, path=store)

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
