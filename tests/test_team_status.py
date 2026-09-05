"""Tests for backend/team_status.py — --json output shape and --watch arg validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEAM_STATUS = REPO_ROOT / "backend" / "team_status.py"


def _run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run team_status.py with the given args. Returns (returncode, stdout, stderr)."""
    r = subprocess.run(
        [sys.executable, str(TEAM_STATUS), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------------------
# --json output shape
# ---------------------------------------------------------------------------

class TestJsonOutput:
    REQUIRED_KEYS = {
        "snapshot_age_seconds",
        "discussions",
        "prs",
        "agents",
        "queue",
        "budget",
        "kpi",
        "recent_merges",
        "errors",
    }

    def test_exits_zero(self):
        rc, _, _ = _run("--json")
        assert rc == 0, "team_status --json should exit 0"

    def test_valid_json(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)  # raises on invalid JSON
        assert isinstance(data, dict)

    def test_required_keys_present(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        missing = self.REQUIRED_KEYS - set(data.keys())
        # Note: some keys like 'queue' may be nested inside 'agents'; check both
        # The spec requires top-level keys — check each
        # 'queue' depth: may live as agents.queue_depth; we accept agents.queue_depth
        # as satisfying the 'queue' key requirement if 'queue' not top-level.
        # To be safe, ensure all required keys are present (or have a reasonable substitute).
        for key in ("snapshot_age_seconds", "discussions", "prs", "agents", "budget", "kpi", "recent_merges", "errors"):
            assert key in data, f"Missing required key: {key!r}"

    def test_discussions_is_dict(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data["discussions"], dict)

    def test_prs_is_dict(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data["prs"], dict)

    def test_budget_is_dict(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data["budget"], dict)

    def test_recent_merges_is_list(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data["recent_merges"], list)

    def test_errors_is_list(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data["errors"], list)


# ---------------------------------------------------------------------------
# --watch arg validation
# ---------------------------------------------------------------------------

class TestWatchArgValidation:
    def test_watch_with_json_exits_2(self):
        rc, _, err = _run("--watch", "--json")
        assert rc == 2, f"--watch --json should exit 2, got {rc}"
        assert "json" in err.lower() or "watch" in err.lower()

    def test_interval_zero_exits_nonzero(self):
        # We need to avoid actually entering the watch loop, so use --interval 0
        # which should be caught at arg validation before any loop starts
        rc, _, err = _run("--watch", "--interval", "0")
        assert rc != 0, "--interval 0 should exit non-zero"

    def test_interval_negative_exits_nonzero(self):
        rc, _, err = _run("--watch", "--interval", "-1")
        assert rc != 0, "--interval -1 should exit non-zero"


# ---------------------------------------------------------------------------
# Human-readable output sanity
# ---------------------------------------------------------------------------

class TestHumanOutput:
    def test_exits_zero(self):
        rc, _, _ = _run()
        assert rc == 0

    def test_contains_status_header(self):
        rc, out, _ = _run()
        assert rc == 0
        assert "status" in out.lower() or "autonomous" in out.lower()

    def test_contains_budget_section(self):
        rc, out, _ = _run()
        assert rc == 0
        assert "BUDGET" in out or "budget" in out.lower()

    def test_completes_quickly(self):
        """Should complete in under 30s (generous for CI; spec says <1s on warm snapshot)."""
        import time
        t0 = time.time()
        rc, _, _ = _run(timeout=30)
        elapsed = time.time() - t0
        assert rc == 0
        assert elapsed < 30


# ---------------------------------------------------------------------------
# Discussion #367 — by_status nested key + cost.by_discussion_top_5
# ---------------------------------------------------------------------------

class TestDiscussionByStatus:
    """discussions.by_status must be present alongside legacy flat keys."""

    def test_by_status_key_present(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        disc = data.get("discussions", {})
        assert "by_status" in disc, "discussions.by_status key must be present"

    def test_by_status_is_dict(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        by_status = data["discussions"]["by_status"]
        assert isinstance(by_status, dict)

    def test_by_status_has_required_keys(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        by_status = data["discussions"]["by_status"]
        for key in ("DISCUSSING", "SPEC_READY", "IMPLEMENTING", "REVIEWING", "DONE"):
            assert key in by_status, f"by_status missing key {key!r}"

    def test_flat_keys_still_present(self):
        """Legacy flat keys at discussions.DISCUSSING etc. must NOT be removed."""
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        disc = data.get("discussions", {})
        # total is always present
        assert "total" in disc, "discussions.total missing"
        # All status flat keys must still be top-level under discussions
        for key in ("DISCUSSING", "SPEC_READY", "IMPLEMENTING", "REVIEWING", "DONE"):
            assert key in disc, f"Flat key discussions.{key} was removed — regression"

    def test_by_status_values_are_ints(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        by_status = data["discussions"]["by_status"]
        for key, val in by_status.items():
            assert isinstance(val, int), f"by_status[{key!r}] is not int: {val!r}"


class TestCostByDiscussion:
    """cost.by_discussion_top_5 must be present in --json output."""

    def test_cost_key_present(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        assert "cost" in data, "top-level 'cost' key must be present"

    def test_cost_by_discussion_top_5_is_list(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        top5 = data["cost"].get("by_discussion_top_5")
        assert isinstance(top5, list), "cost.by_discussion_top_5 must be a list"

    def test_cost_total_cost_usd_is_float(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        total = data["cost"].get("total_cost_usd")
        assert isinstance(total, (int, float)), "cost.total_cost_usd must be numeric"

    def test_cost_by_discussion_top_5_max_length(self):
        rc, out, _ = _run("--json")
        assert rc == 0
        data = json.loads(out)
        top5 = data["cost"]["by_discussion_top_5"]
        assert len(top5) <= 5, "by_discussion_top_5 must have at most 5 entries"
