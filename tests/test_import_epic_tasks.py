"""
Tests for scripts/import-epic-tasks.py — focused on the GraphQL mutation arg shape.

The key bug was that gh api graphql does NOT support dot-notation like
`-f input.repositoryId=...` for nested input objects. The fix switches to
scalar variables with inline input construction in the mutation string.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (it has a hyphen in the name)
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "import-epic-tasks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("import_epic_tasks", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# create_discussion — arg shape
# ---------------------------------------------------------------------------

class TestCreateDiscussionArgShape:
    """Verify the gh CLI args use scalar vars, not dot-notation."""

    def test_uses_scalar_variables_not_dot_notation(self, mod):
        """The mutation must NOT pass -f input.repositoryId=... style args."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = '{"data": {"createDiscussion": {"discussion": {"number": 42, "id": "DISC_42"}}}}'
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result_tuple = mod.create_discussion(
                repo_id="R_test123",
                category_id="DIC_test456",
                title="Test Title",
                body="Test body",
                repo="autonomous-agent-7/autonomous-forever",
                dry_run=False,
            )

        assert result_tuple == (42, "DISC_42")

        # Must NOT have dot-notation args
        for arg in captured_cmd:
            assert "input.repositoryId" not in arg, f"Found forbidden dot-notation arg: {arg}"
            assert "input.categoryId" not in arg, f"Found forbidden dot-notation arg: {arg}"
            assert "input.title" not in arg, f"Found forbidden dot-notation arg: {arg}"
            assert "input.body" not in arg, f"Found forbidden dot-notation arg: {arg}"

    def test_scalar_vars_present_in_args(self, mod):
        """The fix must pass repoId, catId, title, body as separate -f flags."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = '{"data": {"createDiscussion": {"discussion": {"number": 7, "id": "DISC_7"}}}}'
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            mod.create_discussion(
                repo_id="R_abc",
                category_id="DIC_xyz",
                title="My Title",
                body="My body",
                repo="autonomous-agent-7/autonomous-forever",
                dry_run=False,
            )

        # Collect all -f flag values
        f_values = []
        for i, arg in enumerate(captured_cmd):
            if arg == "-f" and i + 1 < len(captured_cmd):
                f_values.append(captured_cmd[i + 1])

        # Must contain the scalar variable assignments
        assert any(v.startswith("repoId=") for v in f_values), f"-f repoId=... missing in {f_values}"
        assert any(v.startswith("catId=") for v in f_values), f"-f catId=... missing in {f_values}"
        assert any(v.startswith("title=") for v in f_values), f"-f title=... missing in {f_values}"
        assert any(v.startswith("body=") for v in f_values), f"-f body=... missing in {f_values}"

    def test_dry_run_returns_none(self, mod):
        """dry_run=True must not call subprocess and return None."""
        with patch("subprocess.run") as mock_run:
            result = mod.create_discussion(
                repo_id="R_abc",
                category_id="DIC_xyz",
                title="Dry run title",
                body="Dry run body",
                repo="autonomous-agent-7/autonomous-forever",
                dry_run=True,
            )
        mock_run.assert_not_called()
        assert result is None

    def test_api_failure_returns_none(self, mod):
        """Non-zero returncode returns None without raising."""
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "Some error"
            result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result = mod.create_discussion(
                repo_id="R_abc",
                category_id="DIC_xyz",
                title="Fail title",
                body="Fail body",
                repo="autonomous-agent-7/autonomous-forever",
                dry_run=False,
            )
        assert result is None

    def test_rate_limit_raises(self, mod):
        """secondary rate limit error must raise RateLimitError."""
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "403 secondary rate limit exceeded"
            result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(mod.RateLimitError):
                mod.create_discussion(
                    repo_id="R_abc",
                    category_id="DIC_xyz",
                    title="Rate limited",
                    body="body",
                    repo="autonomous-agent-7/autonomous-forever",
                    dry_run=False,
                )


# ---------------------------------------------------------------------------
# update_discussion_body — arg shape
# ---------------------------------------------------------------------------

class TestUpdateDiscussionBodyArgShape:
    """Verify updateDiscussion mutation uses scalar vars too."""

    def _make_fake_run(self, disc_id="DISC_node123"):
        """Return a fake subprocess.run that handles the query + mutation calls."""
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            result.returncode = 0
            if call_count[0] == 1:
                # First call: the query to fetch discussion id
                result.stdout = (
                    '{"data": {"repository": {"discussion": {"id": "'
                    + disc_id
                    + '", "body": "old body"}}}}'
                )
            else:
                # Second call: the mutation
                result.stdout = '{"data": {"updateDiscussion": {"discussion": {"number": 99}}}}'
            result.stderr = ""
            return result

        return fake_run, call_count

    def test_no_dot_notation_in_update_mutation(self, mod):
        """update_discussion_body must NOT pass input.discussionId or input.body."""
        all_cmds = []
        fake_run, _ = self._make_fake_run()

        original_fake_run = fake_run

        def capturing_run(cmd, **kwargs):
            all_cmds.extend(cmd)
            return original_fake_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=capturing_run):
            mod.update_discussion_body(
                repo="autonomous-agent-7/autonomous-forever",
                discussion_number=99,
                new_body="new body text",
                dry_run=False,
            )

        for arg in all_cmds:
            assert "input.discussionId" not in arg, f"Found forbidden dot-notation arg: {arg}"
            assert "input.body" not in arg, f"Found forbidden dot-notation arg: {arg}"

    def test_scalar_vars_in_update_mutation(self, mod):
        """update_discussion_body must pass discussionId and body as scalar -f flags."""
        captured_mutation_cmd = []
        fake_run, call_count = self._make_fake_run(disc_id="DISC_xyz")

        original_fake_run = fake_run

        def capturing_run(cmd, **kwargs):
            r = original_fake_run(cmd, **kwargs)
            if call_count[0] >= 2:
                # The mutation call (second subprocess.run call)
                captured_mutation_cmd.extend(cmd)
            return r

        with patch("subprocess.run", side_effect=capturing_run):
            mod.update_discussion_body(
                repo="autonomous-agent-7/autonomous-forever",
                discussion_number=99,
                new_body="updated body",
                dry_run=False,
            )

        # Collect -f values from mutation call
        f_values = []
        for i, arg in enumerate(captured_mutation_cmd):
            if arg == "-f" and i + 1 < len(captured_mutation_cmd):
                f_values.append(captured_mutation_cmd[i + 1])

        assert any(v.startswith("discussionId=") for v in f_values), (
            f"-f discussionId=... missing in mutation args: {f_values}"
        )
        assert any(v.startswith("body=") for v in f_values), (
            f"-f body=... missing in mutation args: {f_values}"
        )


# ---------------------------------------------------------------------------
# Helpers for empty-epic tests
# ---------------------------------------------------------------------------

def _make_epic_fixture(tmp_path: Path, epics: list[dict]) -> Path:
    """Build a minimal epics/ tree.

    Each entry in `epics` is a dict with:
      - name: str  (e.g. "epic-27-typescript-conversion")
      - epic_md: str  (content of epic.md; None → no epic.md)
      - task_files: list[str]  (filenames for numeric task files)
    """
    epics_root = tmp_path / "epics"
    epics_root.mkdir()
    for ep in epics:
        d = epics_root / ep["name"]
        d.mkdir()
        if ep.get("epic_md") is not None:
            (d / "epic.md").write_text(ep["epic_md"])
        for tf in ep.get("task_files", []):
            (d / tf).write_text(f"---\nepic: 99\ntask: 1\ntitle: x\ntype: task\nstatus: not-started\n---\nbody")
    return tmp_path


def _make_stub_gh_run(created_numbers: list[int]):
    """Return a fake subprocess.run that hands out discussion numbers sequentially."""
    counter = [0]

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""

        cmd_str = " ".join(str(c) for c in cmd)

        if "createDiscussion" in cmd_str:
            n = created_numbers[counter[0] % len(created_numbers)] if created_numbers else 100
            counter[0] += 1
            result.stdout = json.dumps({"data": {"createDiscussion": {"discussion": {"number": n, "id": f"DISC_{n}"}}}})
        elif "discussionCategories" in cmd_str:
            result.stdout = '{"data": {"repository": {"discussionCategories": {"nodes": [{"id": "CAT_1", "name": "General"}]}}}}'
        elif '"id"' in cmd_str and "discussions" not in cmd_str and "label" not in cmd_str and "addLabels" not in cmd_str:
            # repo node ID query
            result.stdout = '{"data": {"repository": {"id": "REPO_1"}}}'
        elif "discussions" in cmd_str and "nodes" in cmd_str:
            # list_existing_discussion_titles — return empty
            result.stdout = '{"data": {"repository": {"discussions": {"nodes": [], "pageInfo": {"hasNextPage": false, "endCursor": null}}}}}'
        elif "label" in cmd_str.lower() and "list" in cmd_str:
            result.stdout = "[]"
        elif "addLabelsToLabelable" in cmd_str:
            result.stdout = '{"data": {"addLabelsToLabelable": {"labelable": {"number": 1}}}}'
        elif "label" in cmd_str.lower():
            # label(name:...) lookup for add_labels_to_discussion
            result.stdout = '{"data": {"repository": {"label": {"id": "LABEL_1"}}}}'
        elif "discussion(number" in cmd_str:
            # fetch discussion node id for add_labels_to_discussion
            result.stdout = '{"data": {"repository": {"discussion": {"id": "DISC_1"}}}}'
        else:
            result.stdout = "{}"

        return result

    return fake_run, counter


# ---------------------------------------------------------------------------
# Empty-epic unit tests: find_empty_epic_dirs + epic_title_from_md
# ---------------------------------------------------------------------------

class TestFindEmptyEpicDirs:
    def test_returns_dirs_with_no_task_files(self, mod, tmp_path):
        """Dirs with only epic.md (no numbered task files) are returned."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
            {"name": "epic-32-enterprise-compliance", "epic_md": "# Epic 32"},
        ])
        result = mod.find_empty_epic_dirs(repo, exclude_epics=set())
        names = {d.name for d in result}
        assert "epic-27-typescript-conversion" in names
        assert "epic-32-enterprise-compliance" in names

    def test_excludes_dirs_with_task_files(self, mod, tmp_path):
        """Epic dirs that have at least one non-overview .md are excluded."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
            {"name": "epic-5-agent-monitor", "epic_md": "# Epic 5", "task_files": ["1.md", "2.md"]},
        ])
        result = mod.find_empty_epic_dirs(repo, exclude_epics=set())
        names = {d.name for d in result}
        assert "epic-27-typescript-conversion" in names
        assert "epic-5-agent-monitor" not in names

    def test_exclude_epics_filter(self, mod, tmp_path):
        """Dirs in exclude_epics set are skipped."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-22-vcs-agentblame", "epic_md": "# Epic 22"},
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
        ])
        result = mod.find_empty_epic_dirs(repo, exclude_epics={"epic-22-vcs-agentblame"})
        names = {d.name for d in result}
        assert "epic-22-vcs-agentblame" not in names
        assert "epic-27-typescript-conversion" in names

    def test_skips_dirs_without_epic_md(self, mod, tmp_path):
        """Dirs without epic.md are not returned (no anchor to create an overview from)."""
        epics_root = tmp_path / "epics"
        epics_root.mkdir()
        (epics_root / "epic-99-no-overview").mkdir()
        result = mod.find_empty_epic_dirs(tmp_path, exclude_epics=set())
        assert not result


class TestEpicTitleFromMd:
    def test_extracts_h1_heading(self, mod, tmp_path):
        """First # heading is used as the title."""
        f = tmp_path / "epic.md"
        f.write_text("# My Epic Title\n\nSome body text.")
        assert mod.epic_title_from_md(f) == "My Epic Title"

    def test_fallback_to_dirname_when_no_heading(self, mod, tmp_path):
        """When epic.md has no # heading, the parent dir name is returned."""
        epic_dir = tmp_path / "epic-27-typescript-conversion"
        epic_dir.mkdir()
        f = epic_dir / "epic.md"
        f.write_text("No heading here, just prose.")
        assert mod.epic_title_from_md(f) == "epic-27-typescript-conversion"

    def test_ignores_h2_headings(self, mod, tmp_path):
        """## headings don't count as the epic title; falls back to dirname."""
        epic_dir = tmp_path / "epic-33-dev-studio"
        epic_dir.mkdir()
        f = epic_dir / "epic.md"
        f.write_text("## Subtitle Only\n\nNo H1 here.")
        assert mod.epic_title_from_md(f) == "epic-33-dev-studio"


# ---------------------------------------------------------------------------
# Empty-epic integration tests: run_import with --include-empty-epics
# ---------------------------------------------------------------------------

def _make_graphql_fake_run(created_titles: list[str], existing_titles: list[str] | None = None):
    """Build a reusable fake subprocess.run for GraphQL-based integration tests.

    Distinguishes queries by keywords in the query string argument:
      - createDiscussion mutation → records title, returns numbered discussion
      - discussionCategories → returns one 'General' category
      - repository { id } (repo node ID) → returns REPO_1
      - discussions { nodes ... } → returns `existing_titles` as existing discussions
      - label/addLabels → returns stub responses
    """
    if existing_titles is None:
        existing_titles = []

    def _extract_query_arg(cmd: list) -> str:
        """Extract the -f query=... value from a gh CLI command list."""
        for i, part in enumerate(cmd):
            if part == "-f" and i + 1 < len(cmd) and str(cmd[i + 1]).startswith("query="):
                return str(cmd[i + 1])[len("query="):]
        return ""

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        q = _extract_query_arg(list(cmd))

        if "createDiscussion" in q:
            # Capture title
            for i, part in enumerate(cmd):
                if part == "-f" and i + 1 < len(cmd) and str(cmd[i + 1]).startswith("title="):
                    created_titles.append(str(cmd[i + 1])[len("title="):])
                    break
            n = len(created_titles) + 100
            result.stdout = json.dumps({"data": {"createDiscussion": {"discussion": {"number": n, "id": f"DISC_{n}"}}}})

        elif "discussionCategories" in q:
            result.stdout = '{"data": {"repository": {"discussionCategories": {"nodes": [{"id": "CAT_1", "name": "General"}]}}}}'

        elif "discussions" in q and "nodes" in q:
            # list_existing_discussion_titles
            nodes = [{"number": i + 1, "title": t} for i, t in enumerate(existing_titles)]
            import json as _json
            result.stdout = _json.dumps({
                "data": {"repository": {"discussions": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}}
            })

        elif "repository(owner" in q and "{ id }" in q:
            # get_repo_node_id
            result.stdout = '{"data": {"repository": {"id": "REPO_1"}}}'

        elif "addLabelsToLabelable" in q:
            result.stdout = '{"data": {"addLabelsToLabelable": {"labelable": {"number": 1}}}}'

        elif "label(name" in q:
            # label lookup for add_labels_to_discussion
            result.stdout = '{"data": {"repository": {"label": {"id": "LABEL_1"}}}}'

        elif "discussion(number" in q:
            # fetch discussion node id for add_labels_to_discussion
            result.stdout = '{"data": {"repository": {"discussion": {"id": "DISC_1"}}}}'

        elif "label" in " ".join(str(c) for c in cmd) and "list" in " ".join(str(c) for c in cmd):
            # gh label list
            result.stdout = "[]"

        else:
            result.stdout = "{}"

        return result

    return fake_run


class TestIncludeEmptyEpics:
    def test_include_empty_epics_creates_overview_discussions(self, mod, tmp_path):
        """With flag: creates overview Discussions for 2 empty epics; normal epic gets task discussions."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27\n\nSome detail."},
            {"name": "epic-32-enterprise-compliance", "epic_md": "# Epic 32\n\nOther detail."},
            # Normal epic with task files — will be filtered out by status (no frontmatter)
            {"name": "epic-5-agent-monitor", "epic_md": "# Epic 5", "task_files": ["1.md"]},
        ])

        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=True,
                    exclude_epics=set(),
                )

        # Should have created 2 epic-overview titles
        overview_titles = [t for t in created_titles if t.startswith("[Epic]")]
        assert len(overview_titles) == 2
        assert any("epic-27" in t for t in overview_titles)
        assert any("epic-32" in t for t in overview_titles)

    def test_without_flag_no_overview_discussions_created(self, mod, tmp_path):
        """Without --include-empty-epics, empty epic dirs produce zero Discussions."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
        ])

        created_titles: list[str] = []

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                # include_empty_epics=False (default)
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=False,
                    exclude_epics=set(),
                )

        assert not any(t.startswith("[Epic]") for t in created_titles), (
            f"Expected no [Epic] overviews, got: {created_titles}"
        )

    def test_exclude_epic_filter(self, mod, tmp_path):
        """--exclude-epic skips specified epics; only unexcluded ones get overview Discussions."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-22-vcs-agentblame", "epic_md": "# Epic 22"},
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
            {"name": "epic-32-enterprise-compliance", "epic_md": "# Epic 32"},
        ])

        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=True,
                    exclude_epics={"epic-22-vcs-agentblame", "epic-27-typescript-conversion"},
                )

        # Only epic-32 should be created (22 and 27 excluded)
        overview_titles = [t for t in created_titles if t.startswith("[Epic]")]
        assert len(overview_titles) == 1
        assert "epic-32" in overview_titles[0]

    def test_empty_epic_idempotent(self, mod, tmp_path):
        """Second run with same epics creates 0 new Discussions (all titles already exist)."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Epic 27"},
        ])

        existing_title = "[Epic] epic-27 — Epic 27"
        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles, existing_titles=[existing_title])

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=True,
                    exclude_epics=set(),
                )

        assert len(created_titles) == 0, (
            f"Expected 0 createDiscussion calls on re-run, got {len(created_titles)}: {created_titles}"
        )

    def test_empty_epic_title_from_h1(self, mod, tmp_path):
        """Overview Discussion title uses the # heading from epic.md."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "# Full TypeScript Conversion\n\nBody."},
        ])

        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=True,
                    exclude_epics=set(),
                )

        assert len(created_titles) == 1
        assert created_titles[0] == "[Epic] epic-27 — Full TypeScript Conversion"

    def test_empty_epic_title_fallback_to_dirname(self, mod, tmp_path):
        """When epic.md has no # heading, title falls back to epic dirname."""
        repo = _make_epic_fixture(tmp_path, [
            {"name": "epic-27-typescript-conversion", "epic_md": "No H1 heading here.\n\nJust prose."},
        ])

        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                    include_empty_epics=True,
                    exclude_epics=set(),
                )

        assert len(created_titles) == 1
        assert created_titles[0] == "[Epic] epic-27 — epic-27-typescript-conversion"


# ---------------------------------------------------------------------------
# D#1526 AC#13 — id threading, bounded backoff, timing summary
# ---------------------------------------------------------------------------

def _write_single_task(repo: Path, epic: int = 1, task: int = 1, title: str = "Demo") -> None:
    """Write one not-started task file under epics/epic-<epic>-demo/<task>.md."""
    epic_dir = repo / "epics" / f"epic-{epic}-demo"
    epic_dir.mkdir(parents=True, exist_ok=True)
    (epic_dir / f"{task}.md").write_text(
        f"---\nepic: {epic}\ntask: {task}\ntitle: {title}\ntype: task\nstatus: not-started\n---\nbody"
    )


class TestIdThreadedIntoLabels:
    """AC#13(a): the node id from create_discussion flows into
    add_labels_to_discussion with no intermediate per-Discussion id query."""

    def test_no_intermediate_discussion_id_query(self, mod, tmp_path):
        repo = tmp_path
        _write_single_task(repo)

        forbidden_query_hits: list[str] = []

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""

            if "discussion(number: $number) { id }" in cmd_str:
                forbidden_query_hits.append(cmd_str)
                result.stdout = "{}"
            elif "createDiscussion" in cmd_str:
                result.stdout = json.dumps(
                    {"data": {"createDiscussion": {"discussion": {"number": 55, "id": "DISC_55"}}}}
                )
            elif "discussionCategories" in cmd_str:
                result.stdout = json.dumps(
                    {"data": {"repository": {"discussionCategories": {"nodes": [{"id": "CAT_1", "name": "General"}]}}}}
                )
            elif "addLabelsToLabelable" in cmd_str:
                # Assert the disc_id threaded straight through, not re-queried.
                assert "DISC_55" in cmd_str, f"expected threaded disc_id DISC_55 in mutation, got: {cmd_str}"
                result.stdout = json.dumps({"data": {"addLabelsToLabelable": {"labelable": {"number": 55}}}})
            elif "label(name" in cmd_str:
                result.stdout = json.dumps({"data": {"repository": {"label": {"id": "LABEL_1"}}}})
            elif "discussions" in cmd_str and "nodes" in cmd_str:
                result.stdout = json.dumps(
                    {"data": {"repository": {"discussions": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
                )
            elif "repository(owner" in cmd_str:
                result.stdout = json.dumps({"data": {"repository": {"id": "REPO_1"}}})
            else:
                result.stdout = "{}"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                )

        assert forbidden_query_hits == [], (
            f"add_labels_to_discussion issued a forbidden per-Discussion id query: {forbidden_query_hits}"
        )


class TestRateLimitBoundedBackoff:
    """AC#13(b): 403 triggers bounded capped backoff, then save_pending
    (never an unbounded retry loop)."""

    def test_bounded_backoff_then_save_pending(self, mod, tmp_path):
        repo = tmp_path
        _write_single_task(repo)

        create_attempts = [0]

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.stderr = ""

            if "createDiscussion" in cmd_str:
                create_attempts[0] += 1
                result.returncode = 1
                result.stderr = "403 secondary rate limit exceeded"
                result.stdout = ""
            elif "discussionCategories" in cmd_str:
                result.returncode = 0
                result.stdout = json.dumps(
                    {"data": {"repository": {"discussionCategories": {"nodes": [{"id": "CAT_1", "name": "General"}]}}}}
                )
            elif "discussions" in cmd_str and "nodes" in cmd_str:
                result.returncode = 0
                result.stdout = json.dumps(
                    {"data": {"repository": {"discussions": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
                )
            elif "repository(owner" in cmd_str:
                result.returncode = 0
                result.stdout = json.dumps({"data": {"repository": {"id": "REPO_1"}}})
            else:
                result.returncode = 0
                result.stdout = "{}"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep") as mock_sleep, patch("random.uniform", return_value=0.0):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                )

        # Bounded — exactly MAX_RETRY_ATTEMPTS create attempts, never unbounded.
        assert create_attempts[0] == mod.MAX_RETRY_ATTEMPTS
        # Backoff sleeps happened between retries (not the polite 1s only).
        assert mock_sleep.call_count >= mod.MAX_RETRY_ATTEMPTS - 1

        pending_file = repo / ".autonomous-team" / "pending-imports.json"
        assert pending_file.exists(), "expected pending-imports.json to be written after exhausting retries"
        pending = json.loads(pending_file.read_text())
        assert len(pending) == 1


class TestTimingSummaryLine:
    """AC#13(c): run emits one line with created-count, elapsed seconds,
    and graphql-call count, e.g. 'seeded 1 discussions in 0.0s (N graphql calls)'."""

    def test_timing_summary_line_format(self, mod, tmp_path, capsys):
        repo = tmp_path
        _write_single_task(repo)

        created_titles: list[str] = []
        fake_run = _make_graphql_fake_run(created_titles)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("time.sleep"):
                mod.run_import(
                    repo_path=repo,
                    repo="autonomous-agent-7/test",
                    status_filter={"not-started"},
                    dry_run=False,
                    epic_filter=None,
                )

        out = capsys.readouterr().out
        import re
        match = re.search(r"seeded (\d+) discussions? in ([\d.]+)s \((\d+) graphql calls\)", out)
        assert match, f"timing summary line not found in output:\n{out}"
        assert int(match.group(1)) == 1
        assert int(match.group(3)) > 0
