#!/usr/bin/env bash
# tests/test_spawn_template_repo_plane.sh
#
# D#2538: PR-scoped `gh pr` / `gh api repos/` calls in spawn templates must
# render {{CODE_REPO}} (the code plane, where PRs/CI/labels live), never
# {{REPO}} (the Discussion plane). Before this fix, ten sites across five
# templates rendered `--repo {{REPO}}` for a call that targets a PR living on
# the public code plane:
#
#   accessibility-reviewer.tmpl   gh pr diff / gh pr comment / gh pr edit
#   docs-writer.tmpl              gh pr diff / gh pr view / gh pr comment
#   runbook-writer.tmpl           gh pr diff / gh pr comment
#   run-analyst.tmpl              gh pr list
#   release-manager.tmpl          gh pr comment
#
# (The Discussion measured 8 sites across the first three files; re-deriving
# the full `{{REPO}}` site list per item 1 of the Spec surfaced the same
# defect at run-analyst.tmpl and release-manager.tmpl too.)
#
# PR numbers collide across the two planes, so the wrong-plane call doesn't
# fail loudly -- it silently resolves to a different PR, or to nothing.
#
# Discussion/Issue/team-log calls in the *same* templates legitimately stay
# bound to {{REPO}} (e.g. incident-commander's `gh issue create`) -- a blanket
# {{REPO}}->{{CODE_REPO}} sweep would be a regression, not a fix, so this test
# also pins that Discussion-plane call to make sure it is never swept.
#
# Every check here asserts on backend.spawn_templates.render_body()'s
# RENDERED OUTPUT, never on template source text: the template is the input,
# the rendered command is what actually reaches `gh`. A source-text grep
# would also miss the two sites (docs-writer.tmpl, runbook-writer.tmpl,
# release-manager.tmpl) where `--repo` sits on a shell continuation line
# after the `gh pr` command it belongs to.
#
# A trailing repo-wide sweep re-derives the site list structurally (any
# logical `gh pr`/`gh api repos/` invocation line, across every *.tmpl file,
# that still literally pairs with `--repo {{REPO}}`) so a future regression
# -- or a template this Discussion didn't know about -- fails here too,
# without duplicating scripts/ci/unpinned-gh-pr-guard.sh (deferred to D#...
# per PR #165, not yet on main as of this PR).
#
# Usage: bash tests/test_spawn_template_repo_plane.sh
# Exits 0 if all checks pass, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 - "$REPO_ROOT" <<'PYEOF'
import re
import sys
from pathlib import Path

REPO_ROOT = Path(sys.argv[1])
sys.path.insert(0, str(REPO_ROOT))

from backend import spawn_templates as st  # noqa: E402

DISCUSSION = "disc-org/disc-repo"
CODE = "code-org/code-repo"


def render(role: str) -> str:
    return st.render_body(
        role,
        vars={"REPO": DISCUSSION, "CODE_REPO": CODE},
        ignore_unknown=True,
    )


def logical_lines(text: str) -> list[str]:
    """Join backslash-continued shell lines into single logical lines.

    This is what makes the continuation-line sites (docs-writer.tmpl,
    runbook-writer.tmpl, release-manager.tmpl) visible to a substring check
    at all -- their `--repo` token is one physical line below the `gh pr`
    command it belongs to.
    """
    out: list[str] = []
    buf = ""
    for raw in text.split("\n"):
        line = raw.rstrip()
        if buf:
            buf = buf + " " + line.strip()
        else:
            buf = line
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


# (role, anchor substring identifying the gh call) -- one entry per real
# code-plane site found by re-deriving the {{REPO}} enumeration on the code
# plane (Spec item 1).
CODE_PLANE_SITES = [
    ("accessibility-reviewer", "gh pr diff"),
    ("accessibility-reviewer", "gh pr comment"),
    ("accessibility-reviewer", "gh pr edit"),
    ("docs-writer", "gh pr diff"),
    ("docs-writer", "gh pr view"),
    ("docs-writer", "gh pr comment"),
    ("runbook-writer", "gh pr diff"),
    ("runbook-writer", "gh pr comment"),
    ("run-analyst", "gh pr list"),
    ("release-manager", "gh pr comment"),
]

# A Discussion-plane call in one of the SAME templates being touched, pinned
# so a blanket {{REPO}}->{{CODE_REPO}} sweep (Spec item 3's explicit warning)
# would fail here.
DISCUSSION_PLANE_SITES = [
    ("incident-commander", "gh issue create"),
]

failures: list[str] = []

for role, anchor in CODE_PLANE_SITES:
    rendered = render(role)
    matches = [ln for ln in logical_lines(rendered) if anchor in ln and "--repo" in ln]
    if not matches:
        failures.append(f"{role} ({anchor}): no rendered line found with --repo")
        continue
    for ln in matches:
        if CODE not in ln:
            failures.append(f"{role} ({anchor}): rendered call missing code-plane slug: {ln!r}")
        if DISCUSSION in ln:
            failures.append(
                f"{role} ({anchor}): rendered call still carries the Discussion-plane slug: {ln!r}"
            )

for role, anchor in DISCUSSION_PLANE_SITES:
    rendered = render(role)
    matches = [ln for ln in logical_lines(rendered) if anchor in ln and "--repo" in ln]
    if not matches:
        failures.append(f"{role} ({anchor}): no rendered line found with --repo")
        continue
    for ln in matches:
        if DISCUSSION not in ln:
            failures.append(f"{role} ({anchor}): rendered call missing Discussion-plane slug: {ln!r}")
        if CODE in ln:
            failures.append(
                f"{role} ({anchor}): rendered call picked up the code-plane slug -- "
                f"looks like a blanket sweep regression: {ln!r}"
            )

# Structural sweep across every *.tmpl file: no logical line that invokes
# `gh pr ...` or `gh api repos/...` may still pair with the literal
# `--repo {{REPO}}` placeholder. Source-text based (not rendered), so it
# covers templates/sites this Discussion's enumeration did not name.
tmpl_dir = REPO_ROOT / "backend" / "spawn_templates"
tmpl_files = sorted(tmpl_dir.glob("*.tmpl"))
invocation_re = re.compile(r"(^|[`$(\s])gh pr\b|(^|[`$(\s])gh api repos/")
sweep_hits = 0
for f in tmpl_files:
    for ln in logical_lines(f.read_text()):
        stripped = ln.strip()
        if not invocation_re.search(stripped):
            continue
        if "--repo {{REPO}}" in ln:
            sweep_hits += 1
            failures.append(f"{f.name}: PR-scoped gh call still binds {{{{REPO}}}}: {stripped!r}")

if failures:
    for msg in failures:
        print(f"FAIL: {msg}")
    sys.exit(1)

print(
    f"PASS: {len(CODE_PLANE_SITES)} code-plane site(s) render {{{{CODE_REPO}}}}, "
    f"{len(DISCUSSION_PLANE_SITES)} Discussion-plane site(s) still render {{{{REPO}}}}, "
    f"sweep clean across {len(tmpl_files)} template(s)"
)
sys.exit(0)
PYEOF
