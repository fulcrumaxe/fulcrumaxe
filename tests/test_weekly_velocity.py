"""Tests for backend/stats/weekly_velocity.py

Validates:
  AC1 — shape and non-zero total (monkeypatched with realistic fixture)
  AC2 — RPC handler delegates to weekly_velocity() with correct repo
  AC3 — per-project isolation: projectb gets its own repo, not AF's
  AC4 — cache: two calls within 60s for the same repo issue exactly one gh subprocess
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pr_list(merged_ats: list[datetime]) -> str:
    """Build gh pr list --json output string."""
    rows = [
        {"number": i + 1, "mergedAt": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for i, dt in enumerate(merged_ats)
    ]
    return json.dumps(rows)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clear_cache(mod, repo: str | None = None) -> None:
    """Reset the per-repo cache for a given repo slug (or clear all)."""
    if repo is None:
        mod._CACHE.clear()
    else:
        mod._CACHE.pop(repo, None)


# ---------------------------------------------------------------------------
# AC1 — shape
# ---------------------------------------------------------------------------

class TestWeeklyVelocityShape:
    def test_returns_expected_keys(self):
        now = _now()
        # 5 PRs in the last 7 days
        merged_ats = [now - timedelta(days=i) for i in range(5)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats),
            stderr="",
            returncode=0,
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert isinstance(result, dict)
        assert "total" in result
        assert "by_day" in result
        assert "window_start" in result
        assert "window_end" in result
        assert "prev_total" in result
        assert "trend_pct" in result

    def test_by_day_has_7_entries(self):
        now = _now()
        merged_ats = [now - timedelta(days=1), now - timedelta(days=3)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert len(result["by_day"]) == 7

    def test_by_day_dates_are_ascending(self):
        now = _now()
        merged_ats = [now - timedelta(days=i) for i in range(3)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        dates = [d["date"] for d in result["by_day"]]
        assert dates == sorted(dates)

    def test_total_is_non_zero_when_prs_present(self):
        now = _now()
        merged_ats = [now - timedelta(hours=2), now - timedelta(hours=10)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert result["total"] == 2

    def test_empty_window_returns_zero_total(self):
        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert result["total"] == 0
        assert len(result["by_day"]) == 7
        for day in result["by_day"]:
            assert day["count"] == 0

    def test_applicable_field_present_in_response(self):
        """Response must always include the applicable boolean."""
        now = _now()
        merged_ats = [now - timedelta(hours=2)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert "applicable" in result
        assert result["applicable"] is True

    def test_applicable_false_when_no_prs_in_14d(self):
        """applicable must be False when the 14-day fetch returns no PRs at all."""
        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert result["applicable"] is False

    def test_applicable_true_when_only_prior_week_has_prs(self):
        """applicable is True when prior 7d has PRs even if current 7d is empty."""
        now = _now()
        # PR merged 10 days ago — falls in prior window, not current
        merged_ats = [now - timedelta(days=10)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        assert result["total"] == 0
        assert result["prev_total"] == 1
        assert result["applicable"] is True


# ---------------------------------------------------------------------------
# Regression: sparkline off-by-one — PRs merged today must appear in by_day
# ---------------------------------------------------------------------------

class TestTodayAlignment:
    """PRs merged exactly at now must be counted in both total and by_day sum."""

    def test_pr_merged_today_in_by_day(self):
        now = _now()
        # Merge time is "now" — i.e. today
        merged_ats = [now]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        by_day_sum = sum(d["count"] for d in result["by_day"])
        assert result["total"] == 1, "today's PR must be in total"
        assert by_day_sum == 1, "today's PR must be in by_day sparkline"
        assert result["total"] == by_day_sum, "headline total and sparkline sum must agree"

    def test_today_bucket_exists(self):
        """The last bucket in by_day must be today's date."""
        now = _now()
        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            result = mod.weekly_velocity()

        last_date = result["by_day"][-1]["date"]
        assert last_date == now.date().isoformat(), (
            f"last by_day entry should be today ({now.date().isoformat()}), got {last_date}"
        )


# ---------------------------------------------------------------------------
# AC2 — RPC handler
# ---------------------------------------------------------------------------

class TestRpcHandler:
    def test_handle_returns_weekly_velocity_dict(self):
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result):
            from backend.rpc.stats_weekly_velocity import handle
            result = handle({})

        assert "total" in result
        assert "by_day" in result

    def test_handle_passes_repo_for_project(self):
        """RPC handler must resolve project's repo and pass it to weekly_velocity."""
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with patch("backend.rpc.stats_weekly_velocity._resolve_repo", return_value="acme/projectb") as mock_resolve, \
             patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            from backend.rpc.stats_weekly_velocity import handle
            handle({"project": "projectb"})

        mock_resolve.assert_called_once_with("projectb")
        # The gh pr list command must use the resolved repo, not the AF default
        call_args = mock_run.call_args[0][0]  # first positional arg = cmd list
        assert "acme/projectb" in call_args


# ---------------------------------------------------------------------------
# AC3 — Per-project isolation
# ---------------------------------------------------------------------------

class TestPerProjectIsolation:
    """Calling weekly_velocity with different repo slugs must produce independent results."""

    def test_different_repos_get_independent_results(self):
        now = _now()
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        # AF has 10 PRs, projectb has 3
        af_prs = [now - timedelta(hours=i) for i in range(10)]
        projectb_prs = [now - timedelta(hours=i) for i in range(3)]

        af_fake = SimpleNamespace(stdout=_make_pr_list(af_prs), stderr="", returncode=0)
        projectb_fake = SimpleNamespace(stdout=_make_pr_list(projectb_prs), stderr="", returncode=0)

        with patch("backend.stats.weekly_velocity.subprocess.run") as mock_run:
            mock_run.side_effect = [af_fake, projectb_fake]
            af_result = mod.weekly_velocity(repo="autonomous-agent-7/autonomous-forever")
            projectb_result = mod.weekly_velocity(repo="acme/projectb")

        assert af_result["total"] == 10
        assert projectb_result["total"] == 3

    def test_explicit_repo_queries_correct_github_repo(self):
        """gh pr list must be called with the exact repo slug passed in."""
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            mod.weekly_velocity(repo="acme/projectb")

        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        repo_idx = cmd.index("--repo")
        assert cmd[repo_idx + 1] == "acme/projectb", (
            f"expected 'acme/projectb', got {cmd[repo_idx + 1]}"
        )

    def test_no_repo_arg_uses_module_default(self):
        """Calling without repo must still work (uses AF module-level default)."""
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)
        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            result = mod.weekly_velocity()

        assert result["total"] == 0
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        # Must be a non-empty slug (the AF default)
        repo_idx = cmd.index("--repo")
        assert "/" in cmd[repo_idx + 1]


# ---------------------------------------------------------------------------
# AC4 — cache: exactly one subprocess call within 60s window (per-repo)
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def test_two_calls_within_ttl_issue_one_subprocess(self):
        now = _now()
        merged_ats = [now - timedelta(hours=5)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            first = mod.weekly_velocity()
            second = mod.weekly_velocity()  # should use cache

        assert mock_run.call_count == 1, (
            f"Expected 1 subprocess call, got {mock_run.call_count}"
        )
        assert first == second

    def test_call_after_ttl_re_fetches(self):
        now = _now()
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        stale_repo = mod._REPO
        # Pre-inject expired entry
        mod._CACHE[stale_repo] = {
            "data": {"total": 99, "by_day": [], "window_start": "", "window_end": "", "prev_total": 0, "trend_pct": 0},
            "ts": time.monotonic() - 61.0,  # expired
        }

        merged_ats = [now - timedelta(hours=5)]
        fake_result = SimpleNamespace(
            stdout=_make_pr_list(merged_ats), stderr="", returncode=0
        )
        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            result = mod.weekly_velocity()

        assert mock_run.call_count == 1
        # Result should NOT be the stale 99 value
        assert result["total"] != 99

    def test_different_repos_share_no_cache(self):
        """Cache for repo A must not be served for repo B."""
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        now = _now()
        fake_result = SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with patch("backend.stats.weekly_velocity.subprocess.run", return_value=fake_result) as mock_run:
            mod.weekly_velocity(repo="autonomous-agent-7/autonomous-forever")
            mod.weekly_velocity(repo="acme/projectb")

        # Both repos are cache-miss — expect 2 subprocess calls
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_subprocess_failure_returns_zero_total(self):
        import subprocess
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch(
            "backend.stats.weekly_velocity.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "gh", stderr="rate limited"),
        ):
            result = mod.weekly_velocity()

        assert result["total"] == 0
        assert len(result["by_day"]) == 7

    def test_timeout_returns_zero_total(self):
        import subprocess
        import backend.stats.weekly_velocity as mod
        _clear_cache(mod)

        with patch(
            "backend.stats.weekly_velocity.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 15),
        ):
            result = mod.weekly_velocity()

        assert result["total"] == 0
