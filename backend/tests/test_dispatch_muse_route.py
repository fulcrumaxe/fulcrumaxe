"""Tests for the dispatch.py Muse route (AF_LANE=muse).

When the loop runs on the Muse lane, the dispatcher returns a "muse" routing
signal (mirroring "cc") instead of running Claude-credit/offload logic: spend
accounting lives on a different plane under Muse (subscription gating in
pre-spawn-check), and offload runners are Claude-credential-bound.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator import dispatch


def _route_with(spec_dict: dict, lane: str | None = "muse") -> dict:
    mock_tracker = MagicMock()
    mock_tracker.remaining_usd.return_value = 100.0
    with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), patch.object(
        dispatch, "_SHADOW_MODE", "cc"
    ), patch.dict("os.environ", {}, clear=False):
        import os

        if lane is None:
            os.environ.pop("AF_LANE", None)
        else:
            os.environ["AF_LANE"] = lane
        try:
            return dispatch.route(spec_dict)
        finally:
            os.environ.pop("AF_LANE", None)


def test_muse_lane_routes_executor_to_muse() -> None:
    out = _route_with({"role": "executor", "discussion": 543})
    assert out["route"] == "muse"
    assert out["verdict"] == "routed_to_muse"
    assert out["error"] is None
    assert out["run_id"].startswith("executor-543-")


def test_muse_lane_wins_over_sdk_offload() -> None:
    out = _route_with({"role": "docs-writer", "discussion": 1, "sdk_eligible": True})
    assert out["route"] == "muse"


def test_unset_lane_preserves_cc_default() -> None:
    out = _route_with({"role": "executor", "discussion": 543}, lane=None)
    assert out["route"] == "cc"
    assert out["verdict"] == "routed_to_cc"


def test_muse_route_records_routed_via() -> None:
    with patch("backend.agent_run_tracker.start_run") as mock_start, patch(
        "backend.agent_run_tracker.complete_run"
    ) as mock_complete:
        out = _route_with({"role": "code-reviewer", "discussion": 88, "pr": 90})
    assert out["route"] == "muse"
    mock_start.assert_called_once()
    _, kwargs = mock_complete.call_args
    assert kwargs["routed_via"] == "muse"
    assert kwargs["agent_id"] == out["run_id"]
