"""tests/orchestrator/test_tool_proxy.py — Unit tests for tool proxy (S2, S5)."""

import os
from pathlib import Path

import pytest

from backend.orchestrator.tool_proxy import (
    UnknownToolError,
    EnvLeakError,
    validate_env,
    build_env,
    dispatch,
    run_bash,
    run_read,
    run_write,
    run_edit,
)
from testsupport.fixture_paths import FIXTURE_HOME


# ---------------------------------------------------------------------------
# S2 — env allowlist enforcement
# ---------------------------------------------------------------------------

class TestEnvAllowlist:
    """tool_proxy.run_bash must reject forbidden credential keys in env."""

    def test_anthropic_api_key_blocked(self):
        env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-secret"}
        with pytest.raises(EnvLeakError, match="ANTHROPIC_API_KEY"):
            validate_env(env)

    def test_anthropic_auth_token_blocked(self):
        env = {"ANTHROPIC_AUTH_TOKEN": "tok_secret"}
        with pytest.raises(EnvLeakError, match="ANTHROPIC_AUTH_TOKEN"):
            validate_env(env)

    def test_claude_oauth_token_blocked(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth_secret"}
        with pytest.raises(EnvLeakError, match="CLAUDE_CODE_OAUTH_TOKEN"):
            validate_env(env)

    def test_openai_api_key_blocked(self):
        env = {"OPENAI_API_KEY": "sk-openai-secret"}
        with pytest.raises(EnvLeakError, match="OPENAI_API_KEY"):
            validate_env(env)

    def test_generic_api_key_blocked(self):
        env = {"SOME_SERVICE_API_KEY": "supersecret"}
        with pytest.raises(EnvLeakError):
            validate_env(env)

    def test_generic_secret_blocked(self):
        env = {"DB_PASSWORD_SECRET": "hunter2"}
        with pytest.raises(EnvLeakError):
            validate_env(env)

    def test_clean_env_passes(self):
        env = {"PATH": "/usr/bin", "HOME": FIXTURE_HOME, "GH_TOKEN": "ghs_valid_gh_token_here"}
        # GH_TOKEN is not in the forbidden patterns so should pass
        validate_env(env)  # must not raise

    def test_build_env_excludes_secrets(self, monkeypatch):
        """build_env must not include ANTHROPIC_API_KEY even if in OS env."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-appear")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", FIXTURE_HOME)
        env = build_env([])
        assert "ANTHROPIC_API_KEY" not in env
        assert "PATH" in env
        assert "HOME" in env

    def test_build_env_includes_granted_keys(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghp_mytoken")
        env = build_env(["GH_TOKEN"])
        assert "GH_TOKEN" in env

    def test_run_bash_requires_explicit_env(self, tmp_path):
        """run_bash must NOT have a default env parameter (confirmed via signature)."""
        import inspect
        sig = inspect.signature(run_bash)
        # env must have no default (i.e., it's a required parameter)
        param = sig.parameters["env"]
        assert param.default is inspect.Parameter.empty, (
            "run_bash(env) must be a required parameter with no default"
        )


# ---------------------------------------------------------------------------
# S5 — fail-closed tool dispatch
# ---------------------------------------------------------------------------

class TestFailClosedDispatch:
    def test_unknown_tool_raises(self, tmp_path):
        env = {"PATH": "/usr/bin", "HOME": FIXTURE_HOME}
        whitelist = ["Read", "Bash"]
        with pytest.raises(UnknownToolError, match="'Write'"):
            dispatch("Write", {"path": "x.txt", "content": "hi"}, whitelist, env, str(tmp_path))

    def test_tool_not_in_registry_raises(self, tmp_path):
        env = {"PATH": "/usr/bin", "HOME": FIXTURE_HOME}
        whitelist = ["FakeTool"]
        with pytest.raises(UnknownToolError):
            dispatch("FakeTool", {}, whitelist, env, str(tmp_path))

    def test_whitelisted_read_succeeds(self, tmp_path):
        target = tmp_path / "hello.txt"
        target.write_text("hello world")
        env = {"PATH": "/usr/bin"}
        result = dispatch(
            "Read",
            {"path": str(target)},
            ["Read"],
            env,
            str(tmp_path),
        )
        assert "hello world" in result

    def test_bash_env_leak_raises_from_dispatch(self, tmp_path):
        env = {"ANTHROPIC_API_KEY": "sk-ant-secret"}
        whitelist = ["Bash"]
        with pytest.raises(EnvLeakError):
            dispatch("Bash", {"command": "echo hi"}, whitelist, env, str(tmp_path))


# ---------------------------------------------------------------------------
# Tool handler unit tests
# ---------------------------------------------------------------------------

class TestRunRead:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content here")
        assert run_read(str(f), {}, str(tmp_path)) == "content here"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_read(str(tmp_path / "missing.txt"), {}, str(tmp_path))


class TestRunWrite:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "new.txt"
        run_write(str(path), "hello", {}, str(tmp_path))
        assert path.read_text() == "hello"

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "c.txt"
        run_write(str(path), "deep", {}, str(tmp_path))
        assert path.read_text() == "deep"


class TestRunEdit:
    def test_replaces_unique_string(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")
        result = run_edit(str(f), "world", "earth", {}, str(tmp_path))
        assert "earth" in result
        assert "world" not in result

    def test_raises_if_not_found(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")
        with pytest.raises(ValueError, match="not found"):
            run_edit(str(f), "missing", "replacement", {}, str(tmp_path))

    def test_raises_if_not_unique(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("abc abc abc")
        with pytest.raises(ValueError, match="3 times"):
            run_edit(str(f), "abc", "xyz", {}, str(tmp_path))
