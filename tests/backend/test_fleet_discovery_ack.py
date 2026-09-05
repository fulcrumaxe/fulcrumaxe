"""Tests for backend/rpc/fleet_discovery_ack.py's read-only counterpart,
fleet.discovery_known (D#2317 PR-a item 11).

fleet.discovery_ack itself (the write path) is pre-existing and untouched;
these tests cover only handle_query(), added so
dashboard/src/pages/fleet/lib/new-project-detector.ts can ask the backend
"what do you already know about?" without mutating known.json.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.rpc import fleet_discovery_ack


def test_handle_query_returns_persisted_known_list():
    with patch("backend.rpc.fleet_discovery_ack._read_known", return_value=["autonomous-forever", "projectb"]):
        result = fleet_discovery_ack.handle_query({})

    assert result == {"known": ["autonomous-forever", "projectb"]}


def test_handle_query_returns_empty_list_when_nothing_persisted():
    with patch("backend.rpc.fleet_discovery_ack._read_known", return_value=[]):
        result = fleet_discovery_ack.handle_query({})

    assert result == {"known": []}


def test_handle_query_never_mutates(tmp_path):
    """handle_query must be read-only: it never calls _write_known."""
    with patch("backend.rpc.fleet_discovery_ack._read_known", return_value=["x"]), \
         patch("backend.rpc.fleet_discovery_ack._write_known") as write_mock:
        fleet_discovery_ack.handle_query({})

    write_mock.assert_not_called()


def test_handle_write_path_is_unaffected():
    """The pre-existing write RPC keeps its exact contract."""
    with patch("backend.rpc.fleet_discovery_ack._read_known", return_value=[]), \
         patch("backend.rpc.fleet_discovery_ack._write_known") as write_mock:
        result = fleet_discovery_ack.handle({"project_name": "new-project"})

    assert result == {"ok": True, "known": ["new-project"]}
    write_mock.assert_called_once_with(["new-project"])
