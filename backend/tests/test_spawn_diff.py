"""Tests for backend/spawn_diff.py."""

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import spawn_diff  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal spawn_templates module source that implements render()
_BASE_TEMPLATE_SOURCE = textwrap.dedent(
    """\
    KNOWN_ROLES = {"executor", "code-reviewer"}

    def render(role, context):
        return (
            f"ROLE={role}\\n"
            f"project_context={context.get('project_context', '')}\\n"
            f"agent_memory={context.get('agent_memory', '')}\\n"
            "VERSION=base\\n"
        )
    """
)

_HEAD_TEMPLATE_SOURCE = textwrap.dedent(
    """\
    KNOWN_ROLES = {"executor", "code-reviewer"}

    def render(role, context):
        return (
            f"ROLE={role}\\n"
            f"project_context={context.get('project_context', '')}\\n"
            f"agent_memory={context.get('agent_memory', '')}\\n"
            "VERSION=head\\n"
        )
    """
)

_IDENTICAL_TEMPLATE_SOURCE = _BASE_TEMPLATE_SOURCE  # same as base => empty diff


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self):
        args = spawn_diff._parse_args(["--role", "executor"])
        assert args.role == "executor"
        assert args.base == "main"
        assert args.head == "HEAD"
        assert args.context_file is None

    def test_custom_refs(self):
        args = spawn_diff._parse_args(
            ["--role", "code-reviewer", "--base", "abc123", "--head", "def456"]
        )
        assert args.base == "abc123"
        assert args.head == "def456"


class TestLoadContext:
    def test_returns_default_fixture_when_no_file(self):
        ctx = spawn_diff._load_context(None)
        assert "discussion_number" in ctx
        assert "project_context" in ctx

    def test_loads_json_file(self, tmp_path):
        data = {"project_context": "from file", "discussion_number": "1"}
        p = tmp_path / "ctx.json"
        p.write_text(json.dumps(data))
        ctx = spawn_diff._load_context(str(p))
        assert ctx["project_context"] == "from file"

    def test_exits_1_missing_file(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context("/nonexistent/path.json")
        assert exc_info.value.code == 1

    def test_exits_1_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not-json")
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context(str(p))
        assert exc_info.value.code == 1


class TestRenderFromSource:
    def test_renders_base_template(self):
        ctx = spawn_diff._DEFAULT_FIXTURE.copy()
        result = spawn_diff._render_from_source(_BASE_TEMPLATE_SOURCE, "executor", ctx, "base")
        assert "ROLE=executor" in result
        assert "VERSION=base" in result

    def test_renders_head_template(self):
        ctx = spawn_diff._DEFAULT_FIXTURE.copy()
        result = spawn_diff._render_from_source(_HEAD_TEMPLATE_SOURCE, "executor", ctx, "head")
        assert "VERSION=head" in result

    def test_exits_2_on_bad_source(self):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._render_from_source("syntax error !!!", "executor", {}, "bad")
        assert exc_info.value.code == 2

    def test_exits_2_when_no_render_function(self):
        source = "KNOWN_ROLES = {'executor'}\n"
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._render_from_source(source, "executor", {}, "no-render")
        assert exc_info.value.code == 2


class TestMain:
    """Integration tests that stub _get_module_source_for_ref."""

    def _run_main(self, argv: list[str], base_src: str, head_src: str) -> str:
        """Run main() with git stubbed, return stdout."""

        def fake_get_source(ref: str, role: str) -> str:
            if ref in ("main",):
                return base_src
            return head_src

        with patch("backend.spawn_diff._get_module_source_for_ref", side_effect=fake_get_source):
            spawn_diff.main(argv)

    def test_diff_when_templates_differ(self, capsys):
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"],
            _BASE_TEMPLATE_SOURCE,
            _HEAD_TEMPLATE_SOURCE,
        )
        captured = capsys.readouterr()
        # Unified diff should show changed VERSION line
        assert "-VERSION=base" in captured.out
        assert "+VERSION=head" in captured.out

    def test_diff_shows_plus_minus_markers(self, capsys):
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"],
            _BASE_TEMPLATE_SOURCE,
            _HEAD_TEMPLATE_SOURCE,
        )
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        has_plus = any(line.startswith("+") and not line.startswith("+++") for line in lines)
        has_minus = any(line.startswith("-") and not line.startswith("---") for line in lines)
        assert has_plus and has_minus

    def test_empty_diff_when_identical(self, capsys):
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"],
            _IDENTICAL_TEMPLATE_SOURCE,
            _IDENTICAL_TEMPLATE_SOURCE,
        )
        captured = capsys.readouterr()
        assert "(no diff" in captured.out

    def test_unknown_role_exits_1(self):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff.main(["--role", "not-a-role"])
        assert exc_info.value.code == 1

    def test_context_file_passed_to_render(self, tmp_path, capsys):
        ctx_data = dict(spawn_diff._DEFAULT_FIXTURE)
        ctx_data["project_context"] = "custom-ctx-marker"
        ctx_file = tmp_path / "ctx.json"
        ctx_file.write_text(json.dumps(ctx_data))

        # Use identical sources — diff will be empty, but we just want no crash
        self._run_main(
            [
                "--role",
                "executor",
                "--base",
                "main",
                "--head",
                "HEAD",
                "--context-file",
                str(ctx_file),
            ],
            _IDENTICAL_TEMPLATE_SOURCE,
            _IDENTICAL_TEMPLATE_SOURCE,
        )
        captured = capsys.readouterr()
        assert "(no diff" in captured.out
