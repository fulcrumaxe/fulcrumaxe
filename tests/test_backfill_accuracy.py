"""
tests/test_backfill_accuracy.py — unit tests for scripts/backfill-accuracy.py.

Fixtures simulate gh API responses via monkeypatching.  No real GitHub calls.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── import the module under test ─────────────────────────────────────────────

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backfill-accuracy.py"

# Load the script as a module without executing __main__
_spec = importlib.util.spec_from_file_location("backfill_accuracy", SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

build_completion_block = _mod.build_completion_block
process_discussion = _mod.process_discussion
COMPLETION_MARKER = _mod.COMPLETION_MARKER


# ── helpers ───────────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _disc(
    *,
    number: int = 42,
    disc_id: str = "D_abc123",
    body: str = "Discussion body",
    created_offset_hours: float = 10.0,
    closed: bool = True,
) -> dict:
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=created_offset_hours)
    closed_at = _iso(now - timedelta(hours=1)) if closed else None
    return {
        "id": disc_id,
        "number": number,
        "title": f"Test Discussion #{number}",
        "body": body,
        "createdAt": _iso(created),
        "closedAt": closed_at,
    }


def _pr(
    *,
    number: int = 99,
    merged_offset_hours: float = 2.0,
    merged: bool = True,
) -> dict:
    now = datetime.now(timezone.utc)
    merged_at = _iso(now - timedelta(hours=merged_offset_hours)) if merged else None
    return {
        "number": number,
        "title": f"PR #{number}",
        "body": f"Closes #42",
        "merged_at": merged_at,
        "state": "closed",
    }


# ── build_completion_block ────────────────────────────────────────────────────


def test_completion_block_contains_marker():
    block = build_completion_block(
        actual_hours=3.5,
        merged_at="2026-05-10T12:00:00Z",
        merged_pr=88,
        inferred_estimated_hours=3.5,
    )
    assert COMPLETION_MARKER in block


def test_completion_block_actual_hours():
    block = build_completion_block(4.25, "2026-05-10T12:00:00Z", 88, 4.0)
    assert "actual_hours: 4.25" in block


def test_completion_block_has_inferred_flag():
    block = build_completion_block(4.0, "2026-05-10T12:00:00Z", None, 4.0)
    assert "_inferred: true" in block


def test_completion_block_no_merged_pr():
    """When merged_pr is None the block should still be valid."""
    block = build_completion_block(1.5, "2026-05-10T12:00:00Z", None, 1.5)
    assert "merged_pr" not in block
    assert COMPLETION_MARKER in block


# ── process_discussion: idempotency ──────────────────────────────────────────


def test_idempotent_skips_when_completion_already_present():
    """Discussion with existing COMPLETION block must be skipped without writing."""
    disc = _disc(body=f"Some body\n\n{COMPLETION_MARKER}\nactual_hours: 2.0\n")

    # If process_discussion tried to call find_linked_pr it would fail without mocking,
    # so a clean skip (no side-effect) means find_linked_pr is never called.
    with patch.object(_mod, "find_linked_pr") as mock_find:
        result = process_discussion(disc, dry_run=False, verbose=False)

    assert result == "skip"
    mock_find.assert_not_called()


def test_idempotent_on_dry_run_too():
    """Even in dry-run mode, a COMPLETION block means skip."""
    disc = _disc(body=f"Body\n\n{COMPLETION_MARKER}\nactual_hours: 1.0\n")

    with patch.object(_mod, "find_linked_pr") as mock_find:
        result = process_discussion(disc, dry_run=True, verbose=False)

    assert result == "skip"
    mock_find.assert_not_called()


# ── process_discussion: dry-run ──────────────────────────────────────────────


def test_dry_run_does_not_call_update():
    """Dry-run mode: computes but never updates the Discussion body."""
    disc = _disc(created_offset_hours=8.0)
    pr = _pr(merged_offset_hours=2.0)  # 6h after creation relative to disc

    with patch.object(_mod, "find_linked_pr", return_value=(0, pr)), \
         patch.object(_mod, "update_discussion_body") as mock_update:
        result = process_discussion(disc, dry_run=True, verbose=False)

    assert result == "dry_run"
    mock_update.assert_not_called()


# ── process_discussion: normal update ────────────────────────────────────────


def test_update_called_with_completion_block():
    """Happy path: update_discussion_body is called and status is 'updated'."""
    disc = _disc(created_offset_hours=10.0)
    pr = _pr(merged_offset_hours=2.0)

    captured: dict = {}

    def fake_update(disc_id: str, new_body: str):
        captured["disc_id"] = disc_id
        captured["body"] = new_body
        return 0, "{}"

    with patch.object(_mod, "find_linked_pr", return_value=(0, pr)), \
         patch.object(_mod, "update_discussion_body", side_effect=fake_update):
        result = process_discussion(disc, dry_run=False, verbose=False)

    assert result == "updated"
    assert COMPLETION_MARKER in captured["body"]
    assert "actual_hours" in captured["body"]
    assert "_inferred: true" in captured["body"]


# ── process_discussion: no linked PR ─────────────────────────────────────────


def test_no_pr_found_returns_no_pr():
    disc = _disc()
    with patch.object(_mod, "find_linked_pr", return_value=(0, None)):
        result = process_discussion(disc, dry_run=False, verbose=False)
    assert result == "no_pr"


# ── process_discussion: rate limit ───────────────────────────────────────────


def test_rate_limit_on_find_pr():
    disc = _disc()
    with patch.object(_mod, "find_linked_pr", return_value=(2, None)):
        result = process_discussion(disc, dry_run=False, verbose=False)
    assert result == "rate_limited"


def test_rate_limit_on_update():
    disc = _disc(created_offset_hours=10.0)
    pr = _pr(merged_offset_hours=2.0)

    with patch.object(_mod, "find_linked_pr", return_value=(0, pr)), \
         patch.object(_mod, "update_discussion_body", return_value=(2, "rate limit")):
        result = process_discussion(disc, dry_run=False, verbose=False)

    assert result == "rate_limited"


# ── process_discussion: bad timestamps ───────────────────────────────────────


def test_bad_merged_at_returns_bad_timestamps():
    disc = _disc(created_offset_hours=5.0)
    bad_pr = {**_pr(), "merged_at": "not-a-date"}

    with patch.object(_mod, "find_linked_pr", return_value=(0, bad_pr)):
        result = process_discussion(disc, dry_run=False, verbose=False)

    assert result == "bad_timestamps"


def test_merged_before_created_returns_bad_timestamps():
    """If merged_at < created_at (data anomaly), skip gracefully."""
    now = datetime.now(timezone.utc)
    disc = _disc(created_offset_hours=-2.0)  # created 2h in the future (anomaly)
    pr = {**_pr(), "merged_at": _iso(now)}

    with patch.object(_mod, "find_linked_pr", return_value=(0, pr)):
        result = process_discussion(disc, dry_run=False, verbose=False)

    assert result == "bad_timestamps"


# ── heuristic estimation ──────────────────────────────────────────────────────


def test_inferred_est_min_half_hour():
    """Very fast merges should still get at least 0.5h heuristic estimate."""
    block = build_completion_block(0.1, "2026-05-10T00:00:00Z", None, 0.5)
    assert "estimated_hours: 0.5" in block


def test_inferred_est_rounded_to_half_hour():
    block = build_completion_block(3.7, "2026-05-10T00:00:00Z", None, 4.0)
    assert "estimated_hours: 4.0" in block
