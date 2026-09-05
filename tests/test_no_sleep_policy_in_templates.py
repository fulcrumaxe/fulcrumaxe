"""
Verify that all Bash-using spawn templates contain the canonical ## Bash discipline section,
and that non-Bash specialist templates do NOT contain it.

Templates may embed the section inline or via {{include:bash-discipline}} fragment.
The test expands fragment includes before checking so both approaches are accepted.
"""
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TMPL_DIR = REPO_ROOT / "backend" / "spawn_templates"
FRAGMENTS_DIR = TMPL_DIR / "fragments"

REQUIRED = [
    "executor",
    "code-reviewer",
    "project-manager",
    "browser-tester",
    "docs-writer",
    "release-manager",
    "runbook-writer",
    "incident-commander",
    "run-analyst",
    "security-reviewer",
]

NON_BASH = [
    "acceptance-tester",
    "cost-analyst",
    "performance-expert",
    "product-owner",
    "technical-architect",
    "security-expert",
]


def _expand_fragments(body: str) -> str:
    """Expand {{include:NAME}} directives with fragment file content."""
    def _replace(m: re.Match) -> str:
        name = m.group(1)
        frag = FRAGMENTS_DIR / f"{name}.md"
        if frag.exists():
            return frag.read_text(encoding="utf-8")
        return m.group(0)  # leave unexpanded if fragment missing

    return re.sub(r'\{\{include:([^}]+)\}\}', _replace, body)


def test_all_required_templates_have_bash_discipline():
    missing = []
    for role in REQUIRED:
        raw = (TMPL_DIR / f"{role}.tmpl").read_text()
        body = _expand_fragments(raw)
        if "## Bash discipline" not in body:
            missing.append(f"{role}: section header")
        if "No-sleep rate-limit policy" not in body:
            missing.append(f"{role}: no-sleep block")
        if "Check exit codes before consuming output" not in body:
            missing.append(f"{role}: exit-code block")
        if 'blocked_reason: "rate_limit"' not in body:
            missing.append(f"{role}: blocked_reason example")
    assert not missing, "Missing policy in templates: " + ", ".join(missing)


def test_non_bash_templates_lack_bash_discipline():
    """Non-Bash specialist templates should not have the Bash discipline section."""
    present = []
    for role in NON_BASH:
        tmpl_path = TMPL_DIR / f"{role}.tmpl"
        if not tmpl_path.exists():
            continue
        raw = tmpl_path.read_text()
        body = _expand_fragments(raw)
        if "## Bash discipline" in body:
            present.append(role)
    assert not present, (
        "Non-Bash templates should not contain ## Bash discipline: " + ", ".join(present)
    )
