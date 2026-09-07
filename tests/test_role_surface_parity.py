"""tests/test_role_surface_parity.py

D#2196: a role is defined across three surfaces and nothing keeps them in
sync -- .claude/agents/*.md (required: Claude Code's Agent(subagent_type=...)
loader, and scripts/spawn-agent.sh:577 reads its `model:` frontmatter),
backend/spawn_templates/*.tmpl (optional: supplies the template body and,
with it, the {{include:...}} fragment block -- a role with no .tmpl still
spawns, it just gets no template body), and .autonomous-team/agents/*.json
(optional: display/capability metadata for backend/agent_cards.py, the
dashboard, and validate-workflow -- absence is a warning, not a block).

This is deliberately NOT a three-way equality check. `team-lead` is
json-only by design -- it is the top-level conversation, never spawned via
subagent_type, so it correctly has no card and no template. A guard that
cannot express that legitimate asymmetry gets deleted or exception-flooded
rather than kept honest. Drift is asserted against a declared exception
table (ROLE_SURFACE_EXCEPTIONS below), modeled directly on
RENDER_EMPTY_BY_DESIGN in backend/spawn_var_contract.py:51 -- a
role -> one-line-reason mapping, not a silent carve-out.

Prior art for structure: tests/test_no_dead_role_refs.py (same three
directories, tracked-files-only scan via testsupport.git_tracked, exclusion
table with recorded reasons -- D#2202 fixed an untracked generated .json
under .autonomous-team/ making that guard pass on CI and fail on an
operator checkout, and this test reuses the same fix).

The three scan directories are read from module-level constants overridable
by env var (prior art: SPEC_FRONTMATTER_TEMPLATE_PATH in
tests/test_spec_frontmatter_placement.py). This is what lets this exact,
unmodified test be pointed at an empty directory to prove it can actually
fail on a vacuous scan of any one of the three surfaces -- see the D#2196
PR body for the three transcripts (one per surface) plus the drift-detection
transcript (git mv executor.json out and back).

One surface is not universal across checkouts of this codebase, discovered
while wiring this test's delivery to the code plane: .autonomous-team/ is
deliberately never part of the public code-plane export (D#1870; see
scripts/lib/repo-resolve.sh's "`.autonomous-team/` never ships in the
open-source export" comment, and the export-history commits under
code-plane/main that delete it wholesale). So a checkout of the public repo
has no .autonomous-team/agents/ directory at all -- not an empty one, an
absent one. `_surface_applies` treats "the directory does not exist on disk"
as "this surface is not part of this checkout's contract" and excludes it
from both the anti-vacuity check and the parity comparison, while an
existing-but-empty directory (what the env-var override above produces for
the demonstration) still hard-fails exactly as Spec item 3 describes. Cards
and templates are shipped identically on both planes, so in practice this
only ever affects the json surface, and only on the public checkout.

This module does not replace tests/test_spawn_templates_known_roles.py's
KNOWN_ROLES-vs-glob comparison (that test is vacuous -- KNOWN_ROLES *is* the
glob it's compared against, see backend/spawn_templates.py:190-192 -- and
fixing that is a separate, narrower defect from three-surface membership
drift). It also does not replace tests/test_no_dead_role_refs.py, which
answers "does a dead role name appear anywhere" -- a different question from
"does every live role appear everywhere it should".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
from testsupport.git_tracked import git_tracked_files

# Overridable via env var so the anti-vacuity demonstration (Spec item 3)
# can point one surface at an empty directory without modifying this file.
CARDS_DIR = Path(
    os.environ.get("ROLE_PARITY_CARDS_DIR") or str(REPO_ROOT / ".claude" / "agents")
)
TEMPLATES_DIR = Path(
    os.environ.get("ROLE_PARITY_TEMPLATES_DIR")
    or str(REPO_ROOT / "backend" / "spawn_templates")
)
JSON_DIR = Path(
    os.environ.get("ROLE_PARITY_JSON_DIR") or str(REPO_ROOT / ".autonomous-team" / "agents")
)

# role -> one-line reason a missing surface is fine. Modeled on
# RENDER_EMPTY_BY_DESIGN (backend/spawn_var_contract.py:51). Keep this list
# honest: an entry here silences the drift failure for that role, so it
# should only ever name a role whose asymmetry is a design decision, not an
# oversight. test_exceptions_have_reasons enforces every entry is non-empty.
ROLE_SURFACE_EXCEPTIONS: dict[str, str] = {
    "team-lead": (
        "top-level conversation, never spawned via subagent_type -- "
        "json-only by design, no card and no template"
    ),
    "feedback-scanner": (
        "template-less by design (D#2196): no .tmpl means it receives no "
        "{{include:...}} fragment block (bash-discipline, archive-protocol, "
        "hard-stop-no-claude, rate-limit-policy) -- its card carries the "
        "Repo Scope constraint independently instead"
    ),
    "visual-verifier": (
        "template-less by design (D#2196): same fragment-block gap as "
        "feedback-scanner -- its card carries the Repo Scope constraint "
        "independently instead"
    ),
}


def _scan(dir_path: Path, suffix: str) -> set[str]:
    """Return the role names (file stems) present under dir_path with suffix.

    Restricted to git-tracked files when dir_path resolves inside the repo --
    the normal case -- for the same reason as test_no_dead_role_refs.py: an
    untracked, locally-generated file under .autonomous-team/ must not change
    the verdict (D#2202). When dir_path is an override that resolves outside
    the repo (only exercised by the anti-vacuity demonstration, which points
    at a throwaway empty temp directory), there is no git index to consult
    for it, so this falls back to a plain directory listing -- which still
    correctly reports nothing for an empty directory.
    """
    if not dir_path.is_dir():
        return set()

    resolved = dir_path.resolve()
    try:
        inside_repo = resolved.is_relative_to(REPO_ROOT)
    except AttributeError:  # pragma: no cover - Path.is_relative_to is 3.9+
        inside_repo = str(resolved).startswith(str(REPO_ROOT) + os.sep)

    if inside_repo:
        tracked = git_tracked_files(REPO_ROOT, dir_path)
        return {
            p.stem
            for p in dir_path.iterdir()
            if p.is_file() and p.suffix == suffix and p.resolve() in tracked
        }

    return {p.stem for p in dir_path.iterdir() if p.is_file() and p.suffix == suffix}


def _surface_applies(dir_path: Path) -> bool:
    """A surface directory that does not exist at all is not part of this
    checkout's contract -- e.g. .autonomous-team/ on a code-plane (public)
    checkout, see the module docstring. That is a different condition from
    an existing-but-empty directory, which is exactly the vacuous-scan bug
    this guard exists to catch.
    """
    return dir_path.is_dir()


def _surfaces() -> tuple[set[str], set[str], set[str]]:
    return (
        _scan(CARDS_DIR, ".md"),
        _scan(TEMPLATES_DIR, ".tmpl"),
        _scan(JSON_DIR, ".json"),
    )


def test_no_empty_surface_scan():
    """Refuse to pass on a vacuous scan of any surface this checkout has.

    This is the guard against the exact failure mode named in this Spec:
    tests/test_spawn_templates_known_roles.py compares KNOWN_ROLES against
    TMPL_DIR.glob("*.tmpl"), but KNOWN_ROLES is *derived from* that same
    glob -- both its assertions compare a set to itself and it can never
    fail. Run with ROLE_PARITY_CARDS_DIR / ROLE_PARITY_TEMPLATES_DIR /
    ROLE_PARITY_JSON_DIR pointed at an empty (but existing) directory to see
    this fail and name the empty surface -- see the D#2196 PR body for the
    transcripts. A surface directory that is absent rather than empty is
    skipped (see _surface_applies / module docstring), but at least one
    surface must exist, or there is nothing to compare at all.
    """
    cards, templates, json_cards = _surfaces()
    applicable = [
        (name, scanned)
        for name, dir_path, scanned in (
            ("cards", CARDS_DIR, cards),
            ("templates", TEMPLATES_DIR, templates),
            ("json", JSON_DIR, json_cards),
        )
        if _surface_applies(dir_path)
    ]
    assert applicable, (
        f"no surface directory exists at all among {CARDS_DIR}, {TEMPLATES_DIR}, "
        f"{JSON_DIR} -- nothing to compare"
    )
    for name, scanned in applicable:
        assert scanned, f"{name} surface scanned zero files"


def test_role_surface_parity():
    """Every role present in any applicable surface is present in every
    other applicable surface, or is named in ROLE_SURFACE_EXCEPTIONS with a
    reason. A surface whose directory does not exist at all in this
    checkout (see _surface_applies / module docstring) is excluded from the
    comparison rather than treated as "every role is missing from it".

    Prints the three counts compared (Spec item 2) whether this passes or
    fails -- run with `-s` to see them.
    """
    cards, templates, json_cards = _surfaces()
    union = cards | templates | json_cards
    print(
        f"cards={len(cards)} templates={len(templates)} json={len(json_cards)} "
        f"union={len(union)}"
    )

    applicable_surfaces = [
        (name, surface)
        for name, dir_path, surface in (
            ("cards", CARDS_DIR, cards),
            ("templates", TEMPLATES_DIR, templates),
            ("json", JSON_DIR, json_cards),
        )
        if _surface_applies(dir_path)
    ]

    failures = []
    for role in sorted(union):
        missing = [
            surface_name for surface_name, surface in applicable_surfaces if role not in surface
        ]
        if not missing:
            continue
        if role in ROLE_SURFACE_EXCEPTIONS:
            continue
        failures.append(f"{role}: missing from {', '.join(missing)}")

    assert not failures, (
        "role surface drift not covered by ROLE_SURFACE_EXCEPTIONS "
        "(add the missing surface file, or add a role -> reason entry to "
        "ROLE_SURFACE_EXCEPTIONS in this file):\n" + "\n".join(failures)
    )


def test_exceptions_have_reasons():
    """Every ROLE_SURFACE_EXCEPTIONS entry carries a non-empty reason string."""
    empty = [role for role, reason in ROLE_SURFACE_EXCEPTIONS.items() if not reason.strip()]
    assert not empty, f"ROLE_SURFACE_EXCEPTIONS entries with an empty reason: {empty}"
