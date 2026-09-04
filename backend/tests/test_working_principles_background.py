"""Tests for the D#1881 "never wait on a background run" guidance.

Verifies the §8 addition to working_principles_block() (scripts/lib/working-principles.sh)
actually reaches the RENDERED spawn prompt for executor, code-reviewer, and acceptance-tester
— not just the template/heredoc source. Each of the four grep markers is checked against each
of the three roles as an independent, individually-failing assertion (12 total), per D#1881
acceptance item 4.

D#2253 replaced the old per-role whole-prompt growth bound that used to live here (a frozen
per-role baseline plus a fixed growth ceiling) with a single size assertion on the shared block
itself — see `test_working_principles_block_size_bounded` below and
scripts/ci/working-principles-size.py, which is the real CI gate (wired into the `backend` job).
The old bound measured the entire rendered role prompt against a frozen 2026-08 snapshot and was
red on all three roles, mostly from an unrelated shared fragment (hard-stop-no-claude.md) charged
in full to every role.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import render  # noqa: E402

_ROLES = ("executor", "code-reviewer", "acceptance-tester")

# The four independent markers named in D#1881 acceptance item 2 (M1-M4).
_MARKERS = (
    "### 8. Never Wait on a Background Run",
    "re-invokes you",
    "timeout ",
    "unbounded_background_run",
)

# Import the CI size-gate script directly (its filename has hyphens, so it
# can't be `import`ed by name) so this test reads the same MAX_CHARS the
# real CI step enforces, instead of hardcoding a second copy that could
# drift from it.
_SIZE_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ci" / "working-principles-size.py"
_size_spec = importlib.util.spec_from_file_location("working_principles_size", _SIZE_SCRIPT_PATH)
_size_mod = importlib.util.module_from_spec(_size_spec)  # type: ignore[arg-type]
_size_spec.loader.exec_module(_size_mod)  # type: ignore[union-attr]

_MAX_BLOCK_CHARS = _size_mod.MAX_CHARS


def _working_principles_block() -> str:
    """Shell out to the real helper — mirrors how pre-spawn-check.sh fills the template var."""
    result = subprocess.run(
        ["bash", "scripts/lib/working-principles.sh", "working_principles_block"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def rendered_prompts() -> dict:
    wp = _working_principles_block()
    return {
        role: render(role, {"working_principles": wp}, ignore_unknown=True)
        for role in _ROLES
    }


@pytest.mark.parametrize("marker", _MARKERS)
@pytest.mark.parametrize("role", _ROLES)
def test_marker_present_in_rendered_prompt(rendered_prompts, role, marker):
    """Each of the 4 markers must appear in each of the 3 rendered prompts (12 assertions)."""
    assert marker in rendered_prompts[role], (
        f"marker {marker!r} missing from rendered {role!r} prompt "
        f"(rendered via backend.spawn_templates.render, not the template source)"
    )


def test_working_principles_block_size_bounded():
    """The shared block itself must stay under the CI-enforced limit (D#2253).

    Measures the same block via the same helper as
    scripts/ci/working-principles-size.py, and checks it against that
    script's MAX_CHARS constant — so this test and the real CI gate can
    never disagree about the limit.
    """
    block = _working_principles_block()
    size = len(block)
    assert size <= _MAX_BLOCK_CHARS, (
        f"working-principles block grew to {size} characters "
        f"(limit {_MAX_BLOCK_CHARS}); see scripts/ci/working-principles-size.py"
    )
