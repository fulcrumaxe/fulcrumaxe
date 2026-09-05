"""
Tests for phased orchestration gate defaults in backend/control_plane.py.

Verifies that `phased_orchestration` and `phased_code_review` exist in
_DEFAULT_GATES with correct default values.

Note: phased_code_review defaults to True as of PR-c (D#559). The master
switch (phased_orchestration) still defaults False — the sub-gate activates
only when the master is on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_plane import ControlPlane, _DEFAULT_GATES


class TestPhasedOrchestrationGates:
    """Phased orchestration gates exist and have the correct default values."""

    def test_phased_orchestration_in_defaults(self):
        assert "phased_orchestration" in _DEFAULT_GATES, (
            "phased_orchestration must be registered in _DEFAULT_GATES"
        )

    def test_phased_orchestration_default_false(self):
        assert _DEFAULT_GATES["phased_orchestration"] is False, (
            "phased_orchestration must default to False (zero behavior change on ship)"
        )

    def test_phased_code_review_in_defaults(self):
        assert "phased_code_review" in _DEFAULT_GATES, (
            "phased_code_review must be registered in _DEFAULT_GATES"
        )

    def test_phased_code_review_default_true(self):
        # phased_code_review defaults True — PR-c (D#559) has merged.
        assert _DEFAULT_GATES["phased_code_review"] is True, (
            "phased_code_review must default to True (PR-c merged)"
        )

    def test_fresh_control_plane_has_phased_orchestration_false(self, tmp_path):
        cp = ControlPlane(config_path=tmp_path / "config.json")
        cp.load()
        assert cp.gate_enabled("phased_orchestration") is False

    def test_fresh_control_plane_has_phased_code_review_true(self, tmp_path):
        # phased_code_review defaults True after PR-c (D#559) merged.
        cp = ControlPlane(config_path=tmp_path / "config.json")
        cp.load()
        assert cp.gate_enabled("phased_code_review") is True

    def test_gates_can_be_enabled(self, tmp_path):
        cp = ControlPlane(config_path=tmp_path / "config.json")
        cp.load()
        cp.set("gates.phased_orchestration", True)
        assert cp.gate_enabled("phased_orchestration") is True

    def test_sub_gate_independent_of_main_gate(self, tmp_path):
        """phased_code_review can be set independently of phased_orchestration."""
        cp = ControlPlane(config_path=tmp_path / "config.json")
        cp.load()
        cp.set("gates.phased_orchestration", True)
        # phased_code_review defaults True — confirm it can be toggled independently
        assert cp.gate_enabled("phased_code_review") is True
        cp.set("gates.phased_code_review", False)
        assert cp.gate_enabled("phased_code_review") is False
        cp.set("gates.phased_code_review", True)
        assert cp.gate_enabled("phased_code_review") is True

    def test_existing_config_gets_phased_gates_injected(self, tmp_path):
        """A config.json without the new gates gets them injected on load (backwards compat)."""
        import json
        config_path = tmp_path / "config.json"
        # Write a config that doesn't have the new gates
        existing = {"gates": {"auto_merge": True, "security_review": True}}
        config_path.write_text(json.dumps(existing))

        cp = ControlPlane(config_path=config_path)
        cp.load()
        # New gates injected with correct defaults
        assert cp.gate_enabled("phased_orchestration") is False
        assert cp.gate_enabled("phased_code_review") is True  # PR-c merged — default is now True
        # Existing gates preserved
        assert cp.gate_enabled("auto_merge") is True
