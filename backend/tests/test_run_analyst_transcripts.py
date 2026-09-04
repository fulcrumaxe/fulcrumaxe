"""Tests for Phase A.2 transcript-anomaly classifiers (Discussion #486).

All tests use fixture JSONL files under backend/tests/fixtures/transcripts/.
No LLM calls, no gh API calls, no subprocess side-effects.

HARD RULE: These tests MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts"

# Add parent dirs to path so we can import transcript_reader + run_analyst
sys.path.insert(0, str(Path(__file__).parent.parent))

from transcript_reader import iter_turns, agent_id_from_path, TranscriptTurn, _extract_content
from testsupport.transcript_fixtures import render_fixture
from run_analyst import (
    _scan_transcripts,
    classify_wrong_premise_retries,
    classify_forbidden_subagent_type,
    classify_team_lead_self_edit,
    classify_auth_leak_risk,
    classify_permission_seeking,
    classify_repeated_file_reads,
)


# ---------------------------------------------------------------------------
# Helper: load fixture into transcript state list using glob_pattern override
# ---------------------------------------------------------------------------

def _states_from_fixture(fixture_name: str) -> list[dict]:
    """Load a single fixture file through _scan_transcripts using a direct glob."""
    path = FIXTURES_DIR / fixture_name
    return _states_from_path(path)


def _states_from_path(path: Path) -> list[dict]:
    """Build transcript state dicts from a single fixture file path.

    Rendering happens here rather than at each call site so a fixture that grows
    a placeholder later is picked up without anyone remembering to wire it.
    """
    path = render_fixture(path)
    # We call _scan_transcripts with glob_pattern pointing at this specific file
    # by importing transcript_reader directly and building state manually.
    from transcript_reader import iter_turns as _iter_turns, agent_id_from_path as _agent_id
    from collections import Counter, defaultdict

    agent_id = _agent_id(path)
    state: dict = {
        "path": str(path),
        "agent_id": agent_id,
        "turns": 0,
        "is_team_lead": False,
        "tool_call_counter": Counter(),
        "tool_call_turns": defaultdict(list),
        "has_edit_write": False,
        "auth_leak_cmds": [],
        "permission_phrases": [],
        "repeated_reads": Counter(),
        "read_turns": defaultdict(list),
        "write_paths": set(),
        "general_purpose_hits": [],
        "wrong_premise_err_turns": [],
        "prev_user_text": "",
    }

    import re
    _TEAM_LEAD_KEYWORDS = re.compile(
        r"(?:you are the team lead|team lead operating protocol|"
        r"identity.*team lead|team-lead.*coordinator|"
        r"single.spawner invariant)",
        re.IGNORECASE,
    )
    _EDIT_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
    _RE_AUTH_LEAK = re.compile(
        r"(?:"
        r"\bcurl\s+(?:[^\n]*\s)?-[a-zA-Z]*v\b"
        r"|\bcurl\s+(?:[^\n]*\s)?--verbose\b"
        r"|\bset\s+-x\b"
        r"|\bgh\s+api\b[^\n]*--verbose"
        r"|\bvastai\b[^\n]*(?:--explain|--curl)"
        r")",
        re.IGNORECASE,
    )
    _RE_GENERAL_PURPOSE = re.compile(
        r"(?:subagent_type|subagent-type)[\"'\s:=]*general.?purpose",
        re.IGNORECASE,
    )
    _RE_PERMISSION_SEEKING = re.compile(
        r"\b(?:should\s+i|do\s+you\s+want\s+me\s+to|let\s+me\s+know\s+if|"
        r"shall\s+i|would\s+you\s+like\s+me\s+to)\b",
        re.IGNORECASE,
    )
    _RE_WRONG_PREMISE = re.compile(
        r"(?i)(no such file|file not found|command not found|does not exist|"
        r"cannot find|not a valid|unrecognized|unexpected error|traceback)",
    )

    for turn in _iter_turns(path):
        state["turns"] += 1

        if turn.turn_idx < 3 and turn.role in ("user", "system"):
            if _TEAM_LEAD_KEYWORDS.search(turn.text):
                state["is_team_lead"] = True

        for tc in turn.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            import json as _json
            if name == "Bash":
                norm_key = re.sub(r"\s+", " ", inp.get("command", "")[:80]).strip()
            elif name == "Read":
                norm_key = inp.get("file_path", inp.get("path", ""))
            else:
                norm_key = _json.dumps(inp, sort_keys=True)[:80]

            tc_key = (name, norm_key)
            state["tool_call_counter"][tc_key] += 1
            state["tool_call_turns"][tc_key].append(turn.turn_idx)

            if name in _EDIT_WRITE_TOOLS:
                state["has_edit_write"] = True
                fp = inp.get("file_path", inp.get("path", ""))
                if fp:
                    state["write_paths"].add(fp)

            if name == "Read":
                fp = inp.get("file_path", inp.get("path", ""))
                if fp:
                    state["repeated_reads"][fp] += 1
                    state["read_turns"][fp].append(turn.turn_idx)

            if name == "Bash":
                cmd = inp.get("command", "")
                if _RE_AUTH_LEAK.search(cmd):
                    state["auth_leak_cmds"].append(cmd[:120])

            if name in ("Bash", "Agent"):
                target_text = inp.get("command", "") or inp.get("prompt", "") or _json.dumps(inp)
                if _RE_GENERAL_PURPOSE.search(target_text):
                    state["general_purpose_hits"].append(target_text[:100])

        for tr in turn.tool_results:
            content = tr.get("content", "")
            is_error = tr.get("is_error", False)
            if is_error or _RE_WRONG_PREMISE.search(str(content)):
                tool_use_id = tr.get("tool_use_id", "")
                matched_name = ""
                matched_key = ""
                for tc in turn.tool_calls:
                    if tc.get("id") == tool_use_id or not tool_use_id:
                        matched_name = tc.get("name", "")
                        inp2 = tc.get("input", {})
                        if matched_name == "Bash":
                            matched_key = inp2.get("command", "")[:60]
                        elif matched_name == "Read":
                            matched_key = inp2.get("file_path", "")[:60]
                        else:
                            matched_key = _json.dumps(inp2)[:60]
                        break
                state["wrong_premise_err_turns"].append(
                    (turn.turn_idx, matched_name or "unknown", matched_key)
                )

        if turn.role == "assistant" and _RE_PERMISSION_SEEKING.search(turn.text):
            prev = state.get("prev_user_text", "")
            if not prev.rstrip().endswith("?"):
                state["permission_phrases"].append((turn.turn_idx, turn.text[:100]))

        if turn.role == "user":
            state["prev_user_text"] = turn.text

    return [state]


# ---------------------------------------------------------------------------
# transcript_reader unit tests
# ---------------------------------------------------------------------------

class TestTranscriptReader(unittest.TestCase):

    def test_iter_turns_yields_turns(self):
        """iter_turns parses a valid JSONL fixture and yields TranscriptTurn objects."""
        path = FIXTURES_DIR / "auth_leak_positive.jsonl"
        turns = list(iter_turns(path))
        self.assertGreater(len(turns), 0)
        for t in turns:
            self.assertIsInstance(t, TranscriptTurn)

    def test_iter_turns_empty_file(self, tmp_path=None):
        """iter_turns on an empty file yields nothing (no crash)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".output", mode="w", delete=False) as f:
            tmp = Path(f.name)
        turns = list(iter_turns(tmp))
        self.assertEqual(turns, [])
        tmp.unlink(missing_ok=True)

    def test_iter_turns_malformed_line_skipped(self):
        """iter_turns skips malformed JSONL lines without crashing."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".output", mode="w", delete=False) as f:
            f.write('{"type": "message", "message": {"role": "user", "content": "ok"}}\n')
            f.write('INVALID JSON LINE\n')
            f.write('{"type": "message", "message": {"role": "assistant", "content": "done"}}\n')
            tmp = Path(f.name)
        turns = list(iter_turns(tmp))
        self.assertEqual(len(turns), 2)
        tmp.unlink(missing_ok=True)

    def test_extract_content_str(self):
        """_extract_content handles plain string content."""
        text, calls, results = _extract_content("hello world")
        self.assertEqual(text, "hello world")
        self.assertEqual(calls, [])
        self.assertEqual(results, [])

    def test_extract_content_blocks(self):
        """_extract_content normalizes content block list."""
        blocks = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash", "id": "x1", "input": {"command": "ls"}},
            {"type": "tool_result", "tool_use_id": "x1", "content": "file1"},
        ]
        text, calls, results = _extract_content(blocks)
        self.assertEqual(text, "hello")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "Bash")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_use_id"], "x1")

    def test_agent_id_from_path(self):
        """agent_id_from_path extracts stem from path."""
        p = Path("/tmp/claude-abc/-home-agent-fulcrumaxe/uuid/tasks/agent-123.output")
        self.assertEqual(agent_id_from_path(p), "agent-123")

    def test_iter_turns_string_message_no_crash(self):
        """iter_turns does not crash when obj['message'] is a string (real-world shape).

        Covers two known string-message shapes:
          {"type": "system", "message": "Loaded session abc123", ...}
          {"file": "/ideas", "line": null, "severity": "medium", "message": "..."}
        Both must be silently tolerated — turns are yielded for lines that have parseable
        role/content at the top level, or skipped if not; the key invariant is no crash.
        """
        path = FIXTURES_DIR / "string_message_positive.jsonl"
        # Must not raise AttributeError
        turns = list(iter_turns(path))
        # The fixture has 4 lines; 2 have nested message dicts with role=assistant/user
        # The other 2 have string messages — they fall back to obj which has no role="" so
        # they still yield turns (role="" or role from obj), or at minimum do not crash.
        self.assertIsInstance(turns, list)
        for t in turns:
            self.assertIsInstance(t, TranscriptTurn)

    def test_iter_turns_string_message_negative_normal(self):
        """iter_turns handles a normal transcript (no string-message lines) without regression."""
        path = FIXTURES_DIR / "string_message_negative.jsonl"
        turns = list(iter_turns(path))
        # 3 lines: 1 system (no nested message dict, role=""), 2 message lines with roles
        self.assertGreater(len(turns), 0)
        roles = [t.role for t in turns]
        self.assertIn("assistant", roles)
        self.assertIn("user", roles)


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------

class TestClassifyWrongPremiseRetries(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires when an agent retries same failing command 5+ times."""
        states = _states_from_fixture("wrong_premise_positive.jsonl")
        findings = classify_wrong_premise_retries(states)
        self.assertGreater(len(findings), 0, "Expected a finding for 6 repeated error turns")
        self.assertEqual(findings[0]["category"], "wrong_premise_retries")
        self.assertEqual(findings[0]["severity"], "high")

    def test_negative_fixture_silent(self):
        """Classifier does not fire when tool calls succeed."""
        states = _states_from_fixture("wrong_premise_negative.jsonl")
        findings = classify_wrong_premise_retries(states)
        self.assertEqual(findings, [], "Expected no findings for successful run")


class TestClassifyForbiddenSubagentType(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires when general-purpose subagent_type is used in Bash."""
        states = _states_from_fixture("general_purpose_positive.jsonl")
        findings = classify_forbidden_subagent_type(states)
        self.assertGreater(len(findings), 0, "Expected a finding for general-purpose usage")
        self.assertEqual(findings[0]["category"], "forbidden_subagent_type")
        self.assertEqual(findings[0]["severity"], "high")

    def test_negative_fixture_silent(self):
        """Classifier does not fire when named role (executor) is used."""
        states = _states_from_fixture("general_purpose_negative.jsonl")
        findings = classify_forbidden_subagent_type(states)
        self.assertEqual(findings, [], "Expected no findings for named-role usage")


class TestClassifyTeamLeadSelfEdit(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires when Team Lead transcript contains Edit on project file."""
        states = _states_from_fixture("team_lead_edit_positive.jsonl")
        findings = classify_team_lead_self_edit(states)
        self.assertGreater(len(findings), 0, "Expected a finding for TL self-edit")
        self.assertEqual(findings[0]["category"], "team_lead_self_edit")
        self.assertEqual(findings[0]["severity"], "high")

    def test_negative_fixture_silent(self):
        """Classifier does not fire for executor editing project files."""
        states = _states_from_fixture("team_lead_edit_negative.jsonl")
        findings = classify_team_lead_self_edit(states)
        self.assertEqual(findings, [], "Expected no findings for executor editing files")


class TestClassifyAuthLeakRisk(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires for curl -v command."""
        states = _states_from_fixture("auth_leak_positive.jsonl")
        findings = classify_auth_leak_risk(states)
        self.assertGreater(len(findings), 0, "Expected a finding for curl -v")
        self.assertEqual(findings[0]["category"], "auth_leak_risk")
        self.assertEqual(findings[0]["severity"], "high")

    def test_negative_fixture_silent(self):
        """Classifier does not fire for curl -s (silent, no verbose)."""
        states = _states_from_fixture("auth_leak_negative.jsonl")
        findings = classify_auth_leak_risk(states)
        self.assertEqual(findings, [], "Expected no findings for curl -s")

    def test_set_x_pattern(self):
        """Classifier fires for set -x in Bash commands."""
        import json as _json
        from collections import Counter, defaultdict
        state = {
            "path": "fake.output",
            "agent_id": "test-agent",
            "turns": 2,
            "is_team_lead": False,
            "tool_call_counter": Counter(),
            "tool_call_turns": defaultdict(list),
            "has_edit_write": False,
            "auth_leak_cmds": ["set -x; echo $SECRET_TOKEN"],
            "permission_phrases": [],
            "repeated_reads": Counter(),
            "read_turns": defaultdict(list),
            "write_paths": set(),
            "general_purpose_hits": [],
            "wrong_premise_err_turns": [],
            "prev_user_text": "",
        }
        findings = classify_auth_leak_risk([state])
        self.assertGreater(len(findings), 0)


class TestClassifyPermissionSeeking(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires when assistant asks permission without prior question."""
        states = _states_from_fixture("permission_seeking_positive.jsonl")
        findings = classify_permission_seeking(states)
        self.assertGreater(len(findings), 0, "Expected a finding for permission-seeking phrase")
        self.assertEqual(findings[0]["category"], "permission_seeking")
        self.assertEqual(findings[0]["severity"], "medium")

    def test_negative_fixture_silent(self):
        """Classifier does not fire when prior user turn ends with question mark."""
        states = _states_from_fixture("permission_seeking_negative.jsonl")
        findings = classify_permission_seeking(states)
        self.assertEqual(findings, [], "Expected no findings when user asked a question first")


class TestClassifyRepeatedFileReads(unittest.TestCase):

    def test_positive_fixture_fires(self):
        """Classifier fires when same file read 4+ times without edit."""
        states = _states_from_fixture("repeated_reads_positive.jsonl")
        findings = classify_repeated_file_reads(states)
        self.assertGreater(len(findings), 0, "Expected a finding for 4 reads of same file")
        self.assertEqual(findings[0]["category"], "repeated_file_reads")
        self.assertEqual(findings[0]["severity"], "medium")

    def test_negative_fixture_silent(self):
        """Classifier does not fire when file is edited between reads."""
        states = _states_from_fixture("repeated_reads_negative.jsonl")
        findings = classify_repeated_file_reads(states)
        self.assertEqual(findings, [], "Expected no findings when file was edited between reads")

    def test_three_reads_no_fire(self):
        """Classifier does not fire for exactly 3 reads (threshold is >3)."""
        from collections import Counter, defaultdict
        state = {
            "path": "fake.output",
            "agent_id": "test-agent",
            "turns": 6,
            "is_team_lead": False,
            "tool_call_counter": Counter(),
            "tool_call_turns": defaultdict(list),
            "has_edit_write": False,
            "auth_leak_cmds": [],
            "permission_phrases": [],
            "repeated_reads": Counter({"backend/foo.py": 3}),
            "read_turns": defaultdict(list, {"backend/foo.py": [0, 2, 4]}),
            "write_paths": set(),
            "general_purpose_hits": [],
            "wrong_premise_err_turns": [],
            "prev_user_text": "",
        }
        findings = classify_repeated_file_reads([state])
        self.assertEqual(findings, [], "Should not fire for exactly 3 reads")


if __name__ == "__main__":
    unittest.main()
