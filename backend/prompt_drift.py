"""
Spawn-prompt drift detector.

Compares each role's rendered spawn prompt against the canonical gate-check
rules declared in its ``.claude/agents/<role>.md`` card (with CLAUDE.md's
legacy inline sections merged in as a fallback) and reports any role whose
prompt is missing a required gate key.

The role universe is the known-roles set exported by ``backend.spawn_templates``
(imported below as ``_SPAWN_TEMPLATE_ROLES``) -- the roles with a spawn
template on disk (24 as of this writing). Deriving from a filesystem-backed
set keeps the denominator honest as roles are added or removed; see
``tests/test_spawn_templates_known_roles.py`` for the guard that keeps that
set itself accurate.

Every role in the universe classifies into exactly one bucket:
    - CHECKED     -- card declares >=1 gate key. Its prompt is rendered and
                     compared against those keys.
    - NO_RULES    -- card exists but declares zero gate keys. Reported,
                     never fatal, UNLESS the role is in REQUIRED_RULE_ROLES
                     and has no entry in prompt_drift_exemptions.json, in
                     which case it is a fatal "missing required rules"
                     failure.
    - UNREACHABLE -- no readable card at .claude/agents/<role>.md, or
                     rendering its prompt raised. Always fatal.

Usage (library):
    from backend.prompt_drift import extract_claude_md_rules, check_all
    report = check_all()

Usage (CLI):
    python3 backend/prompt_drift.py check
    # exits 0 if clean, 1 if drift/missing-rules/unreachable, 2 on error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

# Allow `python3 backend/prompt_drift.py` to work when invoked from the repo
# root. This MUST run before any `backend.*` import below -- reversing the
# order raises ModuleNotFoundError for anyone running the command exactly as
# documented above.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend._repo import REPO
from backend.spawn_templates import KNOWN_ROLES as _SPAWN_TEMPLATE_ROLES

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Regex to extract gates.X and policies.X.Y tokens from bash blocks
_GATE_KEY_RE = re.compile(
    r'\b(gates\.[a-z_]+|policies\.[a-z_]+\.[a-z_]+)\b'
)

# Section header pattern: #### {Role} — Control Plane Gates (legacy CLAUDE.md style)
_ROLE_SECTION_RE = re.compile(
    r'^####\s+([\w\s-]+?)\s+[—\-]+\s+Control Plane Gates\s*$',
    re.MULTILINE,
)

# Section header pattern for per-role agent files: ## Control Plane Gates
_AGENT_GATE_SECTION_RE = re.compile(
    r'^##\s+Control Plane Gates\s*$',
    re.MULTILINE,
)

# Any ## or ### header — bounds sections in per-role agent files
_ANY_H2_OR_H3_RE = re.compile(r'^#{2,3}\s+', re.MULTILINE)

# Any #### header — used to bound sections so subsections don't bleed into the
# previous role's content (e.g. "#### Training Triggers Gate" lives after the
# project-manager gate section but is NOT part of that role's checks).
_ANY_H4_RE = re.compile(r'^####\s+', re.MULTILINE)

# Fenced bash block pattern
_BASH_BLOCK_RE = re.compile(
    r'```bash\s*\n(.*?)```',
    re.DOTALL,
)

# Map section header names to canonical role names -- used only by the
# legacy CLAUDE.md inline-section fallback path in extract_claude_md_rules.
_HEADER_TO_ROLE: dict[str, str] = {
    "Executor": "executor",
    "Code-Reviewer": "code-reviewer",
    "Security-Reviewer": "security-reviewer",
    "Project Manager": "project-manager",
    "Acceptance-Tester": "acceptance-tester",
    # Alternative spellings
    "Code Reviewer": "code-reviewer",
    "Security Reviewer": "security-reviewer",
    "Acceptance Tester": "acceptance-tester",
}

# Roles whose Control Plane Gates section gates a real merge decision. If one
# of these declares zero gate keys, that is fatal UNLESS the role has an
# entry in prompt_drift_exemptions.json explaining why (see _load_exemptions
# and DriftReport.exempt). This is the same five roles the old (now deleted)
# _AGENT_FILE_TO_ROLE map named -- that map encoded a real intent, it just
# used the wrong mechanism (a role-count limiter) to express it.
REQUIRED_RULE_ROLES: frozenset[str] = frozenset({
    "executor",
    "code-reviewer",
    "security-reviewer",
    "project-manager",
    "acceptance-tester",
})

# Ledger of roles that are exempt from REQUIRED_RULE_ROLES enforcement, with
# a stated reason. Data only, no logic -- see backend/spec_external_docs_allowlist.txt
# for precedent of a sibling plain-data file in backend/.
_EXEMPTIONS_PATH = Path(__file__).parent / "prompt_drift_exemptions.json"


class RoleRules(NamedTuple):
    """Rules extracted from a role's card for a single role."""
    gate_keys: frozenset[str]  # e.g. {"gates.lint_must_pass", "policies.executor.pr_size_max_lines"}


@dataclass
class DriftIssue:
    """A single drift finding for a role."""
    role: str
    missing_key: str  # e.g. "gates.lint_must_pass"

    def __str__(self) -> str:
        return f"{self.role}: missing key {self.missing_key}"


@dataclass
class DriftReport:
    """Result of check_all() -- an honest, bucketed accounting of every role
    in the known role universe. See module docstring for bucket definitions.
    """

    total_roles: int
    checked: dict[str, list[DriftIssue]]
    no_rules: list[str]
    exempt: list[str]
    missing_required: list[str]
    unreachable: list[tuple[str, str]]  # (role, detail)

    @property
    def checked_count(self) -> int:
        return len(self.checked)

    @property
    def drift_issue_count(self) -> int:
        return sum(len(v) for v in self.checked.values())

    @property
    def is_fatal(self) -> bool:
        return (
            bool(self.unreachable)
            or bool(self.missing_required)
            or self.drift_issue_count > 0
        )


# ---------------------------------------------------------------------------
# Rule extraction
# ---------------------------------------------------------------------------

def _extract_gate_keys_from_agent_file(agent_path: Path) -> frozenset[str]:
    """Extract gate keys from the ``## Control Plane Gates`` section of a per-role agent file.

    Reads up to (but not including) the next ``##`` or ``###`` header so that
    subsections like ``## Self-Observe Gate`` don't bleed in.  Only keys
    appearing inside fenced bash blocks are captured.
    """
    try:
        text = agent_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()

    match = _AGENT_GATE_SECTION_RE.search(text)
    if not match:
        return frozenset()

    section_start = match.end()
    # Bound by the next ## or ### header (any kind)
    next_h = _ANY_H2_OR_H3_RE.search(text, section_start)
    section_end = next_h.start() if next_h else len(text)
    section_text = text[section_start:section_end]

    gate_keys: set[str] = set()
    for bash_match in _BASH_BLOCK_RE.finditer(section_text):
        bash_content = bash_match.group(1)
        for key_match in _GATE_KEY_RE.finditer(bash_content):
            gate_keys.add(key_match.group(1))

    return frozenset(gate_keys)


def extract_claude_md_rules(path: Path) -> dict[str, RoleRules]:
    """Parse per-role agent files (and CLAUDE.md's legacy inline sections)
    into per-role gate-check key sets.

    Primary source: ``.claude/agents/<role>.md`` for every role in the known
    role universe (the roles exported by ``backend.spawn_templates``). A role
    whose card is readable always gets an entry -- ``RoleRules(gate_keys=frozenset())``
    if its ``## Control Plane Gates`` section is absent or empty -- so callers
    can tell "declares nothing" from "card not present". A role whose card is
    NOT readable gets no entry at all; ``check_all()`` treats that as
    UNREACHABLE.

    Fallback/legacy: if CLAUDE.md itself contains ``#### {Role} — Control Plane Gates``
    sections (the old inline style), those are also parsed and merged in
    (union of gate keys).

    Parameters
    ----------
    path:
        Absolute path to CLAUDE.md.  The ``.claude/agents/`` directory is
        resolved relative to ``path.parent``.

    Returns
    -------
    dict mapping role name -> RoleRules, one entry per role with a readable
    card.
    """
    rules: dict[str, RoleRules] = {}

    # --- Primary: per-role agent files in .claude/agents/, one entry per
    # role in the known role universe with a readable card. ---
    agents_dir = path.parent / ".claude" / "agents"
    for role in sorted(_SPAWN_TEMPLATE_ROLES):
        agent_file = agents_dir / f"{role}.md"
        if not agent_file.is_file():
            continue
        gate_keys = _extract_gate_keys_from_agent_file(agent_file)
        rules[role] = RoleRules(gate_keys=gate_keys)

    # --- Fallback: legacy inline sections in CLAUDE.md ---
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rules

    matches = list(_ROLE_SECTION_RE.finditer(text))
    for match in matches:
        header_name = match.group(1).strip()
        role = _HEADER_TO_ROLE.get(header_name)
        if role is None:
            continue  # section not a known role

        # Content of this section: starts after the matched header and runs
        # until the NEXT #### header of any kind.
        section_start = match.end()
        next_h4 = _ANY_H4_RE.search(text, section_start)
        section_end = next_h4.start() if next_h4 else len(text)
        section_text = text[section_start:section_end]

        # Extract gate keys from bash blocks only
        gate_keys_set: set[str] = set()
        for bash_match in _BASH_BLOCK_RE.finditer(section_text):
            bash_content = bash_match.group(1)
            for key_match in _GATE_KEY_RE.finditer(bash_content):
                gate_keys_set.add(key_match.group(1))

        if gate_keys_set:
            existing = rules.get(role, RoleRules(gate_keys=frozenset()))
            rules[role] = RoleRules(gate_keys=existing.gate_keys | frozenset(gate_keys_set))

    return rules


def _load_exemptions(path: Path | None = None) -> dict[str, str]:
    """Load the prompt_drift_exemptions.json ledger.

    Returns {} if the file is missing or malformed -- a missing/empty ledger
    means no role is exempt, which is the fail-loud direction (see
    DriftReport and REQUIRED_RULE_ROLES).
    """
    p = path if path is not None else _EXEMPTIONS_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# Drift checking
# ---------------------------------------------------------------------------

def _render_role_prompt(role: str) -> str:
    """Render a role prompt with safe placeholder variables.

    Uses ``backend.spawn_templates.render`` which unconditionally appends the
    gate-checks appendix, making this the correct surface to check.
    """
    # Import here to avoid circular deps and allow monkeypatching in tests
    from backend.spawn_templates import render, REQUIRED_VARS  # type: ignore[import]

    # Build minimal vars — use placeholder strings for all required/known fields.
    # pr_number is used by reviewer templates; add it as a placeholder too.
    vars_dict: dict[str, str] = {
        "discussion_number": "0",
        "discussion_title": "placeholder",
        "discussion_url": f"https://github.com/{REPO}/discussions/0",
        "task_brief": "placeholder",
        "project_context": "",
        "agent_memory": "",
        "gate_context": "",
        "pr_number": "0",
    }
    # Add any role-specific required vars that aren't in the defaults
    for var in REQUIRED_VARS.get(role, []):
        if var not in vars_dict:
            vars_dict[var] = "placeholder"

    return render(role, vars_dict)


def check_role(role: str, rules: RoleRules) -> list[DriftIssue]:
    """Check a single role's rendered prompt against its declared rules.

    Parameters
    ----------
    role:
        Canonical role name (e.g. ``"executor"``).
    rules:
        RoleRules extracted for this role.

    Returns
    -------
    List of DriftIssue — one per missing gate key.  Empty list means clean.
    """
    prompt = _render_role_prompt(role)
    issues: list[DriftIssue] = []
    for key in sorted(rules.gate_keys):
        if key not in prompt:
            issues.append(DriftIssue(role=role, missing_key=key))
    return issues


def check_all(
    claude_md_path: Path | None = None,
    exemptions_path: Path | None = None,
) -> DriftReport:
    """Run the drift check across the full known role universe.

    Every role in the known role universe (the roles exported by
    ``backend.spawn_templates``) is classified into exactly one bucket --
    checked, no_rules, exempt, missing_required, or unreachable. See the
    module docstring for bucket definitions.

    Parameters
    ----------
    claude_md_path:
        Override path to CLAUDE.md.  Defaults to the project root CLAUDE.md.
        The ``.claude/agents/`` directory is resolved relative to its parent.
    exemptions_path:
        Override path to the exemptions ledger.  Defaults to
        ``backend/prompt_drift_exemptions.json``.

    Returns
    -------
    DriftReport
    """
    if claude_md_path is None:
        claude_md_path = Path(__file__).parent.parent / "CLAUDE.md"

    agents_dir = claude_md_path.parent / ".claude" / "agents"
    exemptions = _load_exemptions(exemptions_path)
    rules_by_role = extract_claude_md_rules(claude_md_path)

    checked: dict[str, list[DriftIssue]] = {}
    no_rules: list[str] = []
    exempt: list[str] = []
    missing_required: list[str] = []
    unreachable: list[tuple[str, str]] = []

    for role in sorted(_SPAWN_TEMPLATE_ROLES):
        if role not in rules_by_role:
            agent_file = agents_dir / f"{role}.md"
            unreachable.append((role, f"no readable role card at {agent_file}"))
            continue

        rules = rules_by_role[role]

        if rules.gate_keys:
            try:
                issues = check_role(role, rules)
            except Exception as exc:
                agent_file = agents_dir / f"{role}.md"
                unreachable.append(
                    (role, f"{agent_file}: render() raised {type(exc).__name__}: {exc}")
                )
                continue
            checked[role] = issues
        elif role in REQUIRED_RULE_ROLES:
            if role in exemptions:
                exempt.append(role)
            else:
                missing_required.append(role)
        else:
            no_rules.append(role)

    return DriftReport(
        total_roles=len(_SPAWN_TEMPLATE_ROLES),
        checked=checked,
        no_rules=sorted(no_rules),
        exempt=sorted(exempt),
        missing_required=sorted(missing_required),
        unreachable=sorted(unreachable),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect spawn-prompt drift vs role gate definitions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_cmd = sub.add_parser(
        "check",
        help="Check all roles for drift and exit 1 if any found.",
    )
    check_cmd.add_argument(
        "--claude-md",
        type=Path,
        default=None,
        help="Path to CLAUDE.md (default: repo root CLAUDE.md).",
    )
    check_cmd.add_argument(
        "--exemptions",
        type=Path,
        default=None,
        help="Path to the exemptions ledger (default: backend/prompt_drift_exemptions.json).",
    )
    check_cmd.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-issue output; only print summary.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns exit code."""
    args = _parse_args(argv)

    if args.command == "check":
        try:
            report = check_all(
                claude_md_path=args.claude_md,
                exemptions_path=args.exemptions,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        if not args.quiet:
            for role in sorted(report.checked):
                for issue in report.checked[role]:
                    print(str(issue))
            for role, detail in report.unreachable:
                print(f"{role}: unreachable — {detail}")
            for role in report.missing_required:
                print(
                    f"{role}: missing required Control Plane Gates rules "
                    f"(no entry in prompt_drift_exemptions.json)"
                )

        summary_parts = [f"checked {report.checked_count} of {report.total_roles} roles"]
        if report.no_rules:
            summary_parts.append(f"{len(report.no_rules)} declare no gate rules")
        if report.exempt:
            summary_parts.append(f"{len(report.exempt)} exempt ({', '.join(report.exempt)})")
        summary = "; ".join(summary_parts)

        if report.is_fatal:
            fail_parts = []
            if report.drift_issue_count:
                fail_parts.append(f"{report.drift_issue_count} drift issue(s)")
            if report.missing_required:
                fail_parts.append(
                    f"{len(report.missing_required)} required role(s) missing rules "
                    f"({', '.join(report.missing_required)})"
                )
            if report.unreachable:
                names = ", ".join(r for r, _ in report.unreachable)
                fail_parts.append(f"{len(report.unreachable)} unreachable role(s) ({names})")
            print(f"prompt-drift: FAIL — {summary}; " + "; ".join(fail_parts))
            return 1
        else:
            print(f"prompt-drift: OK — {summary}; no drift")
            return 0

    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
