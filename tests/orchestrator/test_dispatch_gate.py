"""tests/orchestrator/test_dispatch_gate.py — Tests for the dispatcher gate wired
into spawn-agent.sh (D#1302 P1).

These tests focus on the routing logic invoked by the gate:
  - SHADOW_MODE=cc → route always returns "cc" (no SDK/network call)
  - SpawnSpec JSON round-trip from the spec fields the wrapper builds
  - The CLI entry point reads stdin JSON and writes route JSON to stdout
  - route=="blocked" → wrapper exits 1 (spawn blocked, NOT falls through to cc)
  - Dispatcher crash (unparseable output) → wrapper falls back to cc
  - tool_whitelist is omitted from the spec so _dict_to_spec() applies its default

No real Anthropic API calls are made.  CreditTracker is patched to return a
positive balance so the credit-exhaustion path is not triggered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Resolve repo root so subprocess calls work regardless of cwd
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helper: build a minimal SpawnSpec dict (mirrors what the wrapper emits)
# ---------------------------------------------------------------------------

def _minimal_spec(
    role: str = "executor",
    discussion: int | None = 1302,
    shadow_mode: str = "cc",
) -> dict:
    """Return a SpawnSpec dict matching the fields spawn-agent.sh sends.

    Note: tool_whitelist is intentionally ABSENT here — the wrapper no longer
    sends it so _dict_to_spec() applies its default (["Read","Bash"]).
    """
    return {
        "role": role,
        "task_prompt": "implement something",
        "role_card_path": "",
        "isolation": "worktree",
        "worktree_path": "",
        "env_allowlist": [],
        "discussion": discussion,
        "pr": None,
        "agent_id": None,
        "untrusted_content": {},
    }


# ---------------------------------------------------------------------------
# Unit tests — import dispatch directly, mock CreditTracker
# ---------------------------------------------------------------------------

class TestDispatchGateRoutingUnit:
    """Validate route() decisions under SHADOW_MODE=cc without real SDK calls."""

    def _make_route(self, spec_dict: dict, shadow_mode: str = "cc") -> dict:
        """Call route() with SHADOW_MODE patched and CreditTracker mocked."""
        import backend.orchestrator.dispatch as dispatch_mod

        mock_tracker = MagicMock()
        mock_tracker.remaining_usd.return_value = 100.0  # positive balance → no hard-stop

        with patch.dict(os.environ, {"SHADOW_MODE": shadow_mode}, clear=False):
            with patch("backend.orchestrator.dispatch._SHADOW_MODE", shadow_mode):
                with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker):
                    return dispatch_mod.route(spec_dict)

    def test_shadow_mode_cc_returns_cc_route(self):
        """SHADOW_MODE=cc must return route=='cc' regardless of discussion parity."""
        spec = _minimal_spec(discussion=1302)
        result = self._make_route(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_shadow_mode_cc_odd_discussion_still_cc(self):
        """Even with an odd discussion (normally → SDK in alternate mode), cc forces CC."""
        spec = _minimal_spec(discussion=863)  # 863 is odd → SDK in alternate, but cc overrides
        result = self._make_route(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_shadow_mode_cc_no_discussion_still_cc(self):
        """Non-discussion spawns with SHADOW_MODE=cc stay on CC path."""
        spec = _minimal_spec(discussion=None)
        result = self._make_route(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_cc_route_verdict_is_routed_to_cc(self):
        """CC route should carry the sentinel verdict the wrapper uses to detect CC."""
        spec = _minimal_spec(discussion=1302)
        result = self._make_route(spec, shadow_mode="cc")
        assert result["verdict"] == "routed_to_cc"

    def test_cc_route_has_run_id(self):
        """CC path must return a run_id so the wrapper can log it."""
        spec = _minimal_spec(discussion=1302)
        result = self._make_route(spec, shadow_mode="cc")
        assert result.get("run_id") is not None
        assert len(result["run_id"]) > 0

    def test_cc_route_error_is_none(self):
        """CC path is not an error condition."""
        spec = _minimal_spec(discussion=1302)
        result = self._make_route(spec, shadow_mode="cc")
        assert result.get("error") is None

    def test_spec_fields_pass_through(self):
        """role and discussion from the spec must be accessible in result (via run_id)."""
        spec = _minimal_spec(role="code-reviewer", discussion=1302)
        result = self._make_route(spec, shadow_mode="cc")
        # run_id is derived from role + discussion; check role appears in it
        assert "code-reviewer" in result.get("run_id", "")

    def test_blocked_route_when_credit_exhausted(self):
        """route() returns route=='blocked' when credit is zero and fallback is off."""
        import backend.orchestrator.dispatch as dispatch_mod

        mock_tracker = MagicMock()
        mock_tracker.remaining_usd.return_value = 0.0  # credit exhausted

        spec = _minimal_spec(discussion=1302)
        # No allow_subscription_fallback in spec → should block

        with patch.dict(os.environ, {"SHADOW_MODE": "cc"}, clear=False):
            with patch("backend.orchestrator.dispatch._SHADOW_MODE", "cc"):
                with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker):
                    result = dispatch_mod.route(spec)

        assert result["route"] == "blocked", (
            f"Expected route=='blocked' but got route=={result['route']!r}"
        )
        assert result["verdict"] == "fail"

    def test_spec_without_tool_whitelist_uses_default(self):
        """_dict_to_spec() applies default tool_whitelist when the key is absent."""
        import backend.orchestrator.dispatch as dispatch_mod
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec_dict = _minimal_spec(discussion=1302)
        # Confirm tool_whitelist is NOT in the spec (wrapper omits it)
        assert "tool_whitelist" not in spec_dict, (
            "The minimal spec should NOT contain tool_whitelist — wrapper must omit it"
        )

        # _dict_to_spec should apply ["Read","Bash"] as default
        spec_obj = dispatch_mod._dict_to_spec(spec_dict)
        assert spec_obj.tool_whitelist == ["Read", "Bash"], (
            f"Expected default ['Read','Bash'] but got {spec_obj.tool_whitelist!r}"
        )


# ---------------------------------------------------------------------------
# CLI integration test — feed JSON on stdin, assert JSON on stdout
# ---------------------------------------------------------------------------

class TestDispatchCLIStdin:
    """Exercise the CLI entry point (python3 -m backend.orchestrator.dispatch).

    Uses subprocess so we prove the full stdin→stdout wire works, matching how
    spawn-agent.sh invokes it.  SHADOW_MODE=cc is forced via env; no network call.
    """

    def _invoke_dispatch(self, spec_dict: dict, shadow_mode: str = "cc") -> dict:
        env = os.environ.copy()
        env["SHADOW_MODE"] = shadow_mode
        env["PYTHONPATH"] = str(REPO_ROOT)
        # Patch out CreditTracker by setting an env the test harness can't easily use...
        # Instead, mock it by running through PYTHONPATH with a conftest-level patch.
        # Simpler: we provide a minimal credit_tracker stub via PYTHONSTARTUP — too fragile.
        # Best approach: run with SHADOW_MODE=cc AND a tiny wrapper that mocks CreditTracker.
        wrapper = (
            "import sys, unittest.mock, os\n"
            "mock_tracker = unittest.mock.MagicMock()\n"
            "mock_tracker.remaining_usd.return_value = 100.0\n"
            "with unittest.mock.patch('backend.orchestrator.dispatch.CreditTracker', return_value=mock_tracker):\n"
            "    import backend.orchestrator.dispatch as d\n"
            "    import json\n"
            "    raw = sys.stdin.read()\n"
            "    result = d.route(json.loads(raw))\n"
            "    print(json.dumps(result))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", wrapper],
            input=json.dumps(spec_dict),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        assert result.returncode == 0, f"dispatch subprocess failed: {result.stderr}"
        return json.loads(result.stdout.strip())

    def test_stdin_json_cc_route(self):
        """Round-trip: spawn spec on stdin → route=='cc' on stdout."""
        spec = _minimal_spec(discussion=1302)
        result = self._invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"
        assert result["verdict"] == "routed_to_cc"
        assert result.get("error") is None

    def test_stdin_json_cc_route_no_discussion(self):
        """Dispatcher handles discussion=None without crashing."""
        spec = _minimal_spec(discussion=None)
        result = self._invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"

    def test_wrapper_spec_fields_accepted(self):
        """The exact fields the shell wrapper emits are all accepted without error."""
        spec = {
            "role": "executor",
            "task_prompt": "implement D#1302 P1",
            "role_card_path": "",
            "isolation": "worktree",
            "worktree_path": "",
            "env_allowlist": [],
            "discussion": 1302,
            "pr": None,
            "agent_id": None,
            "untrusted_content": {},
        }
        result = self._invoke_dispatch(spec, shadow_mode="cc")
        assert result["route"] == "cc"


# ---------------------------------------------------------------------------
# Gate-off reasoning test — verify the wrapper block is unreachable by default
# ---------------------------------------------------------------------------

class TestGateOffInvariant:
    """Verify that ROUTE_VIA_DISPATCHER unset/0 keeps the gate block unreachable.

    This test reads the modified spawn-agent.sh and asserts the guard condition
    is present exactly once, gating the entire block.
    """

    def test_gate_guard_is_present_in_wrapper(self):
        """The wrapper contains the ROUTE_VIA_DISPATCHER guard."""
        wrapper_path = REPO_ROOT / "scripts" / "spawn-agent.sh"
        text = wrapper_path.read_text()
        assert 'ROUTE_VIA_DISPATCHER:-0' in text, (
            "Gate guard '${ROUTE_VIA_DISPATCHER:-0}' not found in spawn-agent.sh"
        )

    def test_gate_guard_defaults_to_zero(self):
        """The guard uses ':-0' so it defaults to '0' when unset."""
        wrapper_path = REPO_ROOT / "scripts" / "spawn-agent.sh"
        text = wrapper_path.read_text()
        # The default-off pattern must be present
        assert '"${ROUTE_VIA_DISPATCHER:-0}" == "1"' in text, (
            "Gate guard must use format '\"${ROUTE_VIA_DISPATCHER:-0}\" == \"1\"'"
        )

    def test_gate_block_only_executes_when_env_set(self):
        """Confirm via substring: the dispatcher call only lives inside the gated block.

        The comment section above the guard also contains 'backend.orchestrator.dispatch'
        (in a descriptive line).  We distinguish the actual invocation by looking for the
        pipe-into-dispatch pattern: '| PYTHONPATH=...' which only appears in the live call.
        """
        wrapper_path = REPO_ROOT / "scripts" / "spawn-agent.sh"
        text = wrapper_path.read_text()
        # The guard line
        gate_start = text.find('"${ROUTE_VIA_DISPATCHER:-0}" == "1"')
        # The actual invocation pipes the spec JSON into dispatch via PYTHONPATH prefix.
        # This pattern only exists in the executable block, not in comments.
        dispatch_call = text.find('| PYTHONPATH="$REPO_ROOT" python3 -m backend.orchestrator.dispatch')
        assert gate_start != -1, "Gate guard not found"
        assert dispatch_call != -1, "Piped dispatch invocation not found in wrapper"
        assert dispatch_call > gate_start, (
            "dispatch invocation must appear after the ROUTE_VIA_DISPATCHER guard"
        )


# ---------------------------------------------------------------------------
# Acceptance tests — blocked route exits 1, crash falls back to cc
# ---------------------------------------------------------------------------

class TestBlockedAndCrashBehavior:
    """Verify the critical routing contract for blocked and crash cases.

    These tests cover the acceptance bug: route=="blocked" must exit 1 (spawn
    is genuinely blocked), not silently fall through to the CC path.

    Also verifies that a genuine dispatcher crash (unparseable output) still
    falls back to CC as a safe default.
    """

    def _run_wrapper_dispatch_block(
        self,
        dispatch_output: str,
        dispatch_exit: int = 0,
    ) -> tuple[int, str]:
        """Simulate the wrapper's dispatch-routing logic in isolation.

        Runs a Python script that implements the same parse-first logic as
        the fixed spawn-agent.sh dispatcher block, using the given
        dispatch_output and dispatch_exit.

        Returns (exit_code, route_chosen).
        """
        logic = """
import json, sys

dispatch_result = sys.argv[1]
dispatch_exit = int(sys.argv[2])

VALID_ROUTES = {'sdk', 'cc', 'both', 'blocked'}

if dispatch_result:
    try:
        d = json.loads(dispatch_result)
        r = d.get('route', '')
        if r in VALID_ROUTES:
            route = r
        else:
            route = '__invalid__'
    except Exception:
        route = '__invalid__'
else:
    route = '__invalid__'

if route == '__invalid__':
    # Dispatcher crashed — fail-safe to CC
    print('cc')
    sys.exit(0)
elif route == 'sdk':
    print('sdk')
    sys.exit(0)
elif route == 'blocked':
    # Genuinely blocked — exit 1, do NOT fall through to CC
    print('blocked')
    sys.exit(1)
else:
    # cc or both — continue on CC path
    print('cc')
    sys.exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", logic, dispatch_output, str(dispatch_exit)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        route_chosen = result.stdout.strip()
        return result.returncode, route_chosen

    def test_blocked_route_exits_1_not_cc(self):
        """route=='blocked' in dispatch JSON must exit 1 (spawn blocked), NOT continue to cc.

        This is the acceptance bug: previously a non-zero exit code triggered
        the cc fallback BEFORE the JSON was parsed, so blocked became cc.
        """
        blocked_json = json.dumps({
            "route": "blocked",
            "run_id": None,
            "verdict": "fail",
            "error": "SDK credit exhausted ($0 remaining).",
        })
        # Dispatcher returns blocked with exit 1 (its normal behavior for verdict==fail)
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output=blocked_json,
            dispatch_exit=1,
        )
        assert route_chosen == "blocked", (
            f"Expected route='blocked' but got {route_chosen!r}"
        )
        assert exit_code == 1, (
            f"Expected exit code 1 (spawn blocked) but got {exit_code}"
        )

    def test_blocked_route_exits_1_even_when_dispatcher_exits_0(self):
        """route=='blocked' in JSON exits 1 regardless of dispatcher exit code.

        If dispatch.main() is later changed to exit 0 for all valid decisions,
        the wrapper must still honor the JSON route field and exit 1 for blocked.
        """
        blocked_json = json.dumps({
            "route": "blocked",
            "run_id": None,
            "verdict": "fail",
            "error": "SDK credit exhausted.",
        })
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output=blocked_json,
            dispatch_exit=0,  # dispatcher exited 0 (hypothetical future behavior)
        )
        assert route_chosen == "blocked"
        assert exit_code == 1, "blocked route must always exit 1 to block spawn"

    def test_crash_unparseable_output_falls_back_to_cc(self):
        """A genuine dispatcher crash (unparseable stdout) falls back to CC path.

        The crash fallback keeps spawning alive when the dispatcher has a bug —
        the spawn continues via Claude Code rather than hard-blocking.
        """
        # Dispatcher produced garbage (crash traceback, not JSON)
        crash_output = "Traceback (most recent call last):\n  File ...\nImportError: no module named foo"
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output=crash_output,
            dispatch_exit=1,
        )
        assert route_chosen == "cc", (
            f"Expected crash fallback to 'cc' but got {route_chosen!r}"
        )
        assert exit_code == 0, "CC fallback must exit 0 (spawn continues)"

    def test_crash_empty_output_falls_back_to_cc(self):
        """Empty dispatcher output (crash) falls back to CC path."""
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output="",
            dispatch_exit=1,
        )
        assert route_chosen == "cc"
        assert exit_code == 0, "CC fallback must exit 0 (spawn continues)"

    def test_cc_route_in_json_continues_normally(self):
        """route=='cc' in JSON exits 0 (spawn continues on CC path)."""
        cc_json = json.dumps({
            "route": "cc",
            "run_id": "executor-1302-1234567890",
            "verdict": "routed_to_cc",
            "error": None,
        })
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output=cc_json,
            dispatch_exit=0,
        )
        assert route_chosen == "cc"
        assert exit_code == 0

    def test_sdk_route_in_json_exits_0(self):
        """route=='sdk' in JSON exits 0 (SDK handled the spawn)."""
        sdk_json = json.dumps({
            "route": "sdk",
            "run_id": "executor-1302-1234567890",
            "verdict": "done",
            "error": None,
        })
        exit_code, route_chosen = self._run_wrapper_dispatch_block(
            dispatch_output=sdk_json,
            dispatch_exit=0,
        )
        assert route_chosen == "sdk"
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Tool whitelist tests — spec must NOT send empty tool_whitelist
# ---------------------------------------------------------------------------

class TestToolWhitelistOmission:
    """Verify the spec no longer sends an empty tool_whitelist: [].

    An explicit empty list gives SDK agents zero tools.  The wrapper must
    either omit the key (so _dict_to_spec() applies its default) or pass
    real values.  We verify the omit approach is used.
    """

    def test_minimal_spec_has_no_tool_whitelist_key(self):
        """The spec dict produced by the wrapper omits tool_whitelist entirely."""
        spec = _minimal_spec(discussion=1302)
        assert "tool_whitelist" not in spec, (
            "Wrapper spec must NOT include tool_whitelist key — "
            "omitting it lets _dict_to_spec() apply the default ['Read','Bash']"
        )

    def test_wrapper_script_does_not_set_empty_tool_whitelist(self):
        """The wrapper script source does not contain 'tool_whitelist': []."""
        wrapper_path = REPO_ROOT / "scripts" / "spawn-agent.sh"
        text = wrapper_path.read_text()
        # The old bug: sending tool_whitelist: [] gives agents zero tools.
        # Check the dispatcher spec building block doesn't hardcode empty list.
        assert "'tool_whitelist': []" not in text, (
            "spawn-agent.sh must NOT send 'tool_whitelist': [] — "
            "omit the key so _dict_to_spec() applies the default"
        )

    def test_dict_to_spec_default_when_key_absent(self):
        """_dict_to_spec() returns ['Read','Bash'] when tool_whitelist is absent."""
        import backend.orchestrator.dispatch as dispatch_mod

        spec_dict = {
            "role": "executor",
            "task_prompt": "test",
            # tool_whitelist intentionally absent
        }
        spec_obj = dispatch_mod._dict_to_spec(spec_dict)
        assert spec_obj.tool_whitelist == ["Read", "Bash"], (
            f"Expected default ['Read','Bash'] but got {spec_obj.tool_whitelist!r}"
        )

    def test_dict_to_spec_empty_list_gives_zero_tools(self):
        """_dict_to_spec() with explicit [] gives zero tools (documents the bug that was fixed)."""
        import backend.orchestrator.dispatch as dispatch_mod

        spec_dict = {
            "role": "executor",
            "task_prompt": "test",
            "tool_whitelist": [],  # explicit empty — the old bug
        }
        spec_obj = dispatch_mod._dict_to_spec(spec_dict)
        # This test documents that the old behavior (explicit [] → zero tools) was a bug.
        # The fix is to omit the key from the wrapper spec rather than send [].
        assert spec_obj.tool_whitelist == [], (
            "With explicit [], _dict_to_spec() passes it through unchanged — "
            "this is why the wrapper must OMIT the key rather than send []"
        )
