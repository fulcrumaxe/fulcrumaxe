"""Tests for scripts/spec-context-oracle.py

Fixture-based: synthetic Discussion body with known file/symbol refs;
mock audit/duckdb/git outputs; assert comment shape.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Import oracle module via importlib so we can reload with patched state dir
# ---------------------------------------------------------------------------

def _load_oracle(tmp_path: Path, monkeypatch):
    """Load oracle module with isolated state dir."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    import backend.state_paths as sp
    importlib.reload(sp)
    # Load oracle after patching state paths
    spec = importlib.util.spec_from_file_location(
        "spec_context_oracle",
        str(_REPO_ROOT / "scripts" / "spec-context-oracle.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch the module's STATE_DIR and AUDIT_LOG before exec
    spec.loader.exec_module(mod)
    mod.STATE_DIR = tmp_path
    mod.AUDIT_LOG = tmp_path / "audit.jsonl"
    mod.RETROS_FILE = tmp_path / "agent-retros.jsonl"
    # Reload state_paths to original
    importlib.reload(sp)
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BODY = """
<!-- STATUS:SPEC_READY SINCE:2026-05-19T00:00:00Z -->

This spec references `backend/spec_oracle.py` and `scripts/preflight.sh`.

See also D#1122 and PR #954 for prior context.

The `for_project()` function is called in `gather_file_context`.

Fix the `parse_references` symbol parsing.
"""


@pytest.fixture()
def oracle(tmp_path, monkeypatch):
    return _load_oracle(tmp_path, monkeypatch)


@pytest.fixture()
def tmp_audit(tmp_path):
    """Create a minimal audit.jsonl in tmp_path."""
    audit_path = tmp_path / "audit.jsonl"
    events = [
        {"ts": "2026-05-15T10:00:00Z", "source": "blackboard", "action": "write",
         "key": "backend/spec_oracle.py", "actor": "executor", "seq": 1},
        {"ts": "2026-05-16T11:00:00Z", "source": "blackboard", "action": "write",
         "key": "scripts/preflight.sh", "actor": "project-manager", "seq": 2},
    ]
    with audit_path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return audit_path


@pytest.fixture()
def tmp_retros(tmp_path):
    """Create a minimal agent-retros.jsonl in tmp_path."""
    retros_path = tmp_path / "agent-retros.jsonl"
    # Relative to now, not fixed dates. gather_classifier_signals() only counts
    # the last 30 days, so the original 2026-05 literals silently aged out of
    # the window and the test started asserting against an empty dict.
    now = datetime.now(timezone.utc)

    def _ago(days: int) -> str:
        return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    entries = [
        {"ts": _ago(2), "agent_id": "x", "role": "executor",
         "classifier": "cosmetic_retry", "trigger": "test", "why": "test",
         "future_fix": "test", "work_corrected": False, "shadow_mode": True, "turn_idx": 1},
        {"ts": _ago(3), "agent_id": "y", "role": "code-reviewer",
         "classifier": "wrong_premise_retry", "trigger": "test", "why": "test",
         "future_fix": "test", "work_corrected": True, "shadow_mode": False, "turn_idx": 2},
        {"ts": _ago(1), "agent_id": "z", "role": "executor",
         "classifier": "cosmetic_retry", "trigger": "test", "why": "test",
         "future_fix": "test", "work_corrected": False, "shadow_mode": True, "turn_idx": 3},
    ]
    with retros_path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return retros_path


# ---------------------------------------------------------------------------
# parse_references tests
# ---------------------------------------------------------------------------

class TestParseReferences:
    def test_parses_file_paths(self, oracle):
        result = oracle.parse_references(SAMPLE_BODY)
        assert "backend/spec_oracle.py" in result["files"]
        assert "scripts/preflight.sh" in result["files"]

    def test_parses_discussion_refs(self, oracle):
        result = oracle.parse_references(SAMPLE_BODY)
        assert 1122 in result["dnums"]

    def test_parses_pr_refs(self, oracle):
        result = oracle.parse_references(SAMPLE_BODY)
        assert 954 in result["prnums"]

    def test_parses_symbols(self, oracle):
        result = oracle.parse_references(SAMPLE_BODY)
        # Should find for_project, gather_file_context, parse_references
        assert "for_project" in result["symbols"]

    def test_deduplicates_files(self, oracle):
        body = "backend/foo.py backend/foo.py backend/bar.py"
        result = oracle.parse_references(body)
        assert result["files"].count("backend/foo.py") == 1

    def test_excludes_common_words(self, oracle):
        body = "`true` `false` `None` `json` backend/foo.py"
        result = oracle.parse_references(body)
        assert "true" not in result["symbols"]
        assert "false" not in result["symbols"]
        assert "None" not in result["symbols"]


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------

class TestBudget:
    def test_check_does_not_raise_within_budget(self, oracle):
        budget = oracle.Budget(10.0)
        budget.check()  # should not raise

    def test_check_raises_when_expired(self, oracle):
        budget = oracle.Budget(0.0)
        time.sleep(0.01)
        with pytest.raises(oracle.BudgetExceeded):
            budget.check()

    def test_incomplete_set_on_raise(self, oracle):
        budget = oracle.Budget(0.0)
        time.sleep(0.01)
        assert not budget.incomplete
        try:
            budget.check()
        except oracle.BudgetExceeded:
            pass
        assert budget.incomplete

    def test_set_incomplete_manually(self, oracle):
        budget = oracle.Budget(10.0)
        assert not budget.incomplete
        budget.set_incomplete()
        assert budget.incomplete


# ---------------------------------------------------------------------------
# gather_file_context tests
# ---------------------------------------------------------------------------

class TestGatherFileContext:
    def test_returns_file_entries(self, oracle):
        """Mock git log returns valid commits."""
        def fake_run(cmd, timeout, cwd=None):
            if cmd[0] == "git":
                return "abc12345 2026-05-15 fix backend/spec_oracle.py\n", False
            return "", False

        with patch.object(oracle, "_run_with_timeout", side_effect=fake_run):
            budget = oracle.Budget(10.0)
            result = oracle.gather_file_context(["backend/spec_oracle.py"], budget)

        assert len(result) == 1
        assert result[0]["path"] == "backend/spec_oracle.py"
        assert len(result[0]["recent_commits"]) == 1
        assert result[0]["recent_commits"][0]["sha"] == "abc12345"

    def test_handles_git_timeout(self, oracle):
        """When git times out, entry is marked partial."""
        def fake_run(cmd, timeout, cwd=None):
            if cmd[0] == "git":
                return "", True  # timed out
            return "", False

        with patch.object(oracle, "_run_with_timeout", side_effect=fake_run):
            budget = oracle.Budget(10.0)
            result = oracle.gather_file_context(["backend/slow.py"], budget)

        assert result[0]["partial"] is True
        assert budget.incomplete

    def test_also_called_by_populated(self, oracle):
        """also_called_by lists shell script callers."""
        def fake_run(cmd, timeout, cwd=None):
            if cmd[0] == "git":
                return "abc12345 2026-05-15 add module\n", False
            if cmd[0] == "grep":
                return "scripts/preflight.sh\nscripts/loop.sh\n", False
            return "", False

        with patch.object(oracle, "_run_with_timeout", side_effect=fake_run):
            budget = oracle.Budget(10.0)
            result = oracle.gather_file_context(["backend/spec_oracle.py"], budget)

        assert "scripts/preflight.sh" in result[0]["also_called_by"]


# ---------------------------------------------------------------------------
# gather_discussion_context tests
# ---------------------------------------------------------------------------

class TestGatherDiscussionContext:
    def test_parses_status(self, oracle, tmp_path):
        body = "<!-- STATUS:DONE SINCE:2026-05-01Z -->\nSome body."
        with patch("subprocess.run") as mock_run:
            # First call: discussion_cache.py
            first = MagicMock()
            first.stdout = body
            # Second call: gh api graphql for title
            second = MagicMock()
            second.stdout = json.dumps({
                "data": {"repository": {"discussion": {"title": "Test Discussion"}}}
            })
            mock_run.side_effect = [first, second]
            budget = oracle.Budget(10.0)
            result = oracle.gather_discussion_context([1122], budget)

        assert len(result) == 1
        assert result[0]["number"] == 1122
        assert result[0]["status"] == "DONE"
        assert result[0]["title"] == "Test Discussion"


# ---------------------------------------------------------------------------
# gather_classifier_signals tests
# ---------------------------------------------------------------------------

class TestGatherClassifierSignals:
    def test_counts_classifiers(self, oracle, tmp_path, tmp_retros):
        oracle.RETROS_FILE = tmp_retros
        budget = oracle.Budget(10.0)
        result = oracle.gather_classifier_signals([], budget)

        assert result["cosmetic_retry"] == 2
        assert result["wrong_premise_retry"] == 1

    def test_returns_empty_when_no_retros(self, oracle, tmp_path):
        oracle.RETROS_FILE = tmp_path / "nonexistent.jsonl"
        budget = oracle.Budget(10.0)
        result = oracle.gather_classifier_signals([], budget)
        assert result == {}

    def test_filters_old_entries(self, oracle, tmp_path):
        retros_path = tmp_path / "old-retros.jsonl"
        with retros_path.open("w") as f:
            # Entry from 60 days ago — should be excluded
            f.write(json.dumps({
                "ts": "2020-01-01T00:00:00Z",
                "classifier": "old_classifier",
            }) + "\n")
        oracle.RETROS_FILE = retros_path
        budget = oracle.Budget(10.0)
        result = oracle.gather_classifier_signals([], budget)
        assert "old_classifier" not in result


# ---------------------------------------------------------------------------
# gather_audit_signals tests
# ---------------------------------------------------------------------------

class TestGatherAuditSignals:
    def test_finds_matching_file_references(self, oracle, tmp_path, tmp_audit):
        oracle.AUDIT_LOG = tmp_audit
        budget = oracle.Budget(10.0)
        files = [{"path": "backend/spec_oracle.py"}]
        result = oracle.gather_audit_signals(files, budget)

        assert len(result) >= 1
        assert any(h["file"] == "backend/spec_oracle.py" for h in result)

    def test_returns_empty_when_no_audit(self, oracle, tmp_path):
        oracle.AUDIT_LOG = tmp_path / "nonexistent.jsonl"
        budget = oracle.Budget(10.0)
        result = oracle.gather_audit_signals([{"path": "backend/foo.py"}], budget)
        assert result == []

    def test_returns_empty_when_no_files(self, oracle, tmp_path, tmp_audit):
        oracle.AUDIT_LOG = tmp_audit
        budget = oracle.Budget(10.0)
        result = oracle.gather_audit_signals([], budget)
        assert result == []


# ---------------------------------------------------------------------------
# has_findings tests
# ---------------------------------------------------------------------------

class TestHasFindings:
    def test_false_when_all_empty(self, oracle):
        artifact = {
            "files": [], "discussions_referenced": [], "symbols": [],
            "classifier_signals": {}, "audit_signals": [],
        }
        assert not oracle.has_findings(artifact)

    def test_true_when_files(self, oracle):
        artifact = {
            "files": [{"path": "backend/foo.py"}],
            "discussions_referenced": [], "symbols": [],
            "classifier_signals": {}, "audit_signals": [],
        }
        assert oracle.has_findings(artifact)

    def test_true_when_classifier_signals(self, oracle):
        artifact = {
            "files": [], "discussions_referenced": [], "symbols": [],
            "classifier_signals": {"cosmetic_retry": 2}, "audit_signals": [],
        }
        assert oracle.has_findings(artifact)


# ---------------------------------------------------------------------------
# render_markdown tests
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    def _make_artifact(self, **kwargs):
        defaults = {
            "discussion": 1127,
            "generated_at": "2026-05-19T12:00:00Z",
            "files": [],
            "discussions_referenced": [],
            "symbols": [],
            "classifier_signals": {},
            "audit_signals": [],
            "incomplete": False,
            "duration_ms": 1500,
            "sources_consulted": ["discussion_body"],
            "suppressed": False,
        }
        defaults.update(kwargs)
        return defaults

    def test_header_normal(self, oracle):
        artifact = self._make_artifact()
        md = oracle.render_markdown(artifact, 1127, incomplete=False)
        assert "**Empirical context (auto-generated)**" in md

    def test_header_incomplete(self, oracle):
        artifact = self._make_artifact()
        md = oracle.render_markdown(artifact, 1127, incomplete=True)
        assert "**Empirical context (incomplete)**" in md

    def test_files_section(self, oracle):
        artifact = self._make_artifact(
            files=[{
                "path": "backend/spec_oracle.py",
                "partial": False,
                "recent_commits": [{"sha": "abc12345", "date": "2026-05-15", "subject": "fix spec oracle"}],
                "also_called_by": ["scripts/preflight.sh"],
            }]
        )
        md = oracle.render_markdown(artifact, 1127)
        assert "backend/spec_oracle.py" in md
        assert "fix spec oracle" in md
        assert "scripts/preflight.sh" in md

    def test_discussions_section(self, oracle):
        artifact = self._make_artifact(
            discussions_referenced=[{
                "number": 1122,
                "title": "Fix entry point scripts",
                "status": "DONE",
                "related_prs": [1100],
                "partial": False,
            }]
        )
        md = oracle.render_markdown(artifact, 1127)
        assert "D#1122" in md
        assert "DONE" in md
        assert "PR #1100" in md

    def test_classifier_signals_section(self, oracle):
        artifact = self._make_artifact(
            classifier_signals={"cosmetic_retry": 3, "wrong_premise_retry": 1}
        )
        md = oracle.render_markdown(artifact, 1127)
        assert "cosmetic_retry" in md
        assert "3 occurrence" in md

    def test_footer_contains_discussion_num(self, oracle):
        artifact = self._make_artifact()
        md = oracle.render_markdown(artifact, 1127)
        assert "1127" in md

    def test_suppresses_empty_sections(self, oracle):
        artifact = self._make_artifact(files=[])
        md = oracle.render_markdown(artifact, 1127)
        assert "Files referenced" not in md

    def test_partial_file_shows_timeout(self, oracle):
        artifact = self._make_artifact(
            files=[{"path": "backend/slow.py", "partial": True, "reason": "timeout"}]
        )
        md = oracle.render_markdown(artifact, 1127)
        assert "timed out" in md


# ---------------------------------------------------------------------------
# write_artifact tests
# ---------------------------------------------------------------------------

class TestWriteArtifact:
    def test_creates_json_file(self, oracle, tmp_path):
        oracle.STATE_DIR = tmp_path
        artifact = {
            "discussion": 999,
            "generated_at": "2026-05-19T12:00:00Z",
            "files": [],
            "discussions_referenced": [],
            "symbols": [],
            "classifier_signals": {},
            "audit_signals": [],
            "incomplete": False,
            "duration_ms": 100,
            "sources_consulted": ["discussion_body"],
            "suppressed": False,
        }
        out_path = oracle.write_artifact(artifact, 999)
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert data["discussion"] == 999
        assert "generated_at" in data
        assert "files" in data
        assert "discussions_referenced" in data
        assert "symbols" in data
        assert "classifier_signals" in data

    def test_directory_created(self, oracle, tmp_path):
        oracle.STATE_DIR = tmp_path / "new-state"
        artifact = {"discussion": 1, "generated_at": "x", "files": [],
                    "discussions_referenced": [], "symbols": {}, "classifier_signals": {},
                    "audit_signals": [], "incomplete": False, "duration_ms": 0,
                    "sources_consulted": [], "suppressed": False}
        oracle.write_artifact(artifact, 1)
        assert (tmp_path / "new-state" / "spec-context" / "1.json").exists()


# ---------------------------------------------------------------------------
# append_audit_event tests
# ---------------------------------------------------------------------------

class TestAppendAuditEvent:
    def test_appends_valid_json_line(self, oracle, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        oracle.AUDIT_LOG = audit_path
        oracle.append_audit_event(
            discussion=1127, duration_ms=1500, found_files=2,
            found_discussions=1, found_symbols=1, incomplete=False, suppressed=False,
        )
        assert audit_path.exists()
        line = audit_path.read_text().strip()
        data = json.loads(line)
        assert data["event"] == "spec_oracle_run"
        assert data["discussion"] == 1127
        assert data["duration_ms"] == 1500
        assert data["found_files"] == 2
        assert data["incomplete"] is False
        assert data["suppressed"] is False

    def test_appends_to_existing_file(self, oracle, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        audit_path.write_text('{"existing": true}\n')
        oracle.AUDIT_LOG = audit_path
        oracle.append_audit_event(
            discussion=42, duration_ms=200, found_files=0,
            found_discussions=0, found_symbols=0, incomplete=False, suppressed=True,
        )
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["existing"] is True
        assert json.loads(lines[1])["event"] == "spec_oracle_run"


# ---------------------------------------------------------------------------
# Budget fault injection test (SPEC_ORACLE_FAULT=slow_git)
# ---------------------------------------------------------------------------

class TestBudgetFaultInjection:
    def test_slow_git_does_not_exceed_10s(self, oracle, monkeypatch):
        """With SPEC_ORACLE_FAULT=slow_git, the oracle still returns within 10s budget.

        The slow_git fault injects 5s sleep in git calls. With a 2s per-subquery timeout
        and 10s total budget, the oracle should complete without exceeding 10s.
        We simulate the fault by making _run_with_timeout return (timed_out=True) for
        git commands, mimicking what happens when the 5s sleep exceeds the 2s timeout.
        """
        monkeypatch.setenv("SPEC_ORACLE_FAULT", "slow_git")

        start = time.monotonic()
        budget = oracle.Budget(10.0)

        # Simulate SPEC_ORACLE_FAULT=slow_git: git calls time out after 2s sub-timeout
        def fake_run_with_fault(cmd, timeout, cwd=None):
            if cmd[0] == "git":
                # The fault would sleep 5s, but subprocess timeout catches it at `timeout` seconds
                return "", True  # simulates timeout
            return "", False

        with patch.object(oracle, "_run_with_timeout", side_effect=fake_run_with_fault):
            files = oracle.gather_file_context(["backend/spec_oracle.py"], budget)

        elapsed = time.monotonic() - start
        # Should complete well within 10s since per-subquery timeout is 2s
        assert elapsed < 10.0
        assert files[0]["partial"] is True  # git timed out


# ---------------------------------------------------------------------------
# Integration: live smoke against D#1122
#
# Opt-in:  pytest tests/test_spec_context_oracle.py -m integration
# Requires network access (gh CLI) and a populated state dir.
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestLiveIntegration:
    """Live integration test against the real closed Discussion D#1122.

    Run with:  pytest tests/test_spec_context_oracle.py -m integration
    Skipped by default so CI fast-path is unaffected.
    """

    def test_oracle_dry_run_d1122(self, tmp_path, monkeypatch):
        """Run spec-context-oracle against D#1122 in --dry-run mode.

        Asserts:
        - exit code 0
        - JSON artifact exists at <state_dir>/spec-context/1122.json
        - artifact has required top-level keys: files, symbols, discussions_referenced
        - at least one files[] entry has fix_cycles and last_pr fields
        - oracle stdout contains the 'Empirical context' header
        """
        state_dir = tmp_path / "oracle-test-state"
        state_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state_dir)

        result = subprocess.run(
            [sys.executable,
             str(_REPO_ROOT / "scripts" / "spec-context-oracle.py"),
             "1122",
             "--dry-run",
             "--max-duration", "30"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(_REPO_ROOT),
            env=env,
        )

        # exit code 0
        assert result.returncode == 0, (
            f"oracle exited with code {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )

        # artifact exists
        artifact_path = state_dir / "spec-context" / "1122.json"
        assert artifact_path.exists(), (
            f"Artifact not found at {artifact_path}\nstderr: {result.stderr[:500]}"
        )

        data = json.loads(artifact_path.read_text())

        # required top-level keys
        for key in ("files", "symbols", "discussions_referenced"):
            assert key in data, f"Missing key '{key}' in artifact"

        # at least one file entry has fix_cycles and last_pr
        file_entries = data.get("files", [])
        assert any(
            "fix_cycles" in f and "last_pr" in f
            for f in file_entries
        ), (
            f"No files[] entry has fix_cycles/last_pr fields. "
            f"Got {len(file_entries)} file entries: {[f.get('path') for f in file_entries]}"
        )

        # "Empirical context" header in stdout (dry-run prints to stdout)
        assert "Empirical context" in result.stdout, (
            f"'Empirical context' not in oracle stdout.\n"
            f"stdout: {result.stdout[:500]}"
        )


# ---------------------------------------------------------------------------
# Integration: build_artifact has required keys
# ---------------------------------------------------------------------------

class TestBuildArtifact:
    def test_required_keys_present(self, oracle):
        artifact = oracle.build_artifact(
            discussion=1127,
            files=[],
            discussions_referenced=[],
            symbols=[],
            classifier_signals={},
            audit_signals=[],
            incomplete=False,
            duration_ms=1200,
            sources_consulted=["discussion_body"],
        )
        required_keys = {
            "discussion", "generated_at", "files", "discussions_referenced",
            "symbols", "classifier_signals", "incomplete", "sources_consulted",
        }
        for key in required_keys:
            assert key in artifact, f"Missing required key: {key}"


# ---------------------------------------------------------------------------
# GraphQL target repo (D#2348 PR-a)
# ---------------------------------------------------------------------------

class TestGraphQLTargetRepo:
    """The two gh graphql queries used to spell the repo out as a literal.

    It was the pre-rename slug, so it resolved through GitHub's rename
    redirect and never errored — the oracle would have gone on reading a repo
    it wasn't pointed at, quietly. These assert the argv the oracle actually
    hands to gh, not that a grep comes back empty.
    """

    def _capture_argv(self, oracle, monkeypatch, stdout):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(oracle.subprocess, "run", fake_run)
        return seen

    def test_discussion_title_query_targets_the_resolved_repo(self, oracle, monkeypatch):
        from backend._repo import REPO

        owner, name = REPO.split("/", 1)
        seen = self._capture_argv(
            oracle, monkeypatch,
            json.dumps({"data": {"repository": {"discussion": {"title": "T"}}}}),
        )

        assert oracle._get_discussion_title(1234, oracle.Budget(10.0)) == "T"

        query = " ".join(seen["cmd"])
        assert f'owner:"{owner}"' in query
        assert f'name:"{name}"' in query

    def test_node_id_query_targets_the_resolved_repo(self, oracle, monkeypatch):
        from backend._repo import REPO

        owner, name = REPO.split("/", 1)
        seen = self._capture_argv(
            oracle, monkeypatch,
            json.dumps({"data": {"repository": {"discussion": {"id": "D_1"}}}}),
        )

        assert oracle.get_discussion_node_id(1234) == "D_1"

        query = " ".join(seen["cmd"])
        assert f'owner:"{owner}"' in query
        assert f'name:"{name}"' in query

    def test_no_slug_literal_left_in_the_oracle(self):
        source = (_REPO_ROOT / "scripts" / "spec-context-oracle.py").read_text()
        assert 'owner:\\"autonomous-agent-7\\"' not in source
        assert "autonomous-forever" not in source
