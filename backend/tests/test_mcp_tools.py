"""Tests for backend/orchestrator/mcp_tools.py.

All tests are in-process — no real network call, no OAuth token, no claude CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import asyncio

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLEAN_ENV = {"PATH": "/usr/bin", "HOME": "/tmp"}
_CWD = "/tmp/wt-test"


# ---------------------------------------------------------------------------
# Tests: build_mcp_server
# ---------------------------------------------------------------------------

class TestBuildMcpServer:
    def test_returns_mcp_sdk_server_config(self):
        """build_mcp_server returns McpSdkServerConfig typed dict."""
        from backend.orchestrator.mcp_tools import build_mcp_server
        server = build_mcp_server(
            whitelist=["Read", "Bash"],
            env=_CLEAN_ENV,
            cwd=_CWD,
        )
        assert isinstance(server, dict)
        assert server.get("type") == "sdk"
        assert "instance" in server

    def test_server_name_default_tools(self):
        from backend.orchestrator.mcp_tools import build_mcp_server
        server = build_mcp_server(whitelist=["Read"], env=_CLEAN_ENV, cwd=_CWD)
        assert server.get("name") == "tools"

    def test_custom_server_name(self):
        from backend.orchestrator.mcp_tools import build_mcp_server
        server = build_mcp_server(whitelist=["Read"], env=_CLEAN_ENV, cwd=_CWD, server_name="my-tools")
        assert server.get("name") == "my-tools"

    def test_unknown_tool_skipped(self):
        """Unknown tool names are silently skipped — build succeeds."""
        from backend.orchestrator.mcp_tools import build_mcp_server
        # Should not raise even with an unknown tool name
        server = build_mcp_server(
            whitelist=["Read", "UnknownTool", "Bash"],
            env=_CLEAN_ENV,
            cwd=_CWD,
        )
        assert server.get("type") == "sdk"

    def test_empty_whitelist_returns_server(self):
        """Empty whitelist produces a server with no tools (not an error)."""
        from backend.orchestrator.mcp_tools import build_mcp_server
        server = build_mcp_server(whitelist=[], env=_CLEAN_ENV, cwd=_CWD)
        assert server.get("type") == "sdk"

    def test_all_known_tools_accepted(self):
        """All six supported tool names build without error."""
        from backend.orchestrator.mcp_tools import build_mcp_server
        server = build_mcp_server(
            whitelist=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
            env=_CLEAN_ENV,
            cwd=_CWD,
        )
        assert server.get("type") == "sdk"


# ---------------------------------------------------------------------------
# Tests: individual tool handlers
# ---------------------------------------------------------------------------

class TestReadToolHandler:
    @pytest.mark.asyncio
    async def test_read_success(self, tmp_path):
        """Read handler returns file contents."""
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hello world")

        from backend.orchestrator.mcp_tools import _make_read_tool
        handler = _make_read_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"path": "hello.txt"})

        assert result["content"][0]["text"] == "hello world"
        assert result.get("is_error") is not True

    @pytest.mark.asyncio
    async def test_read_missing_file_returns_error(self, tmp_path):
        """Read handler returns is_error=True for missing files."""
        from backend.orchestrator.mcp_tools import _make_read_tool
        handler = _make_read_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"path": "nonexistent.txt"})

        assert result.get("is_error") is True


class TestEditToolHandler:
    @pytest.mark.asyncio
    async def test_edit_success(self, tmp_path):
        """Edit handler replaces old_string with new_string."""
        test_file = tmp_path / "edit_me.txt"
        test_file.write_text("foo bar baz")

        from backend.orchestrator.mcp_tools import _make_edit_tool
        handler = _make_edit_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({
            "path": "edit_me.txt",
            "old_string": "bar",
            "new_string": "REPLACED",
        })

        assert result.get("is_error") is not True
        assert "REPLACED" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_edit_not_found_returns_error(self, tmp_path):
        """Edit handler returns is_error when old_string not found."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("hello world")

        from backend.orchestrator.mcp_tools import _make_edit_tool
        handler = _make_edit_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({
            "path": "file.txt",
            "old_string": "DOES NOT EXIST",
            "new_string": "something",
        })

        assert result.get("is_error") is True


class TestWriteToolHandler:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        """Write handler creates a new file."""
        from backend.orchestrator.mcp_tools import _make_write_tool
        handler = _make_write_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"path": "new_file.txt", "content": "new content"})

        assert result.get("is_error") is not True
        assert (tmp_path / "new_file.txt").read_text() == "new content"


class TestBashToolHandler:
    @pytest.mark.asyncio
    async def test_bash_runs_command(self, tmp_path):
        """Bash handler executes a command and returns output."""
        from backend.orchestrator.mcp_tools import _make_bash_tool
        handler = _make_bash_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"command": "echo hello_world"})

        assert "hello_world" in result["content"][0]["text"]
        assert result.get("is_error") is not True

    @pytest.mark.asyncio
    async def test_bash_blocks_forbidden_env(self, tmp_path):
        """Bash handler returns error when env contains forbidden credential key."""
        from backend.orchestrator.mcp_tools import _make_bash_tool

        # Inject a forbidden key into the env passed to the factory
        dirty_env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-bad"}
        handler = _make_bash_tool(env=dirty_env, cwd=str(tmp_path))
        result = await handler.handler({"command": "echo hi"})

        # validate_env() inside run_bash() raises EnvLeakError → is_error
        assert result.get("is_error") is True
        assert "Security block" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_bash_blocks_claude_code_oauth_token(self, tmp_path):
        """CLAUDE_CODE_OAUTH_TOKEN is forbidden in child process env."""
        from backend.orchestrator.mcp_tools import _make_bash_tool

        dirty_env = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok-secret"}
        handler = _make_bash_tool(env=dirty_env, cwd=str(tmp_path))
        result = await handler.handler({"command": "echo hi"})

        assert result.get("is_error") is True


class TestGrepToolHandler:
    @pytest.mark.asyncio
    async def test_grep_finds_pattern(self, tmp_path):
        """Grep handler finds matching lines."""
        test_file = tmp_path / "search_me.txt"
        test_file.write_text("line one\nfoo bar\nline three")

        from backend.orchestrator.mcp_tools import _make_grep_tool
        handler = _make_grep_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"pattern": "foo", "path": "."})

        assert "foo" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_path):
        """Grep returns '(no matches)' when pattern not found."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("nothing here")

        from backend.orchestrator.mcp_tools import _make_grep_tool
        handler = _make_grep_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"pattern": "xyz_not_found", "path": str(tmp_path)})

        assert "(no matches)" in result["content"][0]["text"]


class TestGlobToolHandler:
    @pytest.mark.asyncio
    async def test_glob_finds_files(self, tmp_path):
        """Glob handler returns matching paths."""
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()

        from backend.orchestrator.mcp_tools import _make_glob_tool
        handler = _make_glob_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"pattern": "*.py"})

        text = result["content"][0]["text"]
        assert "a.py" in text
        assert "b.py" in text
        assert "c.txt" not in text

    @pytest.mark.asyncio
    async def test_glob_no_match(self, tmp_path):
        """Glob returns '(no matches)' when no files found."""
        from backend.orchestrator.mcp_tools import _make_glob_tool
        handler = _make_glob_tool(env=_CLEAN_ENV, cwd=str(tmp_path))
        result = await handler.handler({"pattern": "*.xyz_nonexistent"})

        assert "(no matches)" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Tests: whitelist enforcement (tool_proxy validation)
# ---------------------------------------------------------------------------

class TestToolProxyValidationReuse:
    def test_forbidden_env_patterns_from_tool_proxy(self):
        """_FORBIDDEN_ENV_PATTERNS from tool_proxy covers the critical keys."""
        from backend.orchestrator.tool_proxy import _FORBIDDEN_ENV_PATTERNS
        import re

        keys_to_block = [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "MY_SERVICE_API_KEY",
            "DB_SECRET",
        ]

        for key in keys_to_block:
            blocked = any(p.fullmatch(key) for p in _FORBIDDEN_ENV_PATTERNS)
            assert blocked, f"Expected {key!r} to be blocked by _FORBIDDEN_ENV_PATTERNS"

    @pytest.mark.asyncio
    async def test_any_api_key_pattern_blocked(self, tmp_path):
        """Any *_API_KEY key in env is blocked by tool_proxy validation."""
        from backend.orchestrator.mcp_tools import _make_bash_tool

        dirty_env = {"PATH": "/usr/bin", "MY_CUSTOM_API_KEY": "val"}
        handler = _make_bash_tool(env=dirty_env, cwd=str(tmp_path))
        result = await handler.handler({"command": "echo test"})

        assert result.get("is_error") is True

    def test_mcp_tools_imports_tool_proxy_directly(self):
        """mcp_tools reuses tool_proxy validation — not a reimplementation."""
        import inspect
        import backend.orchestrator.mcp_tools as mcp_mod

        source = inspect.getsource(mcp_mod)

        # Ensure it imports from tool_proxy (not re-implementing)
        assert "from backend.orchestrator.tool_proxy import" in source

        # Ensure it does NOT define its own forbidden-pattern list
        assert "_FORBIDDEN_ENV_PATTERNS" not in source
        assert "ANTHROPIC_API_KEY" not in source.split("from backend.orchestrator.tool_proxy")[1]


# ---------------------------------------------------------------------------
# Tests: no real subprocess / no real SDK call
# ---------------------------------------------------------------------------

class TestNoRealSDKCall:
    def test_build_mcp_server_no_subprocess(self):
        """build_mcp_server is pure Python — no subprocess at construction time."""
        from backend.orchestrator.mcp_tools import build_mcp_server

        with patch("subprocess.run") as mock_run:
            build_mcp_server(whitelist=["Read", "Grep"], env=_CLEAN_ENV, cwd=_CWD)
            # No subprocess launched at construction time
            mock_run.assert_not_called()
