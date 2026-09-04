"""
Tests for backend/prompt_drift.py — spawn-prompt drift detector.

Acceptance Criteria coverage:
  AC1: check_all() returns empty issue lists on current main (clean state).
  AC2: check returns non-zero exit when drift injected.
  AC3: monkeypatching _GATE_CHECKS_BY_ROLE causes check_role to report missing key.
  AC4: extract_claude_md_rules parses all four documented role sections and their keys.
  AC5: preflight wiring covered by integration test in tests/integration/.
  AC6: check_all() aggregates per-role; at most one summary line from main().

D#2027 coverage (three-bucket classification, honest denominator):
  TestDriftReportBuckets: an unreachable role, a gate role missing rules with
  and without a ledger entry, and the summary string naming its denominator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.prompt_drift import (
    DriftIssue,
    RoleRules,
    check_all,
    check_role,
    extract_claude_md_rules,
    main,
)

_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"


# ---------------------------------------------------------------------------
# AC4: extract_claude_md_rules parses documented role sections + gate keys
# ---------------------------------------------------------------------------

class TestExtractClaudeMdRules:
    def test_parses_all_documented_roles(self):
        rules = extract_claude_md_rules(_CLAUDE_MD)
        assert "executor" in rules, "executor section not found"
        assert "code-reviewer" in rules, "code-reviewer section not found"
        assert "project-manager" in rules, "project-manager section not found"

    def test_executor_gate_keys(self):
        rules = extract_claude_md_rules(_CLAUDE_MD)
        keys = rules["executor"].gate_keys
        assert "gates.lint_must_pass" in keys, f"gates.lint_must_pass not found; got {keys}"
        assert "policies.executor.pr_size_max_lines" in keys, (
            f"policies.executor.pr_size_max_lines not found; got {keys}"
        )

    def test_code_reviewer_gate_keys(self):
        rules = extract_claude_md_rules(_CLAUDE_MD)
        keys = rules["code-reviewer"].gate_keys
        assert "gates.security_review" in keys, f"gates.security_review not found; got {keys}"
        assert "policies.code_reviewer.max_review_rounds" in keys, (
            f"policies.code_reviewer.max_review_rounds not found; got {keys}"
        )

    def test_project_manager_gate_keys(self):
        rules = extract_claude_md_rules(_CLAUDE_MD)
        keys = rules["project-manager"].gate_keys
        assert "gates.idea_generation" in keys, (
            f"gates.idea_generation not found; got {keys}"
        )
        assert "policies.pm.discussion_timeout_minutes" in keys, (
            f"policies.pm.discussion_timeout_minutes not found; got {keys}"
        )

    def test_returns_empty_on_empty_file(self, tmp_path: Path):
        empty = tmp_path / "CLAUDE.md"
        empty.write_text("", encoding="utf-8")
        rules = extract_claude_md_rules(empty)
        assert rules == {}

    def test_ignores_prose_mentions(self, tmp_path: Path):
        """Gate keys mentioned only in prose (not in bash blocks) should not be extracted."""
        content = """\
#### Executor — Control Plane Gates

You should read gates.lint_must_pass before proceeding.

No bash block here.
"""
        md = tmp_path / "CLAUDE.md"
        md.write_text(content, encoding="utf-8")
        rules = extract_claude_md_rules(md)
        # executor should not have any keys since there are no bash blocks
        if "executor" in rules:
            assert len(rules["executor"].gate_keys) == 0


# ---------------------------------------------------------------------------
# AC1: check_all() is clean on current main
# ---------------------------------------------------------------------------

class TestCheckAllClean:
    def test_no_drift_on_current_main(self):
        """All checked roles must have their gate keys present in rendered
        prompts, and no role may be unreachable or a missing-rules failure,
        on the real repo tree."""
        report = check_all(claude_md_path=_CLAUDE_MD)
        if report.is_fatal:
            drift_lines = [
                str(issue)
                for issues in report.checked.values()
                for issue in issues
            ]
            pytest.fail(
                f"Drift/missing-rules/unreachable detected on current main — "
                f"checked={report.checked_count}/{report.total_roles} "
                f"missing_required={report.missing_required} "
                f"unreachable={report.unreachable}\n"
                + "\n".join(f"  {line}" for line in drift_lines)
            )


# ---------------------------------------------------------------------------
# D#2027: three-bucket classification (CHECKED / NO_RULES / UNREACHABLE) and
# the honest N-of-M denominator.
# ---------------------------------------------------------------------------

class TestDriftReportBuckets:
    """Coverage for the bucket classification introduced to fix D#2027:
    checked-3-of-24-reports-OK. Each test builds a tiny fake role universe
    in tmp_path (via monkeypatching the module's role-universe/required-role
    sets) so these don't depend on the real repo's 24 roles or mutate it."""

    def _make_agents_dir(self, tmp_path: Path, cards: dict[str, str]) -> Path:
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        for name, content in cards.items():
            (agents_dir / f"{name}.md").write_text(content, encoding="utf-8")
        return agents_dir

    def test_unreachable_role_is_fatal_and_named(self, tmp_path: Path, monkeypatch):
        """A role in the universe with no card on disk at all is UNREACHABLE:
        always fatal, and named together with the path checked."""
        import backend.prompt_drift as pd

        self._make_agents_dir(tmp_path, {})  # "ghost" has no card
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")

        monkeypatch.setattr(pd, "_SPAWN_TEMPLATE_ROLES", frozenset({"ghost"}))
        monkeypatch.setattr(pd, "REQUIRED_RULE_ROLES", frozenset())

        report = pd.check_all(
            claude_md_path=claude_md,
            exemptions_path=tmp_path / "exemptions.json",
        )

        assert report.is_fatal
        names = [r for r, _ in report.unreachable]
        assert "ghost" in names
        assert any("ghost.md" in detail for _, detail in report.unreachable), (
            f"expected the missing card's path in the detail, got {report.unreachable}"
        )

    def test_required_role_missing_rules_without_ledger_is_fatal(
        self, tmp_path: Path, monkeypatch
    ):
        """A REQUIRED_RULE_ROLES member with a readable card that declares
        zero gate keys, and no exemptions-ledger entry, is fatal."""
        import backend.prompt_drift as pd

        self._make_agents_dir(
            tmp_path, {"gatekeeper": "# gatekeeper\n\nno gates section here.\n"}
        )
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")
        exemptions = tmp_path / "exemptions.json"
        exemptions.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(pd, "_SPAWN_TEMPLATE_ROLES", frozenset({"gatekeeper"}))
        monkeypatch.setattr(pd, "REQUIRED_RULE_ROLES", frozenset({"gatekeeper"}))

        report = pd.check_all(claude_md_path=claude_md, exemptions_path=exemptions)

        assert report.is_fatal
        assert "gatekeeper" in report.missing_required
        assert "gatekeeper" not in report.exempt

    def test_required_role_missing_rules_with_ledger_entry_is_exempt(
        self, tmp_path: Path, monkeypatch
    ):
        """The same role, but with a ledger entry, is exempt — reported by
        name, never fatal."""
        import backend.prompt_drift as pd

        self._make_agents_dir(
            tmp_path, {"gatekeeper": "# gatekeeper\n\nno gates section here.\n"}
        )
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")
        exemptions = tmp_path / "exemptions.json"
        exemptions.write_text(
            _json.dumps({"gatekeeper": "no Control Plane Gates section yet"}),
            encoding="utf-8",
        )

        monkeypatch.setattr(pd, "_SPAWN_TEMPLATE_ROLES", frozenset({"gatekeeper"}))
        monkeypatch.setattr(pd, "REQUIRED_RULE_ROLES", frozenset({"gatekeeper"}))

        report = pd.check_all(claude_md_path=claude_md, exemptions_path=exemptions)

        assert not report.is_fatal
        assert "gatekeeper" in report.exempt
        assert "gatekeeper" not in report.missing_required

    def test_ordinary_role_with_no_rules_is_never_fatal(
        self, tmp_path: Path, monkeypatch
    ):
        """A role that is NOT in REQUIRED_RULE_ROLES and declares no gate
        keys is reported (no_rules) but never fatal on its own."""
        import backend.prompt_drift as pd

        self._make_agents_dir(
            tmp_path, {"advisory-role": "# advisory-role\n\nnothing here.\n"}
        )
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")

        monkeypatch.setattr(pd, "_SPAWN_TEMPLATE_ROLES", frozenset({"advisory-role"}))
        monkeypatch.setattr(pd, "REQUIRED_RULE_ROLES", frozenset())

        report = pd.check_all(
            claude_md_path=claude_md,
            exemptions_path=tmp_path / "exemptions.json",
        )

        assert not report.is_fatal
        assert report.no_rules == ["advisory-role"]

    def test_summary_names_denominator_via_cli(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """The CLI summary line must say 'checked N of M roles' and must
        never use the old, misleading 'role(s) checked' phrasing."""
        import backend.prompt_drift as pd

        self._make_agents_dir(
            tmp_path,
            {
                "a": "# a\n\nno gates\n",
                "b": "# b\n\nno gates\n",
            },
        )
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("", encoding="utf-8")

        monkeypatch.setattr(pd, "_SPAWN_TEMPLATE_ROLES", frozenset({"a", "b"}))
        monkeypatch.setattr(pd, "REQUIRED_RULE_ROLES", frozenset())

        exit_code = pd.main([
            "check",
            "--claude-md", str(claude_md),
            "--exemptions", str(tmp_path / "missing-exemptions.json"),
        ])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "checked 0 of 2 roles" in captured.out
        assert "role(s) checked" not in captured.out


# ---------------------------------------------------------------------------
# AC3: monkeypatch _GATE_CHECKS_BY_ROLE to inject drift and verify detection
# ---------------------------------------------------------------------------

class TestCheckRoleDetectsMissingGate:
    def test_detects_missing_gate_key_via_render_mock(self):
        """Monkeypatching _render_role_prompt to return a stale prompt triggers drift detection."""
        import backend.prompt_drift as pd

        # A prompt that is missing gates.lint_must_pass entirely
        stale_prompt = "This prompt has no gate keys at all."

        with patch.object(pd, "_render_role_prompt", return_value=stale_prompt):
            rules = RoleRules(gate_keys=frozenset({"gates.lint_must_pass"}))
            issues = check_role("executor", rules)

        assert any(
            i.missing_key == "gates.lint_must_pass" for i in issues
        ), f"Expected gates.lint_must_pass to be reported missing; got {issues}"

    def test_clean_role_returns_empty_issues(self):
        """A role whose prompt contains the declared gate key should produce no issues."""
        rules = RoleRules(gate_keys=frozenset({"gates.lint_must_pass"}))
        # executor prompt does contain lint_must_pass — so no drift expected
        issues = check_role("executor", rules)
        assert issues == [], f"Expected no issues for executor, got {issues}"

    def test_multiple_missing_keys_all_reported(self):
        """All missing keys should be reported, not just the first."""
        import backend.prompt_drift as pd

        stale_prompt = "This prompt has no gate keys at all."

        with patch.object(pd, "_render_role_prompt", return_value=stale_prompt):
            rules = RoleRules(gate_keys=frozenset({
                "gates.lint_must_pass",
                "policies.executor.pr_size_max_lines",
            }))
            issues = check_role("executor", rules)

        missing_keys = {i.missing_key for i in issues}
        assert "gates.lint_must_pass" in missing_keys
        assert "policies.executor.pr_size_max_lines" in missing_keys

    def test_detects_missing_gate_when_spawn_templates_gate_checks_patched(self):
        """When _GATE_CHECKS_BY_ROLE is patched to remove a key that ONLY appears there,
        and that key is NOT in the template body, check_role detects the drift.
        This tests the appendix-only path: if a gate key is mandated by CLAUDE.md
        but removed from the gate-checks appendix AND not in the template body,
        the detector flags it."""
        import backend.spawn_templates as st
        import backend.prompt_drift as pd

        # We pick a key that we know does NOT appear in the template body:
        # gates.fake_key_for_test_only — completely synthetic
        FAKE_KEY = "gates.fake_key_for_test_only"

        # Rules say the role MUST have this key
        rules = RoleRules(gate_keys=frozenset({FAKE_KEY}))
        # The rendered prompt will not contain this key — so drift is detected
        issues = check_role("executor", rules)

        assert any(
            i.missing_key == FAKE_KEY for i in issues
        ), f"Expected {FAKE_KEY} to be reported missing; got {issues}"


# ---------------------------------------------------------------------------
# AC2: CLI exits non-zero on drift
# ---------------------------------------------------------------------------

class TestCLI:
    def test_exits_zero_when_clean(self):
        """CLI exits 0 on the current clean codebase."""
        exit_code = main(["check", "--claude-md", str(_CLAUDE_MD)])
        assert exit_code == 0, f"Expected exit 0 but got {exit_code}"

    def test_exits_nonzero_on_drift(self):
        """CLI exits 1 when drift is injected via _render_role_prompt mock."""
        import backend.prompt_drift as pd

        stale_prompt = "No gate keys here."
        with patch.object(pd, "_render_role_prompt", return_value=stale_prompt):
            exit_code = main(["check", "--claude-md", str(_CLAUDE_MD)])

        assert exit_code == 1, f"Expected exit 1 (drift) but got {exit_code}"

    def test_quiet_flag_suppresses_per_issue_lines(self, capsys: Any):
        """--quiet flag should not print per-issue lines."""
        import backend.prompt_drift as pd

        stale_prompt = "No gate keys here."
        with patch.object(pd, "_render_role_prompt", return_value=stale_prompt):
            main(["check", "--claude-md", str(_CLAUDE_MD), "--quiet"])

        captured = capsys.readouterr()
        # Should not contain per-issue "executor: missing key" lines
        assert "executor: missing key" not in captured.out

    def test_summary_line_count_is_one(self, capsys: Any):
        """Regardless of how many roles drift, main() prints exactly one summary line."""
        import backend.prompt_drift as pd

        # Inject drift by making every role prompt return a string with no gate keys
        stale_prompt = "No gate keys here."
        with patch.object(pd, "_render_role_prompt", return_value=stale_prompt):
            main(["check", "--claude-md", str(_CLAUDE_MD), "--quiet"])

        captured = capsys.readouterr()
        # Count lines that contain "prompt-drift:"
        summary_lines = [l for l in captured.out.splitlines() if "prompt-drift:" in l]
        assert len(summary_lines) == 1, (
            f"Expected exactly 1 summary line, got {len(summary_lines)}: {summary_lines}"
        )


# ---------------------------------------------------------------------------
# DriftIssue string representation
# ---------------------------------------------------------------------------

class TestDriftIssue:
    def test_str(self):
        issue = DriftIssue(role="executor", missing_key="gates.lint_must_pass")
        assert str(issue) == "executor: missing key gates.lint_must_pass"


# ---------------------------------------------------------------------------
# transcript_reader in-flight skip (AC1–AC5 per Discussion #1197)
# ---------------------------------------------------------------------------

import json as _json
import os as _os
import time as _time
import sys as _sys
import io

import backend.transcript_reader as _tr


def _write_valid_output(path: Path) -> None:
    """Write a minimal valid .output file."""
    record = {"type": "message", "message": {"role": "assistant", "content": "hello"}}
    path.write_text(_json.dumps(record) + "\n", encoding="utf-8")


def _write_partial_output(path: Path) -> None:
    """Write a .output file whose last line is a truncated JSON object (in-flight scenario)."""
    record = {"type": "message", "message": {"role": "assistant", "content": "hello"}}
    partial = _json.dumps(record) + "\n" + '{"type": "message", "message": {"role'
    path.write_text(partial, encoding="utf-8")


class TestInFlightSkip:
    """transcript_reader must skip .output files whose mtime is within IN_FLIGHT_SECONDS."""

    def test_ac1_in_flight_file_excluded(self, tmp_path: Path, monkeypatch):
        """AC1: find_transcripts() excludes an .output file with mtime = now."""
        output_file = tmp_path / "task.output"
        _write_valid_output(output_file)
        # mtime = now → in-flight
        now = _time.time()
        _os.utime(output_file, (now, now))

        monkeypatch.setattr(_tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(_tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        results = _tr.find_transcripts()
        assert output_file not in results, (
            "In-flight .output (mtime=now) must be excluded by find_transcripts()"
        )

    def test_ac2_completed_file_included(self, tmp_path: Path, monkeypatch):
        """AC2: find_transcripts() includes a .output file with mtime well in the past."""
        output_file = tmp_path / "task.output"
        _write_valid_output(output_file)
        # mtime = now - (IN_FLIGHT_SECONDS + 5) → completed
        old_mtime = _time.time() - (_tr.IN_FLIGHT_SECONDS + 5)
        _os.utime(output_file, (old_mtime, old_mtime))

        monkeypatch.setattr(_tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(_tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        results = _tr.find_transcripts()
        assert output_file in results, (
            "Completed .output (old mtime) must be included by find_transcripts()"
        )
        turns = list(_tr.iter_turns(output_file))
        assert len(turns) == 1, "Should yield one turn from the completed file"

    def test_ac3_no_warning_for_in_flight_partial(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """AC3: a partial last line in an in-flight .output file produces no warning."""
        output_file = tmp_path / "task.output"
        _write_partial_output(output_file)
        # mtime = now → in-flight
        now = _time.time()
        _os.utime(output_file, (now, now))

        monkeypatch.setattr(_tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(_tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        # find_transcripts() won't include this file; iter_turns() is never called on it.
        results = _tr.find_transcripts()
        assert output_file not in results

        captured = capsys.readouterr()
        assert "skipping malformed JSONL" not in captured.err, (
            "No malformed-JSONL warning should appear for an in-flight file"
        )

    def test_ac4_malformed_completed_file_warns(self, tmp_path: Path, monkeypatch, capsys):
        """AC4: a completed .output file with GENUINE MID-FILE corruption still emits the warning.

        Updated (D#1209): _write_partial_output writes a trailing-truncation file, which
        is now class (2) and is silently suppressed.  This test is updated to use a
        mid-file-corrupt fixture: valid line + garbage middle line + valid last line.
        That is class (3) — the genuine signal we must preserve.
        """
        output_file = tmp_path / "task.output"
        # Write: valid, garbage middle, valid last — class (3) mid-file corruption
        valid = {"type": "message", "message": {"role": "assistant", "content": "hello"}}
        content = (
            _json.dumps(valid) + "\n"
            + "THIS IS GARBAGE NOT JSON\n"
            + _json.dumps(valid) + "\n"
        )
        output_file.write_text(content, encoding="utf-8")
        # mtime = now - (IN_FLIGHT_SECONDS + 5) → completed
        old_mtime = _time.time() - (_tr.IN_FLIGHT_SECONDS + 5)
        _os.utime(output_file, (old_mtime, old_mtime))

        monkeypatch.setattr(_tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(_tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        results = _tr.find_transcripts()
        assert output_file in results, "Completed file must be found"

        # Drain the iterator to trigger the warning
        turns = list(_tr.iter_turns(output_file))

        captured = capsys.readouterr()
        assert "skipping malformed JSONL" in captured.err, (
            "Warning must still fire for a genuinely mid-file-corrupt completed .output file"
        )
        assert len(turns) == 2, "Both valid turns (first and last) must be yielded"

    def test_ac5_jsonl_never_skipped_by_recency(self, tmp_path: Path, monkeypatch):
        """AC5: a .jsonl archive file with mtime = now is never excluded."""
        jsonl_file = tmp_path / "agent-abc-executor.jsonl"
        record = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
        jsonl_file.write_text(_json.dumps(record) + "\n", encoding="utf-8")
        # mtime = now → would be in-flight if it were .output, but .jsonl is exempt
        now = _time.time()
        _os.utime(jsonl_file, (now, now))

        monkeypatch.setattr(_tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(_tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        results = _tr.find_transcripts()
        assert jsonl_file in results, (
            ".jsonl archive file must never be excluded by the in-flight recency filter"
        )


# ---------------------------------------------------------------------------
# transcript_reader malformed-JSONL classification (D#1209 AC1-AC6)
# ---------------------------------------------------------------------------


def _old_mtime() -> float:
    """Return an mtime well past IN_FLIGHT_SECONDS so files are not in-flight."""
    return _time.time() - (_tr.IN_FLIGHT_SECONDS + 60)


def _make_valid_record() -> str:
    rec = {"type": "message", "message": {"role": "assistant", "content": "hello"}}
    return _json.dumps(rec)


class TestMalformedClassification:
    """D#1209: classify malformed files and suppress benign per-file warnings."""

    # --- helpers ---

    def _reset_stats(self) -> None:
        """Reset module-level counter between tests."""
        _tr._SKIP_STATS["skipped_non_jsonl"] = 0
        _tr._SKIP_STATS["skipped_trailing_truncation"] = 0
        _tr._SKIP_STATS["corrupt_midfile"] = 0

    # --- AC1: trailing-truncation-only → no per-file warning, valid turns yielded ---

    def test_ac1_trailing_truncation_no_warning(self, tmp_path: Path, capsys):
        """AC1: dead partial (valid + truncated trailing line) → no per-file warning."""
        self._reset_stats()
        f = tmp_path / "partial.output"
        f.write_text(
            _make_valid_record() + "\n"
            + '{"type": "message", "message": {"role',  # truncated
            encoding="utf-8",
        )
        _os.utime(f, (_old_mtime(), _old_mtime()))

        turns = list(_tr.iter_turns(f))
        captured = capsys.readouterr()
        assert "skipping malformed JSONL" not in captured.err, (
            "Trailing-truncation file must not emit per-file warning"
        )
        assert len(turns) == 1, "Valid leading turn must still be yielded"
        assert _tr._SKIP_STATS["skipped_trailing_truncation"] == 1

    # --- AC2: all-fail (non-JSONL) → no per-file warning, zero turns ---

    def test_ac2_non_jsonl_no_warning(self, tmp_path: Path, capsys):
        """AC2: file whose every line is plain shell/test stdout → no warning, no turns."""
        self._reset_stats()
        f = tmp_path / "shell.output"
        f.write_text(
            "[merge-and-hook] merging PR #969...\n"
            "File \"x.py\", line 22\n"
            "diff --git a/foo.py b/foo.py\n",
            encoding="utf-8",
        )
        _os.utime(f, (_old_mtime(), _old_mtime()))

        turns = list(_tr.iter_turns(f))
        captured = capsys.readouterr()
        assert "skipping malformed JSONL" not in captured.err, (
            "Non-JSONL .output file must not emit per-file warning"
        )
        assert turns == [], "Non-JSONL file must yield zero turns"
        assert _tr._SKIP_STATS["skipped_non_jsonl"] == 1

    # --- AC3: mid-file corruption → per-file warning fires exactly once ---

    def test_ac3_midfile_corruption_warns(self, tmp_path: Path, capsys):
        """AC3: valid + garbage-middle + valid → warning fires once, both valid turns yielded."""
        self._reset_stats()
        f = tmp_path / "corrupt.output"
        f.write_text(
            _make_valid_record() + "\n"
            + "NOT JSON AT ALL\n"
            + _make_valid_record() + "\n",
            encoding="utf-8",
        )
        _os.utime(f, (_old_mtime(), _old_mtime()))

        turns = list(_tr.iter_turns(f))
        captured = capsys.readouterr()
        warning_count = captured.err.count("skipping malformed JSONL")
        assert warning_count == 1, (
            f"Exactly one per-file warning expected, got {warning_count}"
        )
        assert len(turns) == 2, "Both valid turns must be yielded"
        assert _tr._SKIP_STATS["corrupt_midfile"] == 1

    # --- AC5: aggregate summary line emitted with correct counts ---

    def test_ac5_aggregate_flush_correct_counts(self, capsys):
        """AC5: _flush_skip_summary emits one line with all three counts."""
        self._reset_stats()
        _tr._SKIP_STATS["skipped_non_jsonl"] = 5
        _tr._SKIP_STATS["skipped_trailing_truncation"] = 2
        _tr._SKIP_STATS["corrupt_midfile"] = 3

        _tr._flush_skip_summary()
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if "transcript_reader:" in l]
        assert len(lines) == 1, f"Expected exactly one aggregate line, got {lines}"
        assert "5" in lines[0]
        assert "2" in lines[0]
        assert "3" in lines[0]

    def test_ac5_no_aggregate_when_all_zero(self, capsys):
        """AC5: _flush_skip_summary is silent when all counters are zero."""
        self._reset_stats()
        _tr._flush_skip_summary()
        captured = capsys.readouterr()
        assert "transcript_reader:" not in captured.err, (
            "No aggregate line when all counters are zero"
        )

    def test_ac5_mixed_batch_aggregate_counts(self, tmp_path: Path, capsys):
        """AC5: after a mixed batch, counters match the actual file classification."""
        self._reset_stats()

        # Non-JSONL file
        f1 = tmp_path / "shell.output"
        f1.write_text("echo hello\npytest traceback\n", encoding="utf-8")
        _os.utime(f1, (_old_mtime(), _old_mtime()))

        # Trailing-truncation file
        f2 = tmp_path / "partial.output"
        f2.write_text(
            _make_valid_record() + "\n" + '{"truncated":',
            encoding="utf-8",
        )
        _os.utime(f2, (_old_mtime(), _old_mtime()))

        # Mid-file-corrupt file
        f3 = tmp_path / "corrupt.output"
        f3.write_text(
            _make_valid_record() + "\n"
            + "GARBAGE\n"
            + _make_valid_record() + "\n",
            encoding="utf-8",
        )
        _os.utime(f3, (_old_mtime(), _old_mtime()))

        for f in (f1, f2, f3):
            list(_tr.iter_turns(f))

        capsys.readouterr()  # consume per-file warnings
        assert _tr._SKIP_STATS["skipped_non_jsonl"] == 1
        assert _tr._SKIP_STATS["skipped_trailing_truncation"] == 1
        assert _tr._SKIP_STATS["corrupt_midfile"] == 1

    # --- AC6: clean .jsonl archive → no warning, no stats increment ---

    def test_ac6_clean_jsonl_no_warning_no_stats(self, tmp_path: Path, capsys):
        """AC6: a clean .jsonl archive yields turns with no warning and no stat increment."""
        self._reset_stats()
        f = tmp_path / "agent-abc-executor.jsonl"
        f.write_text(
            _make_valid_record() + "\n"
            + _make_valid_record() + "\n",
            encoding="utf-8",
        )
        _os.utime(f, (_old_mtime(), _old_mtime()))

        turns = list(_tr.iter_turns(f))
        captured = capsys.readouterr()
        assert "skipping malformed JSONL" not in captured.err
        assert len(turns) == 2
        assert _tr._SKIP_STATS["skipped_non_jsonl"] == 0
        assert _tr._SKIP_STATS["skipped_trailing_truncation"] == 0
        assert _tr._SKIP_STATS["corrupt_midfile"] == 0
