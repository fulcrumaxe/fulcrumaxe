"""Unit tests for backend/hooks/wrong_premise_guard.py

Tests cover:
  - 3 identical failures trigger exactly one injection on the 3rd call
  - 2 identical + 1 different does NOT trigger
  - 5 identical failures trigger exactly one injection (not five)
  - gates.wrong_premise_guard=false suppresses injection
  - non-error tool calls (is_error=False) are ignored
  - volatile fields in error text are normalised (same effective error)

New tests (Requirements 1-4):
  - Bash tool with missing tool_name — extracts synthetic key from command
  - MCP tool with nested tool name — extracts server.toolname
  - fuzzy arg dedup — whitespace/quote/trailing-slash differences collapse
  - total-failure circuit breaker fires at limit regardless of per-key variety
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.hooks import wrong_premise_guard as wpg


def _make_ctx(
    session_id: str,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    tool_output: str = "Error: command not found",
    is_error: bool = True,
) -> dict:
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "ls /nonexistent"},
        "tool_output": tool_output,
        "is_error": is_error,
    }


class TestWrongPremiseGuard(unittest.TestCase):
    def setUp(self) -> None:
        """Use a temp dir for state files so tests don't pollute /tmp."""
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_gettempdir = tempfile.gettempdir

        # Patch _state_path to use our temp dir
        self._state_patch = patch.object(
            wpg,
            "_state_path",
            side_effect=lambda sid: Path(self._tmpdir.name) / f"wpg-{sid}.json",
        )
        self._state_patch.start()

        # Patch _get_control_plane_value so tests don't spawn subprocesses
        self._cp_patch = patch.object(
            wpg,
            "_get_control_plane_value",
            side_effect=self._cp_side_effect,
        )
        self._cp_values: dict[str, str] = {}
        self._cp_patch.start()

        # Patch _emit_team_log to avoid subprocess calls
        self._log_patch = patch.object(wpg, "_emit_team_log")
        self._log_patch.start()

    def _cp_side_effect(self, key: str, default):
        return self._cp_values.get(key, default)

    def tearDown(self) -> None:
        self._state_patch.stop()
        self._cp_patch.stop()
        self._log_patch.stop()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # AC1: 3 identical failures → exactly one injection on the 3rd call
    # ------------------------------------------------------------------

    def test_three_identical_failures_triggers_on_third(self) -> None:
        sid = "sess-ac1"
        ctx = _make_ctx(sid)

        r1 = wpg.run(ctx)
        self.assertEqual(r1, {}, "First failure should not trigger")

        r2 = wpg.run(ctx)
        self.assertEqual(r2, {}, "Second failure should not trigger")

        r3 = wpg.run(ctx)
        self.assertIn("hookSpecificOutput", r3, "Third identical failure must trigger")
        self.assertIn("directive", r3["hookSpecificOutput"])
        self.assertIn("wrong-premise-guard", r3["hookSpecificOutput"]["directive"])

    # ------------------------------------------------------------------
    # AC1b: 2 identical + 1 different does NOT trigger
    # ------------------------------------------------------------------

    def test_two_identical_plus_one_different_no_trigger(self) -> None:
        sid = "sess-ac1b"
        ctx_a = _make_ctx(sid, tool_output="Error: file not found")
        ctx_b = _make_ctx(sid, tool_output="Error: permission denied")  # different error

        wpg.run(ctx_a)
        wpg.run(ctx_a)  # 2 identical
        r = wpg.run(ctx_b)  # different error — should NOT trigger
        self.assertEqual(r, {}, "Different error should not count toward the same key")

    # ------------------------------------------------------------------
    # AC2: 5 identical failures → exactly ONE injection (not five)
    # ------------------------------------------------------------------

    def test_five_identical_failures_single_injection(self) -> None:
        sid = "sess-ac2"
        ctx = _make_ctx(sid)

        results = [wpg.run(ctx) for _ in range(5)]
        injections = [r for r in results if r]
        self.assertEqual(len(injections), 1, "Should inject exactly once across 5 identical failures")
        # Injection must be on the 3rd call (index 2)
        self.assertEqual(results[0], {})
        self.assertEqual(results[1], {})
        self.assertIn("hookSpecificOutput", results[2])
        self.assertEqual(results[3], {})
        self.assertEqual(results[4], {})

    # ------------------------------------------------------------------
    # AC3: gate=false → no injection
    # ------------------------------------------------------------------

    def test_gate_false_suppresses_injection(self) -> None:
        self._cp_values["gates.wrong_premise_guard"] = "false"
        sid = "sess-ac3"
        ctx = _make_ctx(sid)

        results = [wpg.run(ctx) for _ in range(5)]
        self.assertTrue(
            all(r == {} for r in results),
            "With gate=false, no injection should occur",
        )

    # ------------------------------------------------------------------
    # Non-error calls are ignored
    # ------------------------------------------------------------------

    def test_non_error_calls_ignored(self) -> None:
        sid = "sess-noe"
        ctx = _make_ctx(sid, is_error=False)
        results = [wpg.run(ctx) for _ in range(5)]
        self.assertTrue(all(r == {} for r in results))

    # ------------------------------------------------------------------
    # Volatile fields in error text are normalised
    # ------------------------------------------------------------------

    def test_volatile_fields_normalised(self) -> None:
        """Two errors differing only by UUID/timestamp should hash to the same key."""
        sid = "sess-vol"
        err_a = "Error: request-id: 11111111-2222-3333-4444-555555555555 file not found"
        err_b = "Error: request-id: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee file not found"

        ctx_a = _make_ctx(sid, tool_output=err_a)
        ctx_b = _make_ctx(sid, tool_output=err_b)

        key_a = wpg._make_key("Bash", {"command": "ls /nonexistent"}, err_a)
        key_b = wpg._make_key("Bash", {"command": "ls /nonexistent"}, err_b)
        self.assertEqual(key_a, key_b, "Keys should match after volatile-field normalisation")

        wpg.run(ctx_a)
        wpg.run(ctx_a)
        r3 = wpg.run(ctx_b)  # different UUID but same normalised error → should trigger
        self.assertIn("hookSpecificOutput", r3)

    # ------------------------------------------------------------------
    # Excluded roles are ignored
    # ------------------------------------------------------------------

    def test_excluded_role_ignored(self) -> None:
        sid = "sess-excl"
        ctx = _make_ctx(sid)
        ctx["agent_role"] = "browser-tester"
        results = [wpg.run(ctx) for _ in range(5)]
        self.assertTrue(all(r == {} for r in results))

    # ------------------------------------------------------------------
    # Custom retry limit via control plane
    # ------------------------------------------------------------------

    def test_custom_retry_limit(self) -> None:
        self._cp_values["policies.agents.wrong_premise_retry_limit"] = "2"
        sid = "sess-custom"
        ctx = _make_ctx(sid)

        r1 = wpg.run(ctx)
        self.assertEqual(r1, {})
        r2 = wpg.run(ctx)
        self.assertIn("hookSpecificOutput", r2, "Should trigger on 2nd call with limit=2")

    # ------------------------------------------------------------------
    # Directive content includes tool name and limit
    # ------------------------------------------------------------------

    def test_directive_content(self) -> None:
        sid = "sess-content"
        ctx = _make_ctx(sid, tool_name="Read")

        wpg.run(ctx)
        wpg.run(ctx)
        r = wpg.run(ctx)
        directive = r["hookSpecificOutput"]["directive"]
        self.assertIn("Read", directive)
        self.assertIn("3", directive)
        self.assertIn("needs-fix", directive)

    # ------------------------------------------------------------------
    # Requirement 1: Bash tool with missing tool_name
    # Extract synthetic key from command, not collapse to single "unknown"
    # ------------------------------------------------------------------

    def test_bash_missing_tool_name_uses_command_bucket(self) -> None:
        """When tool_name is absent, two different Bash commands produce different keys."""
        input_a = {"command": "ls /nonexistent"}
        input_b = {"command": "cat /missing/file"}

        key_a = wpg._make_key(wpg._extract_tool_name({"tool_input": input_a}), input_a, "Error: not found")
        key_b = wpg._make_key(wpg._extract_tool_name({"tool_input": input_b}), input_b, "Error: not found")

        self.assertNotEqual(key_a, key_b, "Different Bash commands must not collide in the unknown bucket")

    def test_bash_missing_tool_name_same_command_collides(self) -> None:
        """Same Bash command with missing tool_name still deduplicates correctly."""
        sid = "sess-bash-missing"
        ctx = {
            "session_id": sid,
            # tool_name intentionally absent
            "tool_input": {"command": "git status"},
            "tool_output": "Error: not a git repo",
            "is_error": True,
        }
        results = [wpg.run(ctx) for _ in range(3)]
        self.assertIn(
            "hookSpecificOutput", results[2],
            "Guard must trigger on 3rd identical missing-tool_name failure"
        )
        self.assertFalse(results[2].get("hookSpecificOutput", {}).get("circuit_breaker"),
                         "Should be per-key trigger, not circuit breaker")

    def test_bash_missing_tool_name_10_retries_trigger_by_3(self) -> None:
        """Simulate 10-retry sequence with tool=unknown — guard triggers by retry 3."""
        sid = "sess-unknown-10"
        ctx = {
            "session_id": sid,
            "tool_name": "",  # explicitly empty
            "tool_input": {"command": "python3 run.py"},
            "tool_output": "Error: ModuleNotFoundError",
            "is_error": True,
        }
        results = [wpg.run(ctx) for _ in range(10)]
        # Should have triggered at index 2 (3rd call)
        self.assertEqual(results[0], {})
        self.assertEqual(results[1], {})
        self.assertIn("hookSpecificOutput", results[2], "Guard must fire by retry 3")

    # ------------------------------------------------------------------
    # Requirement 2: MCP tool with nested tool name
    # ------------------------------------------------------------------

    def test_mcp_tool_nested_name_extracted(self) -> None:
        """MCP tools with tool.name nested in context are handled."""
        ctx = {
            "session_id": "sess-mcp",
            # tool_name is absent for MCP tools; name lives in tool.name
            "tool": {"name": "search_files"},
            "server_name": "filesystem",
            "tool_input": {"query": "*.py"},
            "tool_output": "Error: permission denied",
            "is_error": True,
        }
        extracted = wpg._extract_tool_name(ctx)
        self.assertEqual(extracted, "filesystem.search_files")

    def test_mcp_tool_no_server_name(self) -> None:
        """MCP tools without server_name still extract the nested tool name."""
        ctx = {
            "session_id": "sess-mcp-no-server",
            "tool": {"name": "list_directory"},
            "tool_input": {"path": "/tmp"},
            "tool_output": "Error: not found",
            "is_error": True,
        }
        extracted = wpg._extract_tool_name(ctx)
        self.assertEqual(extracted, "list_directory")

    def test_mcp_tool_dedup_fires_correctly(self) -> None:
        """MCP tool failures with nested name trigger guard after 3 identical failures."""
        sid = "sess-mcp-dedup"
        ctx = {
            "session_id": sid,
            "tool": {"name": "read_file"},
            "server_name": "fs",
            "tool_input": {"path": "/nonexistent/file.txt"},
            "tool_output": "Error: ENOENT",
            "is_error": True,
        }
        results = [wpg.run(ctx) for _ in range(3)]
        self.assertEqual(results[0], {})
        self.assertEqual(results[1], {})
        self.assertIn("hookSpecificOutput", results[2])
        self.assertEqual(results[2]["hookSpecificOutput"]["tool_name"], "fs.read_file")

    # ------------------------------------------------------------------
    # Requirement 3: Fuzzy arg dedup
    # ------------------------------------------------------------------

    def test_fuzzy_whitespace_collapses(self) -> None:
        """Args differing only in whitespace produce the same key."""
        input_a = {"command": "git   commit  -m 'fix'"}
        input_b = {"command": "git commit -m 'fix'"}
        key_a = wpg._make_key("Bash", input_a, "Error: nothing to commit")
        key_b = wpg._make_key("Bash", input_b, "Error: nothing to commit")
        self.assertEqual(key_a, key_b, "Whitespace differences should not split the dedup key")

    def test_fuzzy_trailing_slash_collapses(self) -> None:
        """Args differing only in trailing slash produce the same key."""
        input_a = {"file_path": "/home/user/project/"}
        input_b = {"file_path": "/home/user/project"}
        key_a = wpg._make_key("Edit", input_a, "Error: file not found")
        key_b = wpg._make_key("Edit", input_b, "Error: file not found")
        self.assertEqual(key_a, key_b, "Trailing slash should not split the dedup key")

    def test_fuzzy_quote_style_collapses(self) -> None:
        """Args differing only in quote style produce the same key."""
        input_a = {"command": 'git commit -m "fix"'}
        input_b = {"command": "git commit -m 'fix'"}
        key_a = wpg._make_key("Bash", input_a, "Error: nothing to commit")
        key_b = wpg._make_key("Bash", input_b, "Error: nothing to commit")
        self.assertEqual(key_a, key_b, "Quote-style differences should not split the dedup key")

    def test_fuzzy_dedup_fires_across_quote_variations(self) -> None:
        """Guard triggers when an agent retries with quote-style variation."""
        sid = "sess-fuzzy-quotes"
        ctx_a = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "add feature"'},
            "tool_output": "Error: nothing to commit",
            "is_error": True,
        }
        ctx_b = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'add feature'"},
            "tool_output": "Error: nothing to commit",
            "is_error": True,
        }
        wpg.run(ctx_a)
        wpg.run(ctx_a)
        r = wpg.run(ctx_b)  # same command with different quotes → should trigger
        self.assertIn("hookSpecificOutput", r, "Fuzzy quote dedup should trigger on 3rd retry")

    # ------------------------------------------------------------------
    # Requirement 4: Total-failure circuit breaker
    # ------------------------------------------------------------------

    def test_circuit_breaker_fires_at_total_limit(self) -> None:
        """Circuit breaker fires after 15 total failures regardless of tool variety."""
        self._cp_values["policies.agents.wrong_premise_total_limit"] = "15"
        sid = "sess-cb"

        # Use 15 distinct (tool, args, error) combos so per-key dedup never fires
        results = []
        for i in range(16):
            ctx = {
                "session_id": sid,
                "tool_name": f"Tool{i}",  # each call has a unique tool name
                "tool_input": {"arg": f"value{i}"},
                "tool_output": f"Error: variant {i}",
                "is_error": True,
            }
            results.append(wpg.run(ctx))

        # First 14 should not trigger circuit breaker (per-key limit=3, each key seen once)
        for i in range(14):
            self.assertEqual(
                results[i], {},
                f"Call {i+1} should not trigger (below total_limit=15)"
            )

        # 15th call should trip the circuit breaker
        self.assertIn(
            "hookSpecificOutput", results[14],
            "Circuit breaker must fire at failure 15"
        )
        self.assertTrue(
            results[14]["hookSpecificOutput"].get("circuit_breaker"),
            "Circuit-breaker output must set circuit_breaker=True"
        )
        directive = results[14]["hookSpecificOutput"]["directive"]
        self.assertIn("step back", directive.lower(), "Circuit-breaker directive must say step back")

        # 16th call should be silent (circuit already tripped)
        self.assertEqual(results[15], {}, "After circuit trip, subsequent calls must be silent")

    def test_circuit_breaker_custom_limit(self) -> None:
        """Circuit breaker respects custom total_limit from control plane."""
        self._cp_values["policies.agents.wrong_premise_total_limit"] = "5"
        sid = "sess-cb-custom"

        results = []
        for i in range(6):
            ctx = {
                "session_id": sid,
                "tool_name": f"UniqueToolX{i}",
                "tool_input": {"x": i},
                "tool_output": f"Error unique {i}",
                "is_error": True,
            }
            results.append(wpg.run(ctx))

        # First 4 silent
        for i in range(4):
            self.assertEqual(results[i], {})
        # 5th trips breaker
        self.assertIn("hookSpecificOutput", results[4])
        self.assertTrue(results[4]["hookSpecificOutput"].get("circuit_breaker"))

    def test_simulate_16_failure_varied_tools_circuit_breaker(self) -> None:
        """AC4: Simulate 16-failure sequence with varied tools — circuit breaker fires at 15."""
        # Set per-key limit high so only circuit breaker can trigger
        self._cp_values["policies.agents.wrong_premise_retry_limit"] = "100"
        self._cp_values["policies.agents.wrong_premise_total_limit"] = "15"
        sid = "sess-ac4"

        fired_at = None
        for i in range(16):
            ctx = {
                "session_id": sid,
                "tool_name": f"DistinctTool{i % 8}",  # rotate through 8 tools
                "tool_input": {"step": i, "variant": f"v{i}"},
                "tool_output": f"SomeError variant={i}",
                "is_error": True,
            }
            result = wpg.run(ctx)
            if result and result.get("hookSpecificOutput", {}).get("circuit_breaker"):
                fired_at = i + 1  # 1-indexed
                break

        self.assertIsNotNone(fired_at, "Circuit breaker must fire within 16 failures")
        self.assertEqual(fired_at, 15, f"Circuit breaker should fire at failure 15, not {fired_at}")


class TestExtractToolName(unittest.TestCase):
    """Unit tests for _extract_tool_name helper."""

    def test_direct_tool_name_returned(self) -> None:
        ctx = {"tool_name": "Bash", "tool_input": {}}
        self.assertEqual(wpg._extract_tool_name(ctx), "Bash")

    def test_empty_tool_name_falls_to_input(self) -> None:
        ctx = {"tool_name": "", "tool_input": {"command": "grep foo bar"}}
        result = wpg._extract_tool_name(ctx)
        self.assertTrue(result.startswith("bash:"), f"Expected bash: prefix, got {result}")

    def test_none_tool_name_falls_to_input(self) -> None:
        ctx = {"tool_name": None, "tool_input": {"file_path": "/src/main.py"}}
        result = wpg._extract_tool_name(ctx)
        self.assertTrue(result.startswith("edit:"), f"Expected edit: prefix, got {result}")

    def test_mcp_nested_with_server(self) -> None:
        ctx = {"tool": {"name": "list_files"}, "server_name": "myserver", "tool_input": {}}
        self.assertEqual(wpg._extract_tool_name(ctx), "myserver.list_files")

    def test_mcp_nested_without_server(self) -> None:
        ctx = {"tool": {"name": "search"}, "tool_input": {}}
        self.assertEqual(wpg._extract_tool_name(ctx), "search")

    def test_tool_use_id_prefix_extraction(self) -> None:
        ctx = {"tool_use_id": "toolu_bash_xyz123", "tool_input": {}}
        result = wpg._extract_tool_name(ctx)
        self.assertEqual(result, "bash")

    def test_fully_unknown_falls_back(self) -> None:
        ctx = {"tool_input": {}}
        self.assertEqual(wpg._extract_tool_name(ctx), "unknown")


class TestFuzzyNormalize(unittest.TestCase):
    """Unit tests for _fuzzy_normalize_args."""

    def test_whitespace_collapsed(self) -> None:
        a = wpg._fuzzy_normalize_args({"cmd": "a  b  c"})
        b = wpg._fuzzy_normalize_args({"cmd": "a b c"})
        self.assertEqual(a, b)

    def test_trailing_slash_removed(self) -> None:
        a = wpg._fuzzy_normalize_args({"path": "/tmp/dir/"})
        b = wpg._fuzzy_normalize_args({"path": "/tmp/dir"})
        self.assertEqual(a, b)

    def test_quote_normalised(self) -> None:
        a = wpg._fuzzy_normalize_args({"cmd": "echo 'hello'"})
        b = wpg._fuzzy_normalize_args({"cmd": 'echo "hello"'})
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
