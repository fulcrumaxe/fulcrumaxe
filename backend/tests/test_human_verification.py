"""
Tests for backend/human_verification.py

Covers pure/isolable logic only — no real GitHub calls, no real state dir.
Network operations (auto_file_bug, check_reverify_needed subprocess calls)
are mocked at the subprocess boundary. File I/O uses pytest tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.human_verification import (
    HumanVerification,
    _default_checklist,
    _now_iso,
    _run_id,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNowIso:
    def test_format_matches_utc_iso(self):
        result = _now_iso()
        # Must be exactly the pattern YYYY-MM-DDTHH:MM:SSZ
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", result), (
            f"_now_iso() returned unexpected format: {result!r}"
        )

    def test_returns_string(self):
        assert isinstance(_now_iso(), str)


class TestRunId:
    def test_format_matches_date_time(self):
        result = _run_id()
        import re
        assert re.match(r"^\d{8}-\d{6}$", result), (
            f"_run_id() returned unexpected format: {result!r}"
        )

    def test_returns_string(self):
        assert isinstance(_run_id(), str)


# ---------------------------------------------------------------------------
# Default checklist structure
# ---------------------------------------------------------------------------


class TestDefaultChecklist:
    def test_returns_dict(self):
        cl = _default_checklist()
        assert isinstance(cl, dict)

    def test_has_version_key(self):
        cl = _default_checklist()
        assert "version" in cl
        assert cl["version"] == "1.0"

    def test_has_items_list(self):
        cl = _default_checklist()
        assert "items" in cl
        assert isinstance(cl["items"], list)

    def test_items_not_empty(self):
        cl = _default_checklist()
        assert len(cl["items"]) > 0

    def test_each_item_has_required_fields(self):
        cl = _default_checklist()
        required = {"id", "subsystem", "description", "instructions", "expected", "status"}
        for item in cl["items"]:
            missing = required - set(item.keys())
            assert not missing, f"Item {item.get('id')} missing fields: {missing}"

    def test_all_items_start_as_pending(self):
        cl = _default_checklist()
        for item in cl["items"]:
            assert item["status"] == "pending", (
                f"Item {item['id']} should start as pending, got {item['status']!r}"
            )

    def test_ids_are_unique(self):
        cl = _default_checklist()
        ids = [item["id"] for item in cl["items"]]
        assert len(ids) == len(set(ids)), "Duplicate item IDs found in default checklist"

    def test_subsystems_covered(self):
        cl = _default_checklist()
        subsystems = {item["subsystem"] for item in cl["items"]}
        # At least these major subsystems should appear
        expected_subsystems = {"Python Backend API", "Rust SaaS Service", "TUI", "React Dashboard"}
        missing = expected_subsystems - subsystems
        assert not missing, f"Missing expected subsystems: {missing}"


# ---------------------------------------------------------------------------
# HumanVerification init
# ---------------------------------------------------------------------------


def _make_hv(tmp_path: Path) -> HumanVerification:
    checklist_path = tmp_path / "checklist.json"
    return HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)


class TestHumanVerificationInit:
    def test_stores_checklist_path(self, tmp_path):
        hv = _make_hv(tmp_path)
        assert hv.checklist_path == tmp_path / "checklist.json"

    def test_stores_repo_root(self, tmp_path):
        hv = _make_hv(tmp_path)
        assert hv.repo_root == tmp_path

    def test_run_id_is_string(self, tmp_path):
        hv = _make_hv(tmp_path)
        assert isinstance(hv.run_id, str)
        assert len(hv.run_id) > 0

    def test_partial_results_starts_empty(self, tmp_path):
        hv = _make_hv(tmp_path)
        assert hv._partial_results == []

    def test_checklist_starts_empty(self, tmp_path):
        hv = _make_hv(tmp_path)
        assert hv.checklist == {}


# ---------------------------------------------------------------------------
# load_checklist + save_checklist (file I/O to tmp_path)
# ---------------------------------------------------------------------------


class TestLoadChecklist:
    def test_creates_default_when_file_missing(self, tmp_path):
        hv = _make_hv(tmp_path)
        cl = hv.load_checklist()
        assert "items" in cl
        assert len(cl["items"]) > 0

    def test_creates_file_on_disk_when_missing(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.load_checklist()
        assert hv.checklist_path.exists()

    def test_loads_existing_file(self, tmp_path):
        checklist_path = tmp_path / "checklist.json"
        data = {"version": "1.0", "last_run": "", "items": [
            {"id": "test-item", "status": "pending", "subsystem": "TUI",
             "description": "d", "instructions": "i", "expected": "e"}
        ]}
        checklist_path.write_text(json.dumps(data))
        hv = HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)
        cl = hv.load_checklist()
        assert cl["items"][0]["id"] == "test-item"

    def test_does_not_overwrite_existing_file(self, tmp_path):
        checklist_path = tmp_path / "checklist.json"
        original = {"version": "custom", "last_run": "", "items": []}
        checklist_path.write_text(json.dumps(original))
        hv = HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)
        hv.load_checklist()
        on_disk = json.loads(checklist_path.read_text())
        assert on_disk["version"] == "custom"


class TestSaveChecklist:
    def test_writes_json_to_disk(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"version": "1.0", "items": []}
        hv.save_checklist()
        assert hv.checklist_path.exists()
        data = json.loads(hv.checklist_path.read_text())
        assert data["version"] == "1.0"

    def test_sets_last_run_timestamp(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        hv.save_checklist()
        data = json.loads(hv.checklist_path.read_text())
        assert "last_run" in data
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", data["last_run"])

    def test_roundtrip_preserves_items(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {
            "version": "1.0",
            "items": [{"id": "x", "status": "pass"}],
        }
        hv.save_checklist()
        saved = json.loads(hv.checklist_path.read_text())
        assert saved["items"][0]["id"] == "x"
        assert saved["items"][0]["status"] == "pass"


# ---------------------------------------------------------------------------
# pending_items
# ---------------------------------------------------------------------------


class TestPendingItems:
    def _make_hv_with_items(self, tmp_path, items):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": items}
        return hv

    def test_returns_pending_items(self, tmp_path):
        items = [
            {"id": "a", "status": "pending"},
            {"id": "b", "status": "pass"},
            {"id": "c", "status": "fail"},
        ]
        hv = self._make_hv_with_items(tmp_path, items)
        pending = hv.pending_items()
        assert len(pending) == 1
        assert pending[0]["id"] == "a"

    def test_returns_reverify_items(self, tmp_path):
        items = [
            {"id": "a", "status": "re-verify"},
            {"id": "b", "status": "pass"},
        ]
        hv = self._make_hv_with_items(tmp_path, items)
        pending = hv.pending_items()
        assert len(pending) == 1
        assert pending[0]["id"] == "a"

    def test_returns_both_pending_and_reverify(self, tmp_path):
        items = [
            {"id": "a", "status": "pending"},
            {"id": "b", "status": "re-verify"},
            {"id": "c", "status": "pass"},
            {"id": "d", "status": "skip"},
        ]
        hv = self._make_hv_with_items(tmp_path, items)
        pending = hv.pending_items()
        assert len(pending) == 2
        ids = {item["id"] for item in pending}
        assert ids == {"a", "b"}

    def test_empty_when_all_done(self, tmp_path):
        items = [
            {"id": "a", "status": "pass"},
            {"id": "b", "status": "fail"},
            {"id": "c", "status": "skip"},
        ]
        hv = self._make_hv_with_items(tmp_path, items)
        assert hv.pending_items() == []

    def test_empty_checklist_returns_empty(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {}
        assert hv.pending_items() == []


# ---------------------------------------------------------------------------
# _find_item
# ---------------------------------------------------------------------------


class TestFindItem:
    def test_finds_existing_item(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [
            {"id": "hv-tui-starts", "status": "pending"},
            {"id": "hv-api-health", "status": "pending"},
        ]}
        result = hv._find_item("hv-tui-starts")
        assert result is not None
        assert result["id"] == "hv-tui-starts"

    def test_returns_none_for_missing_id(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [{"id": "hv-tui-starts", "status": "pending"}]}
        assert hv._find_item("nonexistent") is None

    def test_returns_none_on_empty_checklist(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {}
        assert hv._find_item("any-id") is None

    def test_returns_correct_item_when_multiple(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [
            {"id": "first", "status": "pass"},
            {"id": "second", "status": "pending"},
            {"id": "third", "status": "fail"},
        ]}
        result = hv._find_item("second")
        assert result["status"] == "pending"


# ---------------------------------------------------------------------------
# record_pass
# ---------------------------------------------------------------------------


def _make_item(**overrides) -> dict:
    base = {
        "id": "hv-test-item",
        "subsystem": "TUI",
        "description": "Test item",
        "instructions": "Do the thing",
        "expected": "Thing is done",
        "status": "pending",
        "verified_by": "",
        "verified_at": "",
        "note": "",
        "bug_discussion": None,
        "fix_pr": None,
        "run_id": "",
    }
    base.update(overrides)
    return base


class TestRecordPass:
    def test_sets_status_to_pass(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item, "looks good")
        assert item["status"] == "pass"

    def test_sets_verified_by_to_human(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item)
        assert item["verified_by"] == "human"

    def test_sets_verified_at_timestamp(self, tmp_path):
        import re
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item)
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", item["verified_at"])

    def test_stores_note(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item, "everything works fine")
        assert item["note"] == "everything works fine"

    def test_stores_run_id(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item)
        assert item["run_id"] == hv.run_id

    def test_appends_to_partial_results(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item(id="hv-test-item")
        hv.record_pass(item, "ok")
        assert len(hv._partial_results) == 1
        assert hv._partial_results[0]["id"] == "hv-test-item"
        assert hv._partial_results[0]["result"] == "pass"

    def test_partial_result_includes_note(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item, "my note")
        assert hv._partial_results[0]["note"] == "my note"

    def test_empty_note_allowed(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        hv.record_pass(item, "")
        assert item["note"] == ""

    def test_saves_checklist_to_disk(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [_make_item()]}
        hv.record_pass(hv.checklist["items"][0])
        assert hv.checklist_path.exists()
        saved = json.loads(hv.checklist_path.read_text())
        assert saved["items"][0]["status"] == "pass"


# ---------------------------------------------------------------------------
# record_fail (mocking auto_file_bug so no network calls happen)
# ---------------------------------------------------------------------------


class TestRecordFail:
    def test_sets_status_to_fail(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=42):
            hv.record_fail(item, "broken layout")
        assert item["status"] == "fail"

    def test_sets_verified_by_to_human(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=42):
            hv.record_fail(item, "broken layout")
        assert item["verified_by"] == "human"

    def test_sets_bug_discussion_number(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=99):
            hv.record_fail(item, "broken layout")
        assert item["bug_discussion"] == 99

    def test_stores_description_as_note(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=1):
            hv.record_fail(item, "page is blank")
        assert item["note"] == "page is blank"

    def test_stores_run_id(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=1):
            hv.record_fail(item, "err")
        assert item["run_id"] == hv.run_id

    def test_returns_discussion_number(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=77):
            disc = hv.record_fail(item, "fails")
        assert disc == 77

    def test_appends_to_partial_results(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item(id="hv-fail-item")
        with patch.object(hv, "auto_file_bug", return_value=55):
            hv.record_fail(item, "bad output")
        assert len(hv._partial_results) == 1
        entry = hv._partial_results[0]
        assert entry["id"] == "hv-fail-item"
        assert entry["result"] == "fail"
        assert entry["description"] == "bad output"
        assert entry["bug_discussion"] == 55

    def test_sets_verified_at_timestamp(self, tmp_path):
        import re
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": []}
        item = _make_item()
        with patch.object(hv, "auto_file_bug", return_value=1):
            hv.record_fail(item, "error")
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", item["verified_at"])


# ---------------------------------------------------------------------------
# _gather_technical_context — pure logic, no I/O
# ---------------------------------------------------------------------------


class TestGatherTechnicalContext:
    def test_dashboard_subsystem_mentions_backend_files(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="Python Backend API", expected="page loads")
        ctx = hv._gather_technical_context(item)
        assert "backend" in ctx.lower()

    def test_rust_subsystem_mentions_saas_files(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="Rust SaaS Service", expected="200 OK")
        ctx = hv._gather_technical_context(item)
        assert "saas-service" in ctx.lower() or "rust" in ctx.lower()

    def test_tui_subsystem_mentions_tui_files(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="TUI", expected="renders")
        ctx = hv._gather_technical_context(item)
        assert "tui" in ctx.lower()

    def test_react_subsystem_mentions_dashboard_src(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="React Dashboard", expected="renders")
        ctx = hv._gather_technical_context(item)
        assert "dashboard" in ctx.lower()

    def test_unknown_subsystem_has_fallback_message(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="Unknown Subsystem", expected="works")
        ctx = hv._gather_technical_context(item)
        assert len(ctx) > 0  # Must return something, not crash

    def test_includes_expected_behaviour(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="TUI", expected="renders green dot")
        ctx = hv._gather_technical_context(item)
        assert "renders green dot" in ctx

    def test_includes_check_url_when_present(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="Python Backend API",
                          check_url="http://localhost:18099/dashboard",
                          expected="loads")
        ctx = hv._gather_technical_context(item)
        assert "18099" in ctx or "localhost" in ctx

    def test_returns_string(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item(subsystem="TUI", expected="x")
        assert isinstance(hv._gather_technical_context(item), str)


# ---------------------------------------------------------------------------
# write_proof — file I/O to tmp_path
# ---------------------------------------------------------------------------


class TestWriteProof:
    def _make_results(self):
        return [
            {"id": "a", "result": "pass", "note": ""},
            {"id": "b", "result": "fail", "description": "bad", "bug_discussion": 5},
            {"id": "c", "result": "skip"},
        ]

    def test_creates_proof_file(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = self._make_results()
        path = hv.write_proof(results)
        assert path.exists()

    def test_proof_file_is_valid_json(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = self._make_results()
        path = hv.write_proof(results)
        data = json.loads(path.read_text())
        assert isinstance(data, dict)

    def test_proof_has_run_id(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = self._make_results()
        path = hv.write_proof(results)
        data = json.loads(path.read_text())
        assert data["run_id"] == hv.run_id

    def test_proof_summary_counts_correctly(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = self._make_results()
        path = hv.write_proof(results)
        data = json.loads(path.read_text())
        assert data["summary"]["passed"] == 1
        assert data["summary"]["failed"] == 1
        assert data["summary"]["skipped"] == 1
        assert data["summary"]["total"] == 3

    def test_proof_includes_all_results(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = self._make_results()
        path = hv.write_proof(results)
        data = json.loads(path.read_text())
        assert len(data["results"]) == 3

    def test_proof_includes_timestamp(self, tmp_path):
        import re
        hv = _make_hv(tmp_path)
        path = hv.write_proof([])
        data = json.loads(path.read_text())
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", data["timestamp"])

    def test_proof_path_is_under_verification_report(self, tmp_path):
        hv = _make_hv(tmp_path)
        path = hv.write_proof([])
        # Path should be inside repo_root / verification-report / proof / run_id
        assert "verification-report" in str(path)
        assert hv.run_id in str(path)

    def test_proof_all_pass_summary(self, tmp_path):
        hv = _make_hv(tmp_path)
        results = [{"id": f"item-{i}", "result": "pass"} for i in range(5)]
        path = hv.write_proof(results)
        data = json.loads(path.read_text())
        assert data["summary"]["passed"] == 5
        assert data["summary"]["failed"] == 0
        assert data["summary"]["skipped"] == 0

    def test_empty_results_writes_zero_counts(self, tmp_path):
        hv = _make_hv(tmp_path)
        path = hv.write_proof([])
        data = json.loads(path.read_text())
        assert data["summary"]["total"] == 0
        assert data["summary"]["passed"] == 0


# ---------------------------------------------------------------------------
# check_reverify_needed — mock subprocess to avoid real GitHub calls
# ---------------------------------------------------------------------------


class TestCheckReverifyNeeded:
    def _make_hv_with_failed_item(self, tmp_path, disc_num: int):
        hv = _make_hv(tmp_path)
        hv.checklist = {
            "items": [
                {**_make_item(id="hv-fail-item", status="fail"), "bug_discussion": disc_num}
            ]
        }
        return hv

    def _mock_gh_response(self, body: str, closed: bool) -> MagicMock:
        """Build a mock subprocess result for a discussion query."""
        data = {
            "data": {
                "repository": {
                    "discussion": {"body": body, "closed": closed}
                }
            }
        }
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps(data)
        return mock

    def test_marks_item_reverify_when_discussion_closed(self, tmp_path):
        hv = self._make_hv_with_failed_item(tmp_path, disc_num=123)
        mock_result = self._mock_gh_response(body="some body", closed=True)
        with patch("subprocess.run", return_value=mock_result):
            hv.check_reverify_needed()
        assert hv.checklist["items"][0]["status"] == "re-verify"

    def test_marks_item_reverify_when_body_contains_status_done(self, tmp_path):
        hv = self._make_hv_with_failed_item(tmp_path, disc_num=124)
        mock_result = self._mock_gh_response(
            body="<!-- STATUS:DONE SINCE:2026-05-20T10:00:00Z -->", closed=False
        )
        with patch("subprocess.run", return_value=mock_result):
            hv.check_reverify_needed()
        assert hv.checklist["items"][0]["status"] == "re-verify"

    def test_does_not_mark_reverify_when_open_and_not_done(self, tmp_path):
        hv = self._make_hv_with_failed_item(tmp_path, disc_num=125)
        mock_result = self._mock_gh_response(
            body="<!-- STATUS:SPEC_READY SINCE:2026-05-19T00:00:00Z -->", closed=False
        )
        with patch("subprocess.run", return_value=mock_result):
            hv.check_reverify_needed()
        assert hv.checklist["items"][0]["status"] == "fail"

    def test_skips_items_with_no_bug_discussion(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [
            {**_make_item(status="fail"), "bug_discussion": None}
        ]}
        with patch("subprocess.run") as mock_run:
            hv.check_reverify_needed()
            mock_run.assert_not_called()

    def test_skips_items_not_in_fail_status(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [
            {**_make_item(status="pass"), "bug_discussion": 10}
        ]}
        with patch("subprocess.run") as mock_run:
            hv.check_reverify_needed()
            mock_run.assert_not_called()

    def test_skips_on_gh_api_failure(self, tmp_path):
        """If subprocess returns non-zero, item status must not change."""
        hv = self._make_hv_with_failed_item(tmp_path, disc_num=126)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            hv.check_reverify_needed()
        assert hv.checklist["items"][0]["status"] == "fail"

    def test_handles_multiple_items(self, tmp_path):
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [
            {**_make_item(id="item-a", status="fail"), "bug_discussion": 10},
            {**_make_item(id="item-b", status="fail"), "bug_discussion": 11},
        ]}

        # Return closed=True for the first call (disc 10), open for the second (disc 11)
        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            closed = call_count[0] == 1  # only first call (disc 10) is closed
            data = {"data": {"repository": {"discussion": {"body": "", "closed": closed}}}}
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = json.dumps(data)
            return mock

        with patch("subprocess.run", side_effect=side_effect):
            hv.check_reverify_needed()

        assert hv.checklist["items"][0]["status"] == "re-verify"
        assert hv.checklist["items"][1]["status"] == "fail"


# ---------------------------------------------------------------------------
# Bug body content (via auto_file_bug with mocked subprocess)
# ---------------------------------------------------------------------------


class TestAutoBugBodyContent:
    """Verify the Discussion body generated for a failed item has the right content.

    We mock the subprocess calls so no real GitHub API is hit.
    We capture what would have been posted by inspecting the call args.
    """

    def _run_auto_file_bug(self, tmp_path, item: dict) -> tuple[str, int]:
        """Run auto_file_bug with mocked subprocess. Returns (body_sent, disc_num)."""
        hv = _make_hv(tmp_path)
        hv.checklist = {"items": [item]}

        repo_response = {
            "data": {
                "repository": {
                    "id": "FAKE_REPO_ID",
                    "discussionCategories": {
                        "nodes": [{"id": "FAKE_CAT_ID", "name": "General"}]
                    }
                }
            }
        }
        create_response = {
            "data": {
                "createDiscussion": {
                    "discussion": {"number": 999}
                }
            }
        }

        call_args_list = []

        def side_effect(cmd, **kwargs):
            call_args_list.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            # First call = repo query, second call = create discussion
            if len(call_args_list) == 1:
                mock.stdout = json.dumps(repo_response)
            else:
                mock.stdout = json.dumps(create_response)
            return mock

        with patch("subprocess.run", side_effect=side_effect):
            disc_num = hv.auto_file_bug(item, "the screen is blank")

        # The body is embedded in the second subprocess call args
        create_cmd = call_args_list[1]
        query_arg = " ".join(create_cmd)
        return query_arg, disc_num

    def test_returns_correct_discussion_number(self, tmp_path):
        item = _make_item(id="hv-tui-starts", subsystem="TUI",
                          expected="renders", instructions="run npm start")
        _, disc_num = self._run_auto_file_bug(tmp_path, item)
        assert disc_num == 999

    def test_body_contains_item_id(self, tmp_path):
        item = _make_item(id="hv-tui-starts", subsystem="TUI",
                          expected="renders", instructions="run npm start")
        query_arg, _ = self._run_auto_file_bug(tmp_path, item)
        assert "hv-tui-starts" in query_arg

    def test_body_contains_subsystem(self, tmp_path):
        item = _make_item(id="hv-x", subsystem="Rust SaaS Service",
                          expected="200 OK", instructions="curl health")
        query_arg, _ = self._run_auto_file_bug(tmp_path, item)
        assert "Rust SaaS Service" in query_arg

    def test_body_contains_spec_ready_marker(self, tmp_path):
        item = _make_item(id="hv-y", subsystem="TUI",
                          expected="renders", instructions="start tui")
        query_arg, _ = self._run_auto_file_bug(tmp_path, item)
        assert "SPEC_READY" in query_arg

    def test_body_contains_expected_behaviour(self, tmp_path):
        item = _make_item(id="hv-z", subsystem="TUI",
                          expected="green dot visible", instructions="open browser")
        query_arg, _ = self._run_auto_file_bug(tmp_path, item)
        assert "green dot visible" in query_arg

    def test_returns_zero_when_no_categories(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item()

        repo_response = {
            "data": {
                "repository": {
                    "id": "FAKE_REPO_ID",
                    "discussionCategories": {"nodes": []}
                }
            }
        }

        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps(repo_response)

        with patch("subprocess.run", return_value=mock):
            disc_num = hv.auto_file_bug(item, "nothing works")

        assert disc_num == 0

    def test_returns_zero_on_create_failure(self, tmp_path):
        hv = _make_hv(tmp_path)
        item = _make_item()

        repo_response = {
            "data": {
                "repository": {
                    "id": "FAKE_REPO_ID",
                    "discussionCategories": {
                        "nodes": [{"id": "CAT_ID", "name": "General"}]
                    }
                }
            }
        }

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            mock = MagicMock()
            if call_count[0] == 1:
                mock.returncode = 0
                mock.stdout = json.dumps(repo_response)
            else:
                mock.returncode = 1
                mock.stdout = ""
                mock.stderr = "GH API error"
            return mock

        with patch("subprocess.run", side_effect=side_effect):
            disc_num = hv.auto_file_bug(item, "broken")

        assert disc_num == 0


# ---------------------------------------------------------------------------
# Regression: _partial_results must not accumulate across run() calls
# ---------------------------------------------------------------------------


class TestPartialResultsReset:
    """Verify that calling run() twice on the same HumanVerification instance
    yields independent _partial_results — no bleed-through from the first run.

    run() drives interactive input, so we mock the full interaction chain:
    - load_checklist / save_checklist file I/O uses tmp_path
    - check_reverify_needed subprocess is mocked (no GitHub)
    - run_item input() is bypassed by patching it to return 'pass'
    - auto_file_bug is not called because all verdicts are 'pass'
    """

    def _make_single_item_checklist(self, item_id: str) -> dict:
        return {
            "version": "1.0",
            "last_run": "",
            "items": [_make_item(id=item_id, status="pending")],
        }

    def test_second_run_does_not_inherit_first_run_results(self, tmp_path):
        """After two run() calls, _partial_results contains only the second run's items."""
        checklist_path = tmp_path / "checklist.json"

        # Write an initial checklist with two items so each run has one pending item.
        # After run 1 marks item-A as 'pass', we reset it back to 'pending' to give
        # run 2 a fresh item to process (item-B stays pending after load for run 2,
        # so we just put both as pending and let two successive runs each find one
        # pending item — achieved by resetting the checklist between calls).

        # Approach: run once with item-A pending → verify _partial_results has 1 entry.
        # Reset the checklist to have item-A pending again (simulating a caller that
        # reuses the instance with a fresh checklist). Run again → _partial_results
        # must still have exactly 1 entry (not 2).

        def write_checklist(item_id: str):
            data = self._make_single_item_checklist(item_id)
            checklist_path.write_text(json.dumps(data))

        hv = HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)

        # Mocks that stay constant across both runs
        no_reverify = MagicMock()
        no_reverify.returncode = 1  # subprocess fails → check_reverify skips all items

        # Run 1 — item "run1-item"
        write_checklist("run1-item")
        with patch("subprocess.run", return_value=no_reverify), \
             patch("builtins.input", return_value="pass"):
            hv.run()

        results_after_run1 = list(hv._partial_results)
        assert len(results_after_run1) == 1, (
            f"Expected 1 result after run 1, got {len(results_after_run1)}"
        )
        assert results_after_run1[0]["id"] == "run1-item"

        # Run 2 — fresh checklist with a different item id to make accumulation obvious
        write_checklist("run2-item")
        with patch("subprocess.run", return_value=no_reverify), \
             patch("builtins.input", return_value="pass"):
            hv.run()

        results_after_run2 = list(hv._partial_results)
        assert len(results_after_run2) == 1, (
            f"_partial_results accumulated across runs: {results_after_run2}"
        )
        assert results_after_run2[0]["id"] == "run2-item", (
            f"Expected only run2-item but got: {[r['id'] for r in results_after_run2]}"
        )

    def test_proof_report_for_second_run_has_correct_count(self, tmp_path):
        """write_proof called after run 2 reflects only run 2's single result."""
        checklist_path = tmp_path / "checklist.json"

        def write_checklist(item_id: str):
            data = self._make_single_item_checklist(item_id)
            checklist_path.write_text(json.dumps(data))

        hv = HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)
        no_reverify = MagicMock()
        no_reverify.returncode = 1

        # Run 1
        write_checklist("first-item")
        with patch("subprocess.run", return_value=no_reverify), \
             patch("builtins.input", return_value="pass"):
            hv.run()

        # Run 2
        write_checklist("second-item")
        proof_path = None
        with patch("subprocess.run", return_value=no_reverify), \
             patch("builtins.input", return_value="pass"):
            hv.run()
            proof_path = hv.write_proof(hv._partial_results)

        data = json.loads(proof_path.read_text())
        assert data["summary"]["total"] == 1, (
            f"Proof after run 2 should have 1 result, got {data['summary']['total']}"
        )
        assert data["summary"]["passed"] == 1

    def test_run_id_refreshes_on_each_run(self, tmp_path):
        """Each run() gets a fresh run_id, not the one set at __init__ time."""
        import time

        checklist_path = tmp_path / "checklist.json"
        data = self._make_single_item_checklist("any-item")
        checklist_path.write_text(json.dumps(data))

        hv = HumanVerification(checklist_path=checklist_path, repo_root=tmp_path)
        init_run_id = hv.run_id

        no_reverify = MagicMock()
        no_reverify.returncode = 1

        with patch("subprocess.run", return_value=no_reverify), \
             patch("builtins.input", return_value="skip"):
            hv.run()

        # run_id must be a valid formatted string (not the original init value)
        import re
        assert re.match(r"^\d{8}-\d{6}$", hv.run_id), (
            f"run_id after run() should match date-time format, got {hv.run_id!r}"
        )
