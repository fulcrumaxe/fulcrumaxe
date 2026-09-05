"""tests/test_run_analyst_live.py

Tests for the live-mode classifier extension in backend/run_analyst.py
(Discussion #574 PR-a).

Covers:
  - Each of the 4 allowlisted classifiers fires on partial-transcript fixtures
  - Non-allowlisted classifiers do NOT fire even when their pattern matches
  - next_byte_offset advances correctly across calls
  - --allow-partial flag is accepted
  - Empty transcript returns empty findings
  - Byte-offset resume: second call picks up from where first left off
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Allow imports from repo root and backend/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import run_analyst  # noqa: E402  (backend/ added to path above)

LIVE_ALLOWLIST = run_analyst.LIVE_ALLOWLIST
LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK = run_analyst.LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK
LIVE_WRONG_PREMISE_MIN_RETRIES = run_analyst.LIVE_WRONG_PREMISE_MIN_RETRIES
iter_turns_from_offset = run_analyst.iter_turns_from_offset
run_live_mode = run_analyst.run_live_mode


# ---------------------------------------------------------------------------
# Helpers to build minimal JSONL transcript fixtures
# ---------------------------------------------------------------------------

def _bash_turn(cmd: str, role: str = "assistant") -> dict:
    """One assistant turn with a Bash tool_call."""
    return {
        "role": role,
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "id": f"tool_{hash(cmd) & 0xFFFF:04x}",
                "input": {"command": cmd},
            }
        ],
    }


def _tool_result_turn(content: str, is_error: bool = False, tool_use_id: str = "tid") -> dict:
    """One user turn with a tool_result."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }
        ],
    }


def _agent_turn_text(text: str) -> dict:
    """One assistant turn with plain text (no tool calls)."""
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _user_text_turn(text: str) -> dict:
    """One user turn with plain text."""
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _write_transcript(tmp_path: Path, turns: list[dict], filename: str = "agent.output") -> Path:
    path = tmp_path / filename
    with open(path, "w") as f:
        for turn in turns:
            f.write(json.dumps(turn) + "\n")
    return path


def _agent_turn_with_general_purpose(prompt: str) -> dict:
    """Assistant turn calling Agent() with subagent_type=general-purpose."""
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "name": "Agent",
                "id": "agent_tool_001",
                "input": {
                    "subagent_type": "general-purpose",
                    "prompt": prompt,
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. git_rm_usage — hard rule, fires on any git rm in Bash
# ---------------------------------------------------------------------------

class TestLiveGitRmUsage:
    def test_fires_on_git_rm(self, tmp_path):
        turns = [
            _user_text_turn("Please clean up the old file"),
            _bash_turn("git rm backend/old_module.py"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "git_rm_usage" for f in result["findings"]), (
            "git_rm_usage should fire when git rm is in a Bash call"
        )

    def test_fires_on_git_rm_dash_r(self, tmp_path):
        turns = [
            _bash_turn("git rm -r backend/old_dir/"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "git_rm_usage" for f in result["findings"])

    def test_does_not_fire_on_archive_path(self, tmp_path):
        # git rm on archive/ is safe (the archive protocol)
        turns = [
            _bash_turn("agit rm -r archive/old-script-2026-01-01/"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        # Should NOT fire — archive/ path
        assert not any(f["category"] == "git_rm_usage" for f in result["findings"])

    def test_does_not_fire_on_unrelated_bash(self, tmp_path):
        turns = [
            _bash_turn("ls -la backend/"),
            _bash_turn("python3 -m pytest tests/ -v"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "git_rm_usage" for f in result["findings"])

    def test_finding_has_live_mode_flag(self, tmp_path):
        turns = [_bash_turn("git rm src/main.py")]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        git_rm_findings = [f for f in result["findings"] if f["category"] == "git_rm_usage"]
        assert git_rm_findings
        assert all(f.get("live_mode") is True for f in git_rm_findings)


# ---------------------------------------------------------------------------
# 2. forbidden_subagent_type — hard rule, fires on general-purpose
# ---------------------------------------------------------------------------

class TestLiveForbiddenSubagentType:
    def test_fires_on_general_purpose_in_agent_call(self, tmp_path):
        turns = [
            _user_text_turn("Spawn an agent to do some work"),
            _agent_turn_with_general_purpose("Do the thing"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "forbidden_subagent_type" for f in result["findings"])

    def test_fires_on_general_purpose_in_bash(self, tmp_path):
        # Some executors write Agent() inline in Bash scripts
        turns = [
            _bash_turn('Agent(subagent_type="general-purpose", prompt="do work")'),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "forbidden_subagent_type" for f in result["findings"])

    def test_does_not_fire_on_named_roles(self, tmp_path):
        turns = [
            _bash_turn('Agent(subagent_type="executor", prompt="implement feature")'),
            _bash_turn('Agent(subagent_type="code-reviewer", prompt="review pr")'),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "forbidden_subagent_type" for f in result["findings"])

    def test_finding_has_live_mode_flag(self, tmp_path):
        turns = [_agent_turn_with_general_purpose("just do it")]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        findings = [f for f in result["findings"] if f["category"] == "forbidden_subagent_type"]
        assert findings
        assert all(f.get("live_mode") is True for f in findings)


# ---------------------------------------------------------------------------
# 3. wrong_premise_retries — live threshold >= 8 (not 5 as in full mode)
# ---------------------------------------------------------------------------

class TestLiveWrongPremiseRetries:
    def _make_retry_turns(self, n_retries: int) -> list[dict]:
        """n_retries of the same failing Bash command."""
        turns = []
        for i in range(n_retries):
            tool_id = f"bash_{i:04d}"
            turns.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": tool_id,
                        "input": {"command": "python3 backend/nonexistent.py"},
                    }
                ],
            })
            turns.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "Error: No such file or directory: 'backend/nonexistent.py'",
                        "is_error": True,
                    }
                ],
            })
        return turns

    def test_fires_at_live_threshold(self, tmp_path):
        turns = self._make_retry_turns(LIVE_WRONG_PREMISE_MIN_RETRIES + 1)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "wrong_premise_retries" for f in result["findings"]), (
            f"Should fire when retries >= {LIVE_WRONG_PREMISE_MIN_RETRIES}"
        )

    def test_does_not_fire_below_live_threshold(self, tmp_path):
        # 5 retries: fires in full mode but NOT in live mode (threshold=8)
        turns = self._make_retry_turns(5)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "wrong_premise_retries" for f in result["findings"]), (
            "Should NOT fire at 5 retries in live mode (threshold is 8)"
        )

    def test_does_not_fire_at_threshold_minus_one(self, tmp_path):
        turns = self._make_retry_turns(LIVE_WRONG_PREMISE_MIN_RETRIES - 1)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "wrong_premise_retries" for f in result["findings"])

    def test_finding_includes_threshold_evidence(self, tmp_path):
        turns = self._make_retry_turns(LIVE_WRONG_PREMISE_MIN_RETRIES + 2)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        findings = [f for f in result["findings"] if f["category"] == "wrong_premise_retries"]
        assert findings
        # Evidence should mention the live threshold
        evidence_str = " ".join(str(e) for e in findings[0].get("evidence", []))
        assert "live_threshold" in evidence_str


# ---------------------------------------------------------------------------
# 4. tool_output_ignored — fires when 3+ consecutive errors are ignored
# ---------------------------------------------------------------------------

class TestLiveToolOutputIgnored:
    def _make_ignored_errors(self, n: int) -> list[dict]:
        """n user turns each with is_error:true, each followed by assistant ignoring it."""
        turns = []
        for i in range(n):
            tid = f"err_{i:04d}"
            # User turn: tool result with is_error=True
            turns.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": "Error: command failed with exit code 1",
                        "is_error": True,
                    }
                ],
            })
            # Assistant turn: ignores the error, no tool calls, no acknowledgment
            turns.append({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me continue with the next step."}
                ],
            })
        return turns

    def test_fires_at_live_streak_threshold(self, tmp_path):
        turns = self._make_ignored_errors(LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK + 1)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert any(f["category"] == "tool_output_ignored" for f in result["findings"]), (
            f"Should fire when ignored error streak >= {LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK}"
        )

    def test_does_not_fire_below_streak_threshold(self, tmp_path):
        turns = self._make_ignored_errors(LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK - 1)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "tool_output_ignored" for f in result["findings"])

    def test_resets_streak_on_pivot(self, tmp_path):
        """Streak resets when the agent calls a tool (pivot = course correction)."""
        tid_err = "err_0000"
        turns = [
            # Two ignored errors
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid_err,
                              "content": "Error: exit code 1", "is_error": True}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Continuing."}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "err_0001",
                              "content": "Error: not found", "is_error": True}],
            },
            # Pivot: agent calls Bash (course correction)
            _bash_turn("ls -la backend/"),
            # One more error after pivot — streak should have reset
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "err_0002",
                              "content": "Error: timeout", "is_error": True}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Still trying."}]},
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        # Max streak is 1 after the pivot reset — should NOT fire
        assert not any(f["category"] == "tool_output_ignored" for f in result["findings"])

    def test_finding_has_live_mode_flag(self, tmp_path):
        turns = self._make_ignored_errors(LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK + 1)
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        findings = [f for f in result["findings"] if f["category"] == "tool_output_ignored"]
        assert findings
        assert all(f.get("live_mode") is True for f in findings)


# ---------------------------------------------------------------------------
# 5. Non-allowlisted classifiers do NOT fire in live mode
# ---------------------------------------------------------------------------

class TestLiveAllowlistEnforcement:
    """Non-allowlisted classifiers must NOT fire even if their pattern matches."""

    def test_preflight_skipped_does_not_fire(self, tmp_path):
        # Pattern that would trigger classify_preflight_skipped in full mode:
        # gh pr create without scripts/preflight.sh call
        turns = [
            _bash_turn("gh pr create --base main --title 'my pr' --body 'desc'"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "preflight_skipped" for f in result["findings"]), (
            "preflight_skipped is not in LIVE_ALLOWLIST and should not fire"
        )

    def test_reviewer_skipped_does_not_fire(self, tmp_path):
        turns = [
            _user_text_turn("You are the impl-coordinator. Implement Discussion #42."),
            _agent_turn_text('"verdict": "done"'),  # done without code-reviewer spawn
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "reviewer_skipped_by_impl_coord" for f in result["findings"])

    def test_retro_skipped_does_not_fire(self, tmp_path):
        # Pattern: executor emitting verdict:done without agent_retros.py call
        turns = [
            _user_text_turn("You are the executor. Implement Discussion #99."),
            _agent_turn_text('"verdict": "done", "agent": "executor"'),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        assert not any(f["category"] == "retro_skipped" for f in result["findings"])

    def test_only_allowlisted_categories_in_output(self, tmp_path):
        """All categories in the findings output must be members of LIVE_ALLOWLIST."""
        turns = [
            _bash_turn("git rm backend/old.py"),
            _agent_turn_with_general_purpose("do work"),
            _bash_turn("gh pr create --base main --title 'x' --body 'y'"),  # preflight_skipped bait
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        for finding in result["findings"]:
            assert finding["category"] in LIVE_ALLOWLIST, (
                f"Category '{finding['category']}' is not in LIVE_ALLOWLIST"
            )

    def test_classifiers_run_field_matches_allowlist(self, tmp_path):
        path = _write_transcript(tmp_path, [_user_text_turn("hello")])
        result = run_live_mode(str(path))

        assert set(result["classifiers_run"]) == LIVE_ALLOWLIST


# ---------------------------------------------------------------------------
# 6. Byte-offset resume and next_byte_offset correctness
# ---------------------------------------------------------------------------

class TestLiveByteOffset:
    def test_empty_transcript_returns_zero_offset(self, tmp_path):
        path = tmp_path / "empty.output"
        path.write_text("")
        result = run_live_mode(str(path))

        assert result["next_byte_offset"] == 0
        assert result["findings"] == []
        assert result["turns_read"] == 0

    def test_offset_advances_after_full_read(self, tmp_path):
        turns = [
            _user_text_turn("start"),
            _bash_turn("ls -la"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        file_size = path.stat().st_size
        assert result["next_byte_offset"] == file_size
        assert result["turns_read"] == len(turns)

    def test_resume_from_offset_reads_only_new_turns(self, tmp_path):
        # Write 2 initial turns
        turns_batch1 = [
            _user_text_turn("first turn"),
            _bash_turn("ls"),
        ]
        path = _write_transcript(tmp_path, turns_batch1)
        result1 = run_live_mode(str(path))
        offset_after_batch1 = result1["next_byte_offset"]

        # Append 2 more turns
        with open(path, "a") as f:
            for t in [_bash_turn("git rm src/foo.py"), _user_text_turn("done")]:
                f.write(json.dumps(t) + "\n")

        # Resume from offset
        result2 = run_live_mode(str(path), since_byte=offset_after_batch1)

        assert result2["turns_read"] == 2
        assert any(f["category"] == "git_rm_usage" for f in result2["findings"])

    def test_nonexistent_file_returns_since_byte(self, tmp_path):
        turns, offset = iter_turns_from_offset("/tmp/nonexistent_transcript_abc123.output", 42)
        assert turns == []
        assert offset == 42

    def test_partial_last_line_not_included(self, tmp_path):
        """A partial last line (no newline) should be skipped."""
        turns = [_user_text_turn("complete line")]
        path = _write_transcript(tmp_path, turns)
        # Append an incomplete JSON object (simulates transcript still being written)
        with open(path, "a") as f:
            f.write('{"role": "assistant", "content": [{"type": "text", "text":')
            # No closing brace, no newline — partial line

        result = run_live_mode(str(path))
        # Should have read only the 1 complete turn
        assert result["turns_read"] == 1
        # next_byte_offset should point to end of the complete line (not the partial)
        complete_line_end = path.read_text().index("\n") + 1
        assert result["next_byte_offset"] == complete_line_end


# ---------------------------------------------------------------------------
# 7. Empty transcript / edge cases
# ---------------------------------------------------------------------------

class TestLiveEdgeCases:
    def test_empty_transcript(self, tmp_path):
        path = _write_transcript(tmp_path, [])
        result = run_live_mode(str(path))

        assert result["findings"] == []
        assert result["next_byte_offset"] == 0
        assert result["turns_read"] == 0

    def test_single_clean_turn(self, tmp_path):
        path = _write_transcript(tmp_path, [_user_text_turn("hello world")])
        result = run_live_mode(str(path))

        assert result["findings"] == []
        assert result["turns_read"] == 1

    def test_result_is_json_serializable(self, tmp_path):
        turns = [
            _bash_turn("git rm backend/x.py"),
            _agent_turn_with_general_purpose("work"),
        ]
        path = _write_transcript(tmp_path, turns)
        result = run_live_mode(str(path))

        # Should not raise
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert "findings" in parsed
        assert "next_byte_offset" in parsed


# ---------------------------------------------------------------------------
# 8. CLI integration — --live flag via subprocess
# ---------------------------------------------------------------------------

class TestLiveCLI:
    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        result = subprocess.run(
            [sys.executable, "backend/run_analyst.py", *args],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        return result.returncode, result.stdout, result.stderr

    def test_live_flag_requires_transcript(self):
        rc, stdout, stderr = self._run_cli("--live")
        assert rc != 0
        assert "transcript" in stderr.lower()

    def test_live_flag_with_transcript_produces_json(self, tmp_path):
        path = _write_transcript(tmp_path, [_user_text_turn("hello")])
        rc, stdout, stderr = self._run_cli(
            "--live", "--transcript", str(path)
        )
        assert rc == 0, f"stderr: {stderr}"
        parsed = json.loads(stdout)
        assert "findings" in parsed
        assert "next_byte_offset" in parsed
        assert "classifiers_run" in parsed

    def test_live_flag_with_since_byte(self, tmp_path):
        turns = [_bash_turn("git rm old.py"), _user_text_turn("done")]
        path = _write_transcript(tmp_path, turns)
        file_size = path.stat().st_size

        # Read from end of file — should find nothing new
        rc, stdout, stderr = self._run_cli(
            "--live", "--transcript", str(path), "--since-byte", str(file_size)
        )
        assert rc == 0
        parsed = json.loads(stdout)
        assert parsed["findings"] == []
        assert parsed["turns_read"] == 0

    def test_allow_partial_flag_accepted(self, tmp_path):
        path = _write_transcript(tmp_path, [_user_text_turn("x")])
        rc, stdout, stderr = self._run_cli(
            "--live", "--transcript", str(path), "--allow-partial"
        )
        assert rc == 0

    def test_existing_since_flag_still_works(self, tmp_path):
        """--since=4h should still run without errors (regression check)."""
        # Patch out the slow data loaders by running with --dry-run
        rc, stdout, stderr = self._run_cli("--since=4h", "--dry-run")
        # dry-run prints a JSON report — should succeed
        assert rc == 0, f"stderr: {stderr}"
        parsed = json.loads(stdout)
        assert "findings" in parsed
