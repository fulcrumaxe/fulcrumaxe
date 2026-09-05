"""
tests/test_spec_frontmatter_placement.py

Reproduces D#1808's criterion 3: the project-manager spawn template's own
worked frontmatter example must parse non-empty through BOTH frontmatter
readers --

  - backend/task_specs._parse_frontmatter, anchored to the
    ``<!-- STATUS:... -->`` comment (backend/task_specs.py:46)
  - backend/spec_file_list.extract_file_list, which matches the first bare
    ``---`` block anywhere in the body

This test reads the live template rather than embedding a copy of the
example. A fixture-only test can't catch the template drifting back to an
unparseable placement -- that drift is the entire failure mode this bug
was about.

The template path can be overridden with the SPEC_FRONTMATTER_TEMPLATE_PATH
env var. That's a design requirement (not an afterthought): it's what lets
this exact, unmodified test be pointed at a mutated /tmp copy of the
template to prove the test can fail, without shadowing or stubbing either
parser.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.task_specs import _parse_frontmatter
from backend.spec_file_list import extract_file_list

DEFAULT_TMPL_PATH = REPO_ROOT / "backend" / "spawn_templates" / "project-manager.tmpl"
_FENCE = "```"


def _template_path() -> Path:
    override = os.environ.get("SPEC_FRONTMATTER_TEMPLATE_PATH")
    return Path(override) if override else DEFAULT_TMPL_PATH


def _worked_example_body() -> str:
    """Extract the fenced worked example containing 'estimated_hours:' from
    the template on disk -- not a copy embedded in this file -- so a drift
    in the template's placement changes what this test checks."""
    text = _template_path().read_text(encoding="utf-8")
    candidates = re.findall(_FENCE + r"\n(.*?)" + _FENCE, text, re.DOTALL)
    matches = [b for b in candidates if "estimated_hours:" in b]
    assert matches, (
        f"no fenced block containing 'estimated_hours:' found in "
        f"{_template_path()}"
    )
    return matches[0]


def test_worked_example_parses_via_task_specs():
    """task_specs._parse_frontmatter must return a non-empty dict for the
    template's own worked example -- the parser anchored to the STATUS
    comment (backend/task_specs.py:46)."""
    body = _worked_example_body()
    fm = _parse_frontmatter(body)
    assert fm, "task_specs returned empty"
    assert "estimated_hours" in fm
    assert "complexity_points" in fm


def test_worked_example_parses_via_spec_file_list():
    """spec_file_list.extract_file_list must also return a non-empty list
    for the same worked example, confirming the fix doesn't regress the
    parser that already resolved under both placements."""
    body = _worked_example_body()
    fl = extract_file_list(body)
    assert fl, "spec_file_list returned empty"
