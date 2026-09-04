"""
tests for loop-bootstrap/scripts/generate-initial-plan.py

Tests cover:
- Mocked gh output with grouping logic
- Template substitution
- --force flag behaviour
- Status-based P1/P2/P3 assignment
- Idempotency when plan already exists
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Load module from loop-bootstrap path ─────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "loop-bootstrap" / "scripts" / "generate-initial-plan.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_initial_plan", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return load_module()


# ── Fixture: sample discussion nodes ─────────────────────────────────────────

def make_disc(number: int, title: str, status: str = "", labels=None, category="General"):
    body = ""
    if status:
        body = f"STATUS:{status}\nSome content here."
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": {"nodes": [{"name": l} for l in (labels or [])]},
        "category": {"name": category},
    }


SAMPLE_DISCUSSIONS = [
    make_disc(1, "[Feature] Add login", "SPEC_READY", labels=["epic-1"]),
    make_disc(2, "[Bug] Fix crash", "SPEC_READY", labels=["epic-2"]),
    make_disc(3, "[Feature] Dashboard", "SPEC_READY", labels=["epic-1"]),
    make_disc(4, "[Feature] Settings page", "SPEC_READY", labels=["epic-3"]),
    make_disc(5, "[Feature] API rate limit", "SPEC_READY", labels=["epic-3"]),
    make_disc(6, "[Feature] Webhooks", "SPEC_READY"),  # no epic label -> category
    make_disc(7, "[Feature] CLI tool", "IMPLEMENTING", labels=["epic-2"]),
    make_disc(8, "[Research] Evaluate options", "SPEC_WRITING"),
    make_disc(9, "[Idea] New feature", "DISCUSSING"),
    make_disc(10, "[Enhancement] Improve perf", ""),
]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGetSpecStatus:
    def test_spec_ready(self, mod):
        body = "STATUS:SPEC_READY\nSome content."
        assert mod.get_spec_status(body) == "SPEC_READY"

    def test_implementing(self, mod):
        body = "Title\n\nSTATUS:IMPLEMENTING\nMore."
        assert mod.get_spec_status(body) == "IMPLEMENTING"

    def test_no_status(self, mod):
        assert mod.get_spec_status("No status here") == ""

    def test_empty_body(self, mod):
        assert mod.get_spec_status("") == ""

    def test_none_body(self, mod):
        assert mod.get_spec_status(None) == ""

    def test_get_spec_status_from_label(self, mod):
        """Label-only, no body marker → returns SPEC_READY."""
        assert mod.get_spec_status("", ["bug", "SPEC_READY"]) == "SPEC_READY"

    def test_body_marker_takes_precedence_over_label(self, mod):
        """Body marker wins when both are present and conflict."""
        body = "STATUS:IMPLEMENTING\nSome content."
        assert mod.get_spec_status(body, ["SPEC_READY"]) == "IMPLEMENTING"

    def test_no_status_when_neither_present(self, mod):
        """Empty body and no matching label → returns empty string."""
        assert mod.get_spec_status("", ["bug", "enhancement"]) == ""


class TestGetEpicLabel:
    def test_finds_epic(self, mod):
        assert mod.get_epic_label(["bug", "epic-3", "enhancement"]) == "epic-3"

    def test_first_epic(self, mod):
        assert mod.get_epic_label(["epic-1", "epic-2"]) == "epic-1"

    def test_no_epic(self, mod):
        assert mod.get_epic_label(["bug", "feature"]) == ""

    def test_empty(self, mod):
        assert mod.get_epic_label([]) == ""


class TestGroupDiscussions:
    def test_groups_by_epic(self, mod):
        discs = [
            make_disc(1, "A", labels=["epic-1"]),
            make_disc(2, "B", labels=["epic-2"]),
            make_disc(3, "C", labels=["epic-1"]),
        ]
        groups = mod.group_discussions(discs)
        assert "epic-1" in groups
        assert len(groups["epic-1"]) == 2
        assert "epic-2" in groups
        assert len(groups["epic-2"]) == 1

    def test_falls_back_to_category(self, mod):
        discs = [
            make_disc(1, "A", labels=[], category="Ideas"),
            make_disc(2, "B", labels=[], category="General"),
        ]
        groups = mod.group_discussions(discs)
        assert "Ideas" in groups
        assert "General" in groups

    def test_empty(self, mod):
        groups = mod.group_discussions([])
        assert groups == {}


class TestPriorityAssignment:
    """Test that P1/P2/P3 assignment matches the spec:
    - SPEC_READY → P1 (first p1_count) or P2 (overflow)
    - IMPLEMENTING/SPEC_WRITING → P2
    - Everything else → P3
    """

    def _run(self, mod, discussions, p1_count=5):
        spec_ready = [d for d in discussions if mod.get_spec_status(d.get("body", "") or "") == "SPEC_READY"]
        in_progress = [d for d in discussions if mod.get_spec_status(d.get("body", "") or "") in ("IMPLEMENTING", "REVIEWING", "SPEC_WRITING")]
        backlog = [d for d in discussions if mod.get_spec_status(d.get("body", "") or "") not in ("SPEC_READY", "IMPLEMENTING", "REVIEWING", "SPEC_WRITING")]

        p1 = spec_ready[:p1_count]
        p2_candidates = spec_ready[p1_count:] + in_progress
        p2 = p2_candidates[:10]
        p3 = backlog + p2_candidates[10:]
        return p1, p2, p3

    def test_first_five_spec_ready_are_p1(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=5)
        assert len(p1) == 5
        for d in p1:
            assert mod.get_spec_status(d.get("body", "")) == "SPEC_READY"

    def test_overflow_spec_ready_goes_to_p2(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=5)
        # disc 6 is SPEC_READY and was 6th — goes to P2
        p2_numbers = [d["number"] for d in p2]
        assert 6 in p2_numbers

    def test_implementing_goes_to_p2(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=5)
        p2_numbers = [d["number"] for d in p2]
        assert 7 in p2_numbers  # disc 7 is IMPLEMENTING

    def test_no_status_goes_to_p3(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=5)
        p3_numbers = [d["number"] for d in p3]
        assert 10 in p3_numbers  # disc 10 has no status

    def test_custom_p1_count(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=2)
        assert len(p1) == 2

    def test_p1_count_larger_than_available(self, mod):
        p1, p2, p3 = self._run(mod, SAMPLE_DISCUSSIONS, p1_count=100)
        spec_ready_count = sum(1 for d in SAMPLE_DISCUSSIONS
                               if mod.get_spec_status(d.get("body", "")) == "SPEC_READY")
        assert len(p1) == spec_ready_count


class TestTemplateSubstitution:
    def test_date_and_project_substituted(self, mod):
        template = "# Plan — {{date}}\nProject: {{project_name}}\n## P1\n"
        result = mod.render_plan(
            template=template,
            date="2026-05-17",
            project_name="my-proj",
            repo="acme/my-proj",
            p1=[],
            p2=[],
            p3=[],
        )
        assert "2026-05-17" in result
        assert "my-proj" in result
        assert "{{date}}" not in result
        assert "{{project_name}}" not in result

    def test_disc_lines_rendered(self, mod):
        template = (
            "# Plan — {{date}}\nProject: {{project_name}}\n"
            "## P1\n<!-- Format: D#NNN — short title [SPEC_READY] -->\n"
        )
        p1 = [make_disc(42, "My feature", "SPEC_READY", labels=["epic-7"])]
        result = mod.render_plan(
            template=template,
            date="2026-05-17",
            project_name="test",
            repo="acme/test",
            p1=p1,
            p2=[],
            p3=[],
        )
        assert "D#42" in result
        assert "My feature" in result

    def test_empty_p1_shows_placeholder(self, mod):
        template = (
            "# Plan — {{date}}\nProject: {{project_name}}\n"
            "## P1\n<!-- Format: D#NNN — short title [SPEC_READY] -->\n"
        )
        result = mod.render_plan(
            template=template,
            date="2026-05-17",
            project_name="test",
            repo="acme/test",
            p1=[],
            p2=[],
            p3=[],
        )
        assert "<!-- none ready -->" in result


class TestIdempotency:
    def test_skips_if_plan_exists_no_force(self, mod, tmp_path):
        # Create a fake project.json
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({"project_name": "test", "repo": "acme/test"})
        )

        # Create existing plan
        existing = at_dir / "PLAN-2026-05-17.md"
        existing.write_text("# Existing plan\n")

        # Patch sys.argv
        with patch("sys.argv", ["generate-initial-plan.py", str(tmp_path), "--date", "2026-05-17"]):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 0

        # Plan must be unchanged
        assert existing.read_text() == "# Existing plan\n"

    def test_force_overwrites(self, mod, tmp_path):
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({"project_name": "test", "repo": "acme/test"})
        )

        existing = at_dir / "PLAN-2026-05-17.md"
        existing.write_text("# Old plan\n")

        # Mock gh call to return no discussions
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"data": {"repository": {"discussions": {"nodes": []}}}}),
                stderr="",
            )
            with patch("sys.argv", [
                "generate-initial-plan.py", str(tmp_path),
                "--date", "2026-05-17", "--force"
            ]):
                mod.main()

        # Plan was overwritten
        content = existing.read_text()
        assert content != "# Old plan\n"


class TestGhIntegration:
    """Tests that mock the gh CLI call."""

    def test_gh_list_discussions_parses_response(self, mod):
        raw = {
            "data": {
                "repository": {
                    "discussions": {
                        "nodes": [
                            make_disc(1, "Hello", "SPEC_READY"),
                            make_disc(2, "World", "IMPLEMENTING"),
                        ]
                    }
                }
            }
        }
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(raw),
                stderr="",
            )
            result = mod.gh_list_discussions("acme/test")

        assert len(result) == 2
        assert result[0]["number"] == 1
        assert result[1]["title"] == "World"

    def test_gh_list_discussions_handles_failure(self, mod):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="error: auth required",
            )
            result = mod.gh_list_discussions("acme/test")

        assert result == []

    def test_full_main_writes_plan(self, mod, tmp_path):
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({
                "project_name": "test-proj",
                "repo": "acme/test-proj",
                "state_dir": str(tmp_path / "state"),
            })
        )

        # Fake script dir for template loading
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "PLAN-template.md").write_text(
            "# Plan — {{date}}\nProject: {{project_name}}\n"
            "## P1\n<!-- Format: D#NNN — short title [SPEC_READY] -->\n"
            "## P2\n<!-- Format: D#NNN — short title [DISCUSSING/SPEC_WRITING] -->\n"
            "## P3\n<!-- Format: D#NNN — short title -->\n"
        )

        raw = {
            "data": {
                "repository": {
                    "discussions": {
                        "nodes": [
                            make_disc(10, "SPEC_READY thing", "SPEC_READY"),
                            make_disc(11, "In-flight", "IMPLEMENTING"),
                            make_disc(12, "Backlog item", ""),
                        ]
                    }
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(raw),
                stderr="",
            )
            # Patch __file__ so load_template finds our template
            with patch.object(mod, "load_template", return_value=(templates_dir / "PLAN-template.md").read_text()):
                with patch("sys.argv", [
                    "generate-initial-plan.py", str(tmp_path),
                    "--date", "2026-05-17",
                ]):
                    mod.main()

        plan = at_dir / "PLAN-2026-05-17.md"
        assert plan.exists()
        content = plan.read_text()
        assert "2026-05-17" in content
        assert "test-proj" in content
        assert "D#10" in content


class TestTemplateLoading:
    """Verify load_template() finds the installed template at backend/spawn_templates/."""

    def test_loads_from_backend_spawn_templates(self, mod, tmp_path):
        """When templates/ is absent but backend/spawn_templates/ exists, use it."""
        # Simulate an installed target layout:
        #   tmp_path/scripts/generate-initial-plan.py   ← scripts_dir
        #   tmp_path/backend/spawn_templates/PLAN-template.md
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        spawn_tpl = tmp_path / "backend" / "spawn_templates"
        spawn_tpl.mkdir(parents=True)
        (spawn_tpl / "PLAN-template.md").write_text(
            "# INSTALLED TEMPLATE — {{date}}\nProject: {{project_name}}\n"
        )

        result = mod.load_template(scripts_dir)
        assert "INSTALLED TEMPLATE" in result, (
            f"load_template() returned fallback instead of installed template; got: {result!r}"
        )

    def test_prefers_loop_bootstrap_source(self, mod, tmp_path):
        """The loop-bootstrap source templates/ is preferred over backend/spawn_templates/."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()

        # Source layout: scripts/../templates/
        source_tpl = tmp_path / "templates"
        source_tpl.mkdir()
        (source_tpl / "PLAN-template.md").write_text("# SOURCE TEMPLATE\n")

        # Also create installed location
        spawn_tpl = tmp_path / "backend" / "spawn_templates"
        spawn_tpl.mkdir(parents=True)
        (spawn_tpl / "PLAN-template.md").write_text("# INSTALLED TEMPLATE\n")

        result = mod.load_template(scripts_dir)
        assert "SOURCE TEMPLATE" in result


class TestTemplateLoadingHardFail:
    """D#2218: load_template() must hard-fail, not silently return an empty
    shell, when no PLAN-template.md candidate exists anywhere.

    Unlike every test above, this deliberately does NOT create the template
    under tmp_path at all -- that omission is the point. The tests above
    construct the world in which the code already works (they write the
    template file themselves before calling load_template()), which is
    exactly why the original silent-fallback bug went unnoticed: a real
    freshly-bootstrapped target that bootstrap.sh forgot to install the
    template into looks like this test's tmp_path, not like the tests above.
    """

    def test_raises_when_no_template_found(self, mod, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        # No templates/ anywhere, no backend/spawn_templates/ -- nothing.
        with pytest.raises(FileNotFoundError) as exc_info:
            mod.load_template(scripts_dir)
        # The error should name a concrete next step, not just "not found".
        assert "PLAN-template.md" in str(exc_info.value)
        assert "bootstrap.sh" in str(exc_info.value)

    def test_main_exits_nonzero_and_writes_nothing_with_no_template(self, mod, tmp_path):
        """End-to-end: main() must hard-fail (nonzero exit) and must NOT write
        a PLAN-<DATE>.md when no template resolves -- the actual bug shipped a
        plan file anyway, just an empty one, and reported success."""
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({"project_name": "test-proj", "repo": "acme/test-proj"})
        )

        fake_script = tmp_path / "scripts" / "generate-initial-plan.py"
        fake_script.parent.mkdir(parents=True, exist_ok=True)

        raw = {
            "data": {
                "repository": {
                    "discussions": {
                        "nodes": [
                            make_disc(10, "SPEC_READY thing", "SPEC_READY"),
                        ]
                    }
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(raw), stderr="",
            )
            # __file__ drives scripts_dir inside main() -- point it at a
            # scripts/ dir with no templates/ or backend/spawn_templates/
            # anywhere under tmp_path, the real shape of the bug.
            with patch.object(mod, "__file__", str(fake_script)):
                with patch("sys.argv", [
                    "generate-initial-plan.py", str(tmp_path),
                    "--date", "2026-05-17",
                ]):
                    with pytest.raises(SystemExit) as exc_info:
                        mod.main()
                    assert exc_info.value.code != 0

        assert not (at_dir / "PLAN-2026-05-17.md").exists(), (
            "main() must not write a plan file when the template can't be loaded"
        )


class TestDryRun:
    """Tests for --dry-run flag: no file written, stdout contains [dry-run] marker."""

    def test_dry_run_does_not_write(self, mod, tmp_path):
        """--dry-run must not write PLAN-<DATE>.md to disk."""
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({"project_name": "test-proj", "repo": "acme/test-proj"})
        )

        plan_path = at_dir / "PLAN-2026-05-17.md"
        assert not plan_path.exists()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"data": {"repository": {"discussions": {"nodes": []}}}}),
                stderr="",
            )
            with patch.object(
                mod,
                "load_template",
                return_value="# Plan — {{date}}\nProject: {{project_name}}\n",
            ):
                with patch("sys.argv", [
                    "generate-initial-plan.py", str(tmp_path),
                    "--date", "2026-05-17", "--dry-run",
                ]):
                    import io
                    captured = io.StringIO()
                    import sys as _sys
                    old_stdout = _sys.stdout
                    _sys.stdout = captured
                    try:
                        mod.main()
                    finally:
                        _sys.stdout = old_stdout
                    stdout_text = captured.getvalue()

        assert not plan_path.exists(), "dry-run must not write the plan file"
        assert "[dry-run]" in stdout_text, f"stdout missing [dry-run] marker; got: {stdout_text!r}"

    def test_dry_run_renders_same_content(self, mod, tmp_path):
        """Dry-run stdout content matches the file written by a live run on the same input."""
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir()
        (at_dir / "project.json").write_text(
            json.dumps({"project_name": "test-proj", "repo": "acme/test-proj"})
        )

        fake_discussions = [
            make_disc(10, "SPEC_READY thing", "SPEC_READY"),
            make_disc(11, "In-flight", "IMPLEMENTING"),
        ]
        raw = {
            "data": {
                "repository": {
                    "discussions": {"nodes": fake_discussions}
                }
            }
        }
        template_text = (
            "# Plan — {{date}}\nProject: {{project_name}}\n"
            "## P1\n<!-- Format: D#NNN — short title [SPEC_READY] -->\n"
            "## P2\n<!-- Format: D#NNN — short title [DISCUSSING/SPEC_WRITING] -->\n"
            "## P3\n<!-- Format: D#NNN — short title -->\n"
        )

        def _run_main(extra_args):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout=json.dumps(raw), stderr=""
                )
                with patch.object(mod, "load_template", return_value=template_text):
                    with patch("sys.argv", [
                        "generate-initial-plan.py", str(tmp_path),
                        "--date", "2026-05-17",
                    ] + extra_args):
                        import io, sys as _sys
                        captured = io.StringIO()
                        old_stdout = _sys.stdout
                        _sys.stdout = captured
                        try:
                            mod.main()
                        finally:
                            _sys.stdout = old_stdout
                        return captured.getvalue()

        # Dry-run: capture stdout (the rendered plan + [dry-run] line)
        dry_stdout = _run_main(["--dry-run"])

        # Live run: write file, then read it
        _run_main(["--force"])
        file_content = (at_dir / "PLAN-2026-05-17.md").read_text()

        # The dry-run stdout should contain the same rendered plan content.
        # Strip the trailing "[dry-run] would write to ..." line before comparing.
        dry_plan_part = "\n".join(
            line for line in dry_stdout.splitlines()
            if not line.startswith("[dry-run]") and not line.startswith("Fetching") and not line.startswith("WARNING")
        ).strip()
        assert dry_plan_part == file_content.strip(), (
            f"dry-run content differs from live-run file content.\n"
            f"dry: {dry_plan_part!r}\nfile: {file_content.strip()!r}"
        )
