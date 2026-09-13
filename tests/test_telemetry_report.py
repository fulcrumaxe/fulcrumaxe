"""Tests for the opt-in telemetry report — PR-a foundation (D#2565).

Covers AC-1 (gate default/backfill), AC-2 (Python level-0 network guard),
AC-4/AC-5/AC-6 (install id shape, location, uniqueness, never-minted-on-read),
AC-8's redaction half (literal-value redaction, not shape-based), and AC-16
(disclosure at opt-in never prints the id).

AC-3 (the bash level-0 guard) and AC-7's bash half live in
tests/test_telemetry_level0_bash.sh instead — a pytest socket guard proves
nothing about scripts/lib/telemetry.sh, which is a separate process.

Every test that touches backend.state_paths.STATE_DIR points
AUTONOMOUS_TEAM_STATE_DIR at a scratch dir first (tmp_path), per CLAUDE.md —
never the production state directory.
"""
from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_plane import ControlPlane, _DEFAULT_GATES  # noqa: E402
from backend import telemetry_install_id, telemetry_report  # noqa: E402
from hooks.spawn_tag_redaction import redact_literal  # noqa: E402


# ---------------------------------------------------------------------------
# AC-1 — boolean gate, default False, backfilled via setdefault
# ---------------------------------------------------------------------------


class TestTelemetryReportGate:
    def test_registered_in_defaults(self):
        assert "telemetry_report" in _DEFAULT_GATES

    def test_default_is_false(self):
        assert _DEFAULT_GATES["telemetry_report"] is False

    def test_backfilled_into_existing_gates_block(self, tmp_path):
        # A config file with a `gates` block that has never seen this key —
        # exercises the setdefault backfill at control_plane.py:326, not the
        # from-scratch default path.
        config_path = tmp_path / "config.json"
        config_path.write_text(
            '{"gates": {"auto_merge": true}}\n', encoding="utf-8"
        )
        cp = ControlPlane(config_path=config_path)
        cp.load()
        assert cp.gate_enabled("telemetry_report") is False

    def test_fresh_control_plane_gate_off(self, tmp_path):
        cp = ControlPlane(config_path=tmp_path / "config.json")
        cp.load()
        assert cp.gate_enabled("telemetry_report") is False


# ---------------------------------------------------------------------------
# AC-2 — Python level-0 network guard
# ---------------------------------------------------------------------------


def _raise(*_args, **_kwargs):
    raise AssertionError("network touched while gates.telemetry_report is False")


class TestLevel0GuardPython:
    def test_maybe_send_no_socket_when_gate_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setattr(socket, "socket", _raise)
        monkeypatch.setattr(socket, "getaddrinfo", _raise)

        # Must return cleanly — neither patch fires.
        telemetry_report.maybe_send()

    def test_maybe_send_raises_not_implemented_when_gate_on(self, monkeypatch, tmp_path):
        # Gate-on path is PR-b's to build; PR-a proves it's not silently a
        # no-op (a bare `return` here would be indistinguishable from the
        # gate-off case and mask a regression PR-b needs to actually wire).
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(config_path))
        cp = ControlPlane(config_path=config_path)
        cp.load()
        cp.set("gates.telemetry_report", True)

        with pytest.raises(NotImplementedError):
            telemetry_report.maybe_send()


# ---------------------------------------------------------------------------
# AC-4, AC-5, AC-6 — install id shape, location, uniqueness, pure read
# ---------------------------------------------------------------------------


class TestInstallId:
    def test_generated_id_shape_mode_and_location(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        new_id = telemetry_install_id.ensure_id()

        assert re.fullmatch(r"[0-9a-f]{32}", new_id)

        id_path = tmp_path / telemetry_install_id.ID_FILENAME
        assert id_path.is_file()
        assert (id_path.stat().st_mode & 0o777) == 0o600
        assert id_path.parent == tmp_path  # under backend.state_paths.STATE_DIR

    def test_no_in_repo_path_can_reach_it(self):
        # The id's own filename never appears as a tracked or untracked
        # in-repo path — nothing points at it, unlike audit.jsonl/state.db.
        repo_root = Path(__file__).resolve().parent.parent
        candidate = repo_root / ".autonomous-team" / telemetry_install_id.ID_FILENAME
        assert not candidate.exists()

    def test_two_installs_from_one_clone_differ(self, monkeypatch, tmp_path):
        dir_a = tmp_path / "install-a"
        dir_b = tmp_path / "install-b"
        dir_a.mkdir()
        dir_b.mkdir()

        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(dir_a))
        id_a = telemetry_install_id.ensure_id()

        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(dir_b))
        id_b = telemetry_install_id.ensure_id()

        assert id_a != id_b

    def test_never_minted_on_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        id_path = tmp_path / telemetry_install_id.ID_FILENAME

        for _ in range(5):
            assert telemetry_install_id.read_id() is None

        assert not id_path.exists()

    def test_read_returns_existing_id_without_mutating(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        minted = telemetry_install_id.ensure_id()

        for _ in range(3):
            assert telemetry_install_id.read_id() == minted

    def test_ensure_id_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        first = telemetry_install_id.ensure_id()
        second = telemetry_install_id.ensure_id()
        assert first == second


# ---------------------------------------------------------------------------
# AC-8 (redaction half) — literal-value redaction, not shape-based
# ---------------------------------------------------------------------------


class TestInstallIdRedaction:
    def test_redacts_id_but_not_unrelated_md5(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        real_id = telemetry_install_id.ensure_id()
        unrelated_md5 = "9e107d9d372bb6826bd81d3542a419d6"  # 32 hex chars, not the id

        fixture = f"install={real_id} other_hash={unrelated_md5}"
        redacted = redact_literal(fixture, real_id)

        assert real_id not in redacted
        assert unrelated_md5 in redacted

    def test_no_secret_is_a_no_op(self):
        text = "nothing sensitive here"
        assert redact_literal(text, None) == text
        assert redact_literal(text, "") == text

    def test_non_str_input_is_coerced(self):
        assert isinstance(redact_literal(12345, None), str)


# ---------------------------------------------------------------------------
# AC-16 — disclosure at opt-in never prints the id
# ---------------------------------------------------------------------------


class TestDisclosureAtOptIn:
    def test_opt_in_flips_gate_and_prints_disclosure_without_id(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(config_path))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))

        disclosure = telemetry_install_id.opt_in()

        cp = ControlPlane(config_path=config_path)
        cp.load()
        assert cp.gate_enabled("telemetry_report") is True

        for field in telemetry_install_id.DISCLOSED_FIELDS:
            assert field in disclosure
        assert telemetry_install_id.DISCLOSURE_URL in disclosure
        assert telemetry_install_id.ERASE_COMMAND in disclosure

        minted_id = telemetry_install_id.read_id()
        assert minted_id is not None
        assert minted_id not in disclosure

    def test_opt_in_mints_id_if_missing(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(config_path))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))

        assert telemetry_install_id.read_id() is None
        telemetry_install_id.opt_in()
        assert telemetry_install_id.read_id() is not None
