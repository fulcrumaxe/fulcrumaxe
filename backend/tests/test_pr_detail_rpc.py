"""
Unit tests for the dashboard.pr_detail RPC method and the
CostTracker.per_pr_summary helper it depends on.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# CostTracker.per_pr_summary tests
# ---------------------------------------------------------------------------


class TestPerPrSummary(unittest.TestCase):
    def _make_tracker(self, agent_records: dict) -> object:
        """Create a CostTracker with a mocked Blackboard."""
        from backend.cost_tracker import CostTracker

        bb = MagicMock()
        # quality/<N> returns None by default (no linked discussion)
        bb.read.side_effect = lambda key: agent_records.get(key)
        bb.list_keys.return_value = list(agent_records.keys())

        tracker = CostTracker.__new__(CostTracker)
        tracker._bb = bb
        from backend.cost_tracker import _load_pricing
        tracker._pricing = _load_pricing()
        return tracker

    def test_returns_none_when_no_matching_records(self):
        """per_pr_summary returns None when no budget/agents/ records match."""
        from backend.cost_tracker import CostTracker

        bb = MagicMock()
        bb.read.return_value = None
        bb.list_keys.return_value = []

        tracker = CostTracker.__new__(CostTracker)
        tracker._bb = bb
        from backend.cost_tracker import _load_pricing
        tracker._pricing = _load_pricing()

        result = tracker.per_pr_summary(999)
        self.assertIsNone(result)

    def test_returns_none_when_agents_have_different_pr(self):
        """per_pr_summary returns None when agent records don't match the PR."""
        from backend.cost_tracker import CostTracker, _AGENTS_PREFIX

        bb = MagicMock()
        bb.read.side_effect = lambda key: {
            f"{_AGENTS_PREFIX}executor-123": {
                "agent": "executor",
                "agent_id": "executor-123",
                "input": 1000,
                "output": 200,
                "model": "default",
                "discussion": 50,  # linked to discussion 50, not to our PR
            }
        }.get(key)
        bb.list_keys.return_value = [f"{_AGENTS_PREFIX}executor-123"]

        tracker = CostTracker.__new__(CostTracker)
        tracker._bb = bb
        from backend.cost_tracker import _load_pricing
        tracker._pricing = _load_pricing()

        # No quality record for PR 999
        result = tracker.per_pr_summary(999)
        self.assertIsNone(result)

    def test_aggregates_matching_records(self):
        """per_pr_summary aggregates tokens and USD when agent_id contains PR number."""
        from backend.cost_tracker import CostTracker, _AGENTS_PREFIX

        pr_number = 385
        agent_records = {
            f"{_AGENTS_PREFIX}executor-385-abc": {
                "agent": "executor",
                "agent_id": f"executor-{pr_number}-abc",
                "input": 38000,
                "output": 2800,
                "model": "default",
                "discussion": 368,
            },
            f"{_AGENTS_PREFIX}code-reviewer-385-def": {
                "agent": "code-reviewer",
                "agent_id": f"code-reviewer-{pr_number}-def",
                "input": 7000,
                "output": 400,
                "model": "default",
                "discussion": 368,
            },
        }

        bb = MagicMock()
        bb.read.side_effect = lambda key: agent_records.get(key)
        bb.list_keys.return_value = list(agent_records.keys())

        tracker = CostTracker.__new__(CostTracker)
        tracker._bb = bb
        from backend.cost_tracker import _load_pricing
        tracker._pricing = _load_pricing()

        result = tracker.per_pr_summary(pr_number)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 45000)
        self.assertEqual(result["output_tokens"], 3200)
        self.assertEqual(result["total_tokens"], 48200)
        self.assertGreater(result["usd"], 0)
        self.assertEqual(len(result["by_role"]), 2)
        roles = {r["role"] for r in result["by_role"]}
        self.assertIn("executor", roles)
        self.assertIn("code-reviewer", roles)

    def test_by_role_structure(self):
        """Each by_role entry has the expected keys."""
        from backend.cost_tracker import CostTracker, _AGENTS_PREFIX

        pr_number = 100
        agent_records = {
            f"{_AGENTS_PREFIX}executor-100-x": {
                "agent": "executor",
                "agent_id": "executor-100-x",
                "input": 5000,
                "output": 500,
                "model": "default",
                "discussion": 42,
            }
        }

        bb = MagicMock()
        bb.read.side_effect = lambda key: agent_records.get(key)
        bb.list_keys.return_value = list(agent_records.keys())

        tracker = CostTracker.__new__(CostTracker)
        tracker._bb = bb
        from backend.cost_tracker import _load_pricing
        tracker._pricing = _load_pricing()

        result = tracker.per_pr_summary(pr_number)
        self.assertIsNotNone(result)
        for key in ("input_tokens", "output_tokens", "total_tokens", "usd", "by_role"):
            self.assertIn(key, result)
        role_entry = result["by_role"][0]
        for key in ("role", "input_tokens", "output_tokens", "usd"):
            self.assertIn(key, role_entry)


# ---------------------------------------------------------------------------
# _rpc_pr_detail handler tests (using fixture env)
# ---------------------------------------------------------------------------


class TestPrDetailRpc(unittest.TestCase):
    def _call_rpc(self, params: dict) -> dict:
        """Call _rpc_pr_detail directly (bypasses HTTP layer)."""
        from backend import server as _srv

        # Find the registered handler
        handler = _srv._RPC_METHODS.get("dashboard.pr_detail")
        self.assertIsNotNone(handler, "dashboard.pr_detail RPC method not registered")
        return handler(params)

    def test_fixture_mode_happy_path(self):
        """In E2E fixture mode, returns pr_detail fixture for PR 385."""
        import os

        with tempfile.TemporaryDirectory() as tmp_dir:
            fixtures = {
                "pr_detail": {
                    "pr": {"number": 385, "title": "Test PR"},
                    "discussion": None,
                    "quality": None,
                    "cost": None,
                    "review_rounds": 0,
                },
                "pr_detail_not_found": {"error": "not_found"},
            }
            fixture_file = Path(tmp_dir) / "e2e-fixtures.json"
            fixture_file.write_text(json.dumps(fixtures))

            # Patch the fixture path inside server.py
            import backend.server as srv
            original_root = srv._REPO_ROOT
            srv._REPO_ROOT = Path(tmp_dir)

            # Create the expected subpath
            (Path(tmp_dir) / ".autonomous-team" / "tmp").mkdir(parents=True)
            dest = Path(tmp_dir) / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
            dest.write_text(json.dumps(fixtures))

            try:
                with patch.dict(os.environ, {"AF_E2E_FIXTURES": "1"}):
                    result = self._call_rpc({"pr_number": 385})
                self.assertIn("pr", result)
                self.assertEqual(result["pr"]["number"], 385)
            finally:
                srv._REPO_ROOT = original_root

    def test_fixture_mode_not_found(self):
        """In E2E fixture mode, returns not_found for PR 999999."""
        import os

        with tempfile.TemporaryDirectory() as tmp_dir:
            fixtures = {
                "pr_detail": {
                    "pr": {"number": 385, "title": "Test PR"},
                    "discussion": None,
                    "quality": None,
                    "cost": None,
                    "review_rounds": 0,
                },
                "pr_detail_not_found": {"error": "not_found"},
            }
            import backend.server as srv
            original_root = srv._REPO_ROOT
            srv._REPO_ROOT = Path(tmp_dir)

            (Path(tmp_dir) / ".autonomous-team" / "tmp").mkdir(parents=True)
            dest = Path(tmp_dir) / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
            dest.write_text(json.dumps(fixtures))

            try:
                with patch.dict(os.environ, {"AF_E2E_FIXTURES": "1"}):
                    result = self._call_rpc({"pr_number": 999999})
                self.assertEqual(result.get("error"), "not_found")
            finally:
                srv._REPO_ROOT = original_root

    def test_invalid_pr_number_raises(self):
        """Passing a non-integer pr_number raises InvalidParams."""
        with self.assertRaises(Exception) as ctx:
            self._call_rpc({"pr_number": "notanumber"})
        # Should raise an RPC -32602 error
        exc = ctx.exception
        self.assertTrue(
            hasattr(exc, "rpc_code") and exc.rpc_code == -32602
            or "integer" in str(exc).lower()
            or "invalid" in str(exc).lower(),
            f"Expected InvalidParams-style error, got: {exc}",
        )

    def test_gh_pr_view_failure_returns_not_found(self):
        """When gh pr view fails, the RPC returns not_found."""
        import os
        import backend.server as srv

        with patch.object(srv, "_gh_pr_view", return_value=None):
            with patch.dict(os.environ, {}, clear=False):
                # Ensure fixture mode is off
                os.environ.pop("AF_E2E_FIXTURES", None)
                result = self._call_rpc({"pr_number": 12345})
        self.assertEqual(result.get("error"), "not_found")


if __name__ == "__main__":
    unittest.main()
