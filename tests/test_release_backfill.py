"""
Tests for release_backfill.py.

Acceptance criteria:
  - git-log (#NNN) parse extracts correct PR numbers
  - Idempotency: second run produces 0 new records (skips already-recorded PRs)
  - Empty range (no PRs in window): returns 0 written, 0 errors
  - merged_at comes from the BULK MAP (gh pr list), not from now() or per-PR calls
  - PRs missing from the bulk map are SKIPPED and warned, never now()-stamped
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# _git_log_pr_numbers — parse tests
# ---------------------------------------------------------------------------

def test_parse_pr_numbers_from_typical_squash_merge():
    """Standard squash-merge subject '...title (#123)' extracts PR 123."""
    from backend.release_backfill import _PR_NUMBER_RE
    subject = "add URL detection for Meet, Zoom, and Teams (#123)"
    matches = [int(m.group(1)) for m in _PR_NUMBER_RE.finditer(subject)]
    assert matches == [123]


def test_parse_pr_numbers_extracts_multiple():
    """Multiple PR refs in one subject are all extracted."""
    from backend.release_backfill import _PR_NUMBER_RE
    subject = "merge cleanup (#456) and fix (#789)"
    matches = [int(m.group(1)) for m in _PR_NUMBER_RE.finditer(subject)]
    assert 456 in matches
    assert 789 in matches


def test_parse_pr_numbers_skips_no_match():
    """Subjects without (#NNN) produce no matches."""
    from backend.release_backfill import _PR_NUMBER_RE
    subject = "Initial commit"
    matches = [int(m.group(1)) for m in _PR_NUMBER_RE.finditer(subject)]
    assert matches == []


def test_git_log_pr_numbers_deduplicates():
    """Same PR number appearing twice in git log is deduplicated."""
    fake_log = "fix thing (#100)\nfix thing (#100)\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_log, stderr="")
        from backend.release_backfill import _git_log_pr_numbers
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=7)
        result = _git_log_pr_numbers(since)
    assert result.count(100) == 1


def test_git_log_pr_numbers_empty_log():
    """Empty git log returns empty list."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from backend.release_backfill import _git_log_pr_numbers
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=7)
        result = _git_log_pr_numbers(since)
    assert result == []


def test_git_log_failure_raises():
    """Non-zero git exit raises RuntimeError."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="not a repo")
        from backend.release_backfill import _git_log_pr_numbers
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=7)
        with pytest.raises(RuntimeError, match="git log failed"):
            _git_log_pr_numbers(since)


# ---------------------------------------------------------------------------
# _bulk_fetch_merged_at
# ---------------------------------------------------------------------------

def test_bulk_fetch_returns_merged_at_map():
    """_bulk_fetch_merged_at returns {pr_number: mergedAt} from gh pr list output."""
    fake_gh_output = json.dumps([
        {"number": 100, "mergedAt": "2026-05-01T12:00:00Z"},
        {"number": 200, "mergedAt": "2026-05-10T08:30:00Z"},
        {"number": 999, "mergedAt": "2026-05-20T15:00:00Z"},
    ])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_gh_output, stderr="")
        from backend.release_backfill import _bulk_fetch_merged_at
        result = _bulk_fetch_merged_at([100, 200])

    # Only requested PRs are in the result
    assert result == {
        100: "2026-05-01T12:00:00Z",
        200: "2026-05-10T08:30:00Z",
    }
    # PR 999 was not requested, so it's absent
    assert 999 not in result


def test_bulk_fetch_empty_list():
    """Empty pr_numbers list returns empty dict without calling gh."""
    with patch("subprocess.run") as mock_run:
        from backend.release_backfill import _bulk_fetch_merged_at
        result = _bulk_fetch_merged_at([])
    mock_run.assert_not_called()
    assert result == {}


def test_bulk_fetch_missing_pr_absent_from_map():
    """PRs not found in gh pr list output are simply absent from the returned map."""
    fake_gh_output = json.dumps([
        {"number": 100, "mergedAt": "2026-05-01T12:00:00Z"},
    ])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_gh_output, stderr="")
        from backend.release_backfill import _bulk_fetch_merged_at
        result = _bulk_fetch_merged_at([100, 777])  # 777 not in gh output

    assert 100 in result
    assert 777 not in result  # missing, not raised


def test_bulk_fetch_gh_failure_raises():
    """Non-zero gh exit raises RuntimeError."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="API rate limit exceeded")
        from backend.release_backfill import _bulk_fetch_merged_at
        with pytest.raises(RuntimeError, match="gh pr list failed"):
            _bulk_fetch_merged_at([100])


def test_bulk_fetch_makes_single_call():
    """_bulk_fetch_merged_at issues exactly ONE subprocess call regardless of PR count."""
    fake_gh_output = json.dumps([{"number": n, "mergedAt": f"2026-05-01T{n:02d}:00:00Z"} for n in range(1, 51)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_gh_output, stderr="")
        from backend.release_backfill import _bulk_fetch_merged_at
        _bulk_fetch_merged_at(list(range(1, 51)))

    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# _already_recorded_pr_numbers
# ---------------------------------------------------------------------------

def test_already_recorded_reads_pr_numbers(tmp_path):
    """_already_recorded_pr_numbers reads pr_numbers from existing release files."""
    releases_dir = tmp_path / ".autonomous-team" / "releases"
    releases_dir.mkdir(parents=True)
    record = {"id": "2026-05-21-001", "pr_numbers": [42, 43], "merged_at": "2026-05-21T00:00:00Z"}
    (releases_dir / "2026-05-21-001.json").write_text(json.dumps(record))

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir
    try:
        result = rb._already_recorded_pr_numbers()
    finally:
        rb._RELEASES_DIR = original_dir

    assert 42 in result
    assert 43 in result


def test_already_recorded_empty_dir(tmp_path):
    """Empty releases dir returns empty set."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir
    try:
        result = rb._already_recorded_pr_numbers()
    finally:
        rb._RELEASES_DIR = original_dir

    assert result == set()


def test_already_recorded_missing_dir(tmp_path):
    """Missing releases dir returns empty set without error."""
    releases_dir = tmp_path / "nonexistent"

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir
    try:
        result = rb._already_recorded_pr_numbers()
    finally:
        rb._RELEASES_DIR = original_dir

    assert result == set()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_second_run_skips_all(tmp_path):
    """Second backfill with same PRs writes 0 new records (idempotent)."""
    releases_dir = tmp_path / ".autonomous-team" / "releases"
    releases_dir.mkdir(parents=True)

    # Pre-populate with PR 100 already recorded
    existing = {"id": "2026-05-21-001", "pr_numbers": [100], "merged_at": "2026-05-21T00:00:00Z"}
    (releases_dir / "2026-05-21-001.json").write_text(json.dumps(existing))

    fake_log = "some feature (#100)\n"

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_log, stderr="")
            with patch("backend.release_backfill.record_release") as mock_record:
                result = rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    # PR 100 already recorded — record_release must NOT be called
    mock_record.assert_not_called()
    assert result["skipped"] == 1
    assert result["written"] == 0
    assert result["discovered"] == 1


def test_idempotency_new_prs_are_written(tmp_path):
    """PRs not yet recorded are passed to record_release with real merged_at."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir(parents=True)
    # PR 99 already there, 100 is new
    existing = {"id": "2026-05-21-001", "pr_numbers": [99], "merged_at": "2026-05-21T00:00:00Z"}
    (releases_dir / "2026-05-21-001.json").write_text(json.dumps(existing))

    fake_log_output = "feat A (#99)\nfeat B (#100)\n"
    fake_bulk_output = json.dumps([{"number": 100, "mergedAt": "2026-05-10T12:00:00Z"}])

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    try:
        def fake_subprocess(cmd, **kwargs):
            if cmd[0] == "git":
                return MagicMock(returncode=0, stdout=fake_log_output, stderr="")
            if cmd[0] == "gh":
                return MagicMock(returncode=0, stdout=fake_bulk_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_subprocess):
            with patch("backend.release_backfill.record_release") as mock_record:
                mock_record.return_value = {"id": "2026-05-21-002", "pr_numbers": [100]}
                result = rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    mock_record.assert_called_once_with(
        pr_numbers=[100],
        merged_at="2026-05-10T12:00:00Z",
        dry_run=False,
    )
    assert result["written"] == 1
    assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Empty range
# ---------------------------------------------------------------------------

def test_empty_range_no_op(tmp_path):
    """When git log returns no PRs, backfill is a no-op."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("backend.release_backfill.record_release") as mock_record:
                result = rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    mock_record.assert_not_called()
    assert result["discovered"] == 0
    assert result["written"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# merged_at comes from bulk map, not now()
# ---------------------------------------------------------------------------

def test_merged_at_comes_from_bulk_map_not_now(tmp_path):
    """record_release receives merged_at from the bulk map, not now().

    This is the core fix: backfill must pass merged_at=<real_date> to
    record_release. The real date comes from gh pr list (bulk call), not
    from datetime.now().
    """
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    fake_log_output = "fix timer drift (#200)\n"
    real_merged_at = "2026-04-15T09:30:00Z"
    fake_bulk_output = json.dumps([{"number": 200, "mergedAt": real_merged_at}])

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    captured_calls = []

    def capture_record_release(**kwargs):
        captured_calls.append(kwargs)
        return {"id": "2026-05-21-001", "pr_numbers": [200], "merged_at": real_merged_at}

    try:
        def fake_subprocess(cmd, **kwargs):
            if cmd[0] == "git":
                return MagicMock(returncode=0, stdout=fake_log_output, stderr="")
            if cmd[0] == "gh":
                return MagicMock(returncode=0, stdout=fake_bulk_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_subprocess):
            with patch("backend.release_backfill.record_release", side_effect=capture_record_release):
                result = rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    assert len(captured_calls) == 1
    call_kwargs = captured_calls[0]

    # merged_at must be the real historical date from bulk map, not today
    assert call_kwargs["merged_at"] == real_merged_at, (
        f"Expected merged_at={real_merged_at!r}, got {call_kwargs.get('merged_at')!r}. "
        "Backfill must use real mergedAt from bulk gh pr list, not now()."
    )
    assert call_kwargs["pr_numbers"] == [200]


def test_pr_missing_from_bulk_map_is_skipped_not_now_stamped(tmp_path):
    """A PR absent from the bulk gh pr list result is SKIPPED, never now()-stamped.

    This is the critical safety invariant: if the bulk fetch doesn't have
    a PR's mergedAt, we skip it with a warning. We never fall back to now().
    """
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    fake_log_output = "fix A (#300)\nfix B (#400)\n"
    # Bulk map only has PR 300, not 400
    fake_bulk_output = json.dumps([{"number": 300, "mergedAt": "2026-05-01T10:00:00Z"}])

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    called_with = []

    def capture(**kwargs):
        called_with.append(kwargs)
        return {"id": "2026-05-21-001", "pr_numbers": [300]}

    try:
        def fake_subprocess(cmd, **kwargs):
            if cmd[0] == "git":
                return MagicMock(returncode=0, stdout=fake_log_output, stderr="")
            if cmd[0] == "gh":
                return MagicMock(returncode=0, stdout=fake_bulk_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_subprocess):
            with patch("backend.release_backfill.record_release", side_effect=capture):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    result = rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    # PR 300 written, PR 400 skipped
    assert result["written"] == 1
    assert len(result["skipped_no_date"]) == 1
    assert 400 in result["skipped_no_date"]

    # record_release was NOT called for PR 400
    assert all(kw["pr_numbers"] != [400] for kw in called_with), (
        "record_release must not be called for PR #400 — it has no mergedAt"
    )

    # A warning was emitted for the skipped PR
    assert any("400" in str(warning.message) for warning in w), (
        "Expected a warning about PR #400 being skipped"
    )

    # Verify record_release was NOT called with now() as merged_at
    for kw in called_with:
        ma = kw.get("merged_at", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert not ma.startswith(today), (
            f"record_release was called with today's date {today!r} as merged_at — "
            "this indicates now() fallback was used (the bug we're fixing)"
        )


def test_bulk_fetch_not_called_per_pr(tmp_path):
    """The backfill module calls gh ONCE (bulk), not once per PR.

    290 PRs → 1 gh call, not 290.
    """
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    # 10 PRs in git log
    fake_log_lines = "\n".join(f"feat #{n} (#{n})" for n in range(1, 11))
    fake_bulk_output = json.dumps([
        {"number": n, "mergedAt": f"2026-05-0{n % 9 + 1}T10:00:00Z"} for n in range(1, 11)
    ])

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    gh_call_count = 0

    def fake_subprocess(cmd, **kwargs):
        nonlocal gh_call_count
        if cmd[0] == "git":
            return MagicMock(returncode=0, stdout=fake_log_lines, stderr="")
        if cmd[0] == "gh":
            gh_call_count += 1
            return MagicMock(returncode=0, stdout=fake_bulk_output, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    try:
        with patch("subprocess.run", side_effect=fake_subprocess):
            with patch("backend.release_backfill.record_release") as mock_record:
                mock_record.return_value = {"id": "x", "pr_numbers": [1]}
                rb.backfill(since_str="7d", dry_run=False)
    finally:
        rb._RELEASES_DIR = original_dir

    assert gh_call_count == 1, (
        f"Expected 1 gh call (bulk), got {gh_call_count}. "
        "Backfill must not call gh once per PR."
    )


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------

def test_parse_since_7d():
    """'7d' parses to roughly 7 days ago."""
    from backend.release_backfill import _parse_since
    before = datetime.now(timezone.utc)
    result = _parse_since("7d")
    after = datetime.now(timezone.utc)
    delta = before - result
    assert 6 * 24 * 3600 < delta.total_seconds() < 8 * 24 * 3600


def test_parse_since_invalid():
    """Invalid since string raises ValueError."""
    from backend.release_backfill import _parse_since
    with pytest.raises(ValueError):
        _parse_since("2weeks")


def test_parse_since_30d():
    """'30d' parses to roughly 30 days ago."""
    from backend.release_backfill import _parse_since
    before = datetime.now(timezone.utc)
    result = _parse_since("30d")
    delta = before - result
    assert 29 * 24 * 3600 < delta.total_seconds() < 31 * 24 * 3600


# ---------------------------------------------------------------------------
# dry_run flag propagates
# ---------------------------------------------------------------------------

def test_dry_run_propagates_to_record_release(tmp_path):
    """dry_run=True is forwarded to record_release."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()

    fake_log_output = "feat X (#300)\n"
    fake_bulk_output = json.dumps([{"number": 300, "mergedAt": "2026-05-01T10:00:00Z"}])

    from backend import release_backfill as rb
    original_dir = rb._RELEASES_DIR
    rb._RELEASES_DIR = releases_dir

    captured_calls = []

    def capture(**kwargs):
        captured_calls.append(kwargs)
        return {"id": "2026-05-21-001", "pr_numbers": [300]}

    try:
        def fake_subprocess(cmd, **kwargs):
            if cmd[0] == "git":
                return MagicMock(returncode=0, stdout=fake_log_output, stderr="")
            if cmd[0] == "gh":
                return MagicMock(returncode=0, stdout=fake_bulk_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_subprocess):
            with patch("backend.release_backfill.record_release", side_effect=capture):
                result = rb.backfill(since_str="7d", dry_run=True)
    finally:
        rb._RELEASES_DIR = original_dir

    assert captured_calls[0]["dry_run"] is True
    # dry_run=True means nothing is "written" by the backfill count
    assert result["written"] == 0
