"""Tests for scripts/spec-acceptance-lint.py (D#2377).

The linter covers exactly two decidable shapes from D#2377's retrospective — a
referenced filename that does not exist, and a criterion stated as a bare count.
Fixture material below reuses real strings from D#2377's own body where possible
(tests/test_post_merge_hook.sh genuinely does not exist on the tree; the count
phrasing is quoted from the Discussion's own catalogue of wrong counts) rather than
inventing synthetic examples.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

_MODULE_PATH = _REPO_ROOT / "scripts" / "spec-acceptance-lint.py"
_spec = importlib.util.spec_from_file_location("spec_acceptance_lint", _MODULE_PATH)
lint_module = importlib.util.module_from_spec(_spec)
# Register in sys.modules before exec — the module's @dataclass needs to find its
# own module by name (dataclasses looks it up via sys.modules[cls.__module__]),
# which spec_from_file_location alone does not set up.
sys.modules[_spec.name] = lint_module
_spec.loader.exec_module(lint_module)  # type: ignore[union-attr]


def _wrap(spec_acceptance_body: str) -> str:
    """Wrap a `## Spec (Acceptance)` fragment in the three-section template so
    backend.discussion_status.get_sections can find it."""
    return (
        "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n\n"
        "Example topic for a test fixture.\n\n"
        "---\n\n"
        "## Intent\n"
        "- **Goal:** example.\n\n"
        "## Spec (Acceptance)\n\n"
        + spec_acceptance_body +
        "\n\n## Implementation Notes (advisory — system may override)\n"
        "- example note.\n\n"
        "**Status**: FROZEN — do not modify after SPEC_READY\n"
    )


# A filename that genuinely does not exist on the tree, quoted from D#2377's own
# catalogue: "names `bash tests/test_post_merge_hook.sh`. That file does not exist
# in the tree" (the .py sibling does). A count phrase also quoted verbatim from the
# same Discussion's list of wrongly-stated counts ("67 occurrences across 10 files").
# A third item names a file that does exist, to prove the missing-file check does
# not fire on every backtick span.
FIXTURE_WITH_ISSUES = _wrap(
    "---\n"
    "planned_prs: 1\n"
    "---\n\n"
    "1. Run `bash tests/test_post_merge_hook.sh` and confirm it passes.\n"
    "2. Fix all 67 occurrences across 10 files of the old pattern.\n"
    "3. Confirm `scripts/spec-context-oracle.py` still runs cleanly.\n"
)

# No bare counts, and both referenced files exist on the tree — modeled on the
# retro's own suggested rewrite ("Every `gh pr` call takes the code slug; enumerate
# them in the PR body" is checkable and survives a rename).
FIXTURE_CLEAN = _wrap(
    "---\n"
    "planned_prs: 1\n"
    "---\n\n"
    "1. Every `gh pr` call in `scripts/spec-context-oracle.py` takes the code "
    "slug; enumerate them in the PR body.\n"
    "2. `CLAUDE.md` documents the Build Commands section unchanged.\n"
)

# The `acceptance_files:` frontmatter routinely names files the PR will create —
# they don't exist yet by design, and must never be linted as findings.
FIXTURE_FRONTMATTER_ONLY = _wrap(
    "---\n"
    "planned_prs: 1\n"
    "acceptance_files:\n"
    "  - scripts/this-file-does-not-exist-anywhere.py\n"
    "---\n\n"
    "1. `CLAUDE.md` stays unchanged.\n"
)


def test_lint_flags_missing_file_and_bare_count():
    findings = lint_module.lint(FIXTURE_WITH_ISSUES, _REPO_ROOT)
    by_item = {f.item: f for f in findings}

    assert 1 in by_item, f"expected item 1 (missing file) flagged, got {findings}"
    assert by_item[1].kind == "missing_file"
    assert "test_post_merge_hook.sh" in by_item[1].detail

    assert 2 in by_item, f"expected item 2 (bare count) flagged, got {findings}"
    assert by_item[2].kind == "bare_count"

    assert 3 not in by_item, (
        f"item 3 names an existing file (scripts/spec-context-oracle.py) and "
        f"should not be flagged, got {findings}"
    )


def test_lint_no_false_positives_on_clean_fixture():
    findings = lint_module.lint(FIXTURE_CLEAN, _REPO_ROOT)
    assert findings == [], (
        f"a Spec whose items name only existing files with no bare-count "
        f"criteria must produce zero findings, got {findings}"
    )


def test_frontmatter_files_are_not_linted():
    findings = lint_module.lint(FIXTURE_FRONTMATTER_ONLY, _REPO_ROOT)
    assert findings == [], (
        f"a file named only in acceptance_files: frontmatter must never be "
        f"linted (it names work the PR will create), got {findings}"
    )


def test_parse_items_keeps_multiline_items_whole():
    items = lint_module.parse_items(
        "---\nplanned_prs: 1\n---\n\n"
        "1. First line of item one.\n"
        "   Second line, still item one.\n"
        "2. Item two.\n"
    )
    assert [n for n, _ in items] == [1, 2]
    assert "Second line" in dict(items)[1]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("scripts/spec-context-oracle.py", True),
        ("CLAUDE.md", True),
        ("SPEC_READY", False),
        ("https://example.com/foo.py", False),
        ("--force", False),
        ("245eb284", False),
        ("backend/discussion_status.py::get_sections", True),
    ],
)
def test_looks_like_path(token, expected):
    assert lint_module._looks_like_path(token) is expected
