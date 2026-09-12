"""backend/tests/test_spawn_anchoring.py — D#2434 AC-8 / AC-9.

The consensus panel's technical-architect measured, in Round 1, that every
instruction asset a spawned agent loads resolves against the operator
checkout — never against the PR tree the agent then works in. That
measurement is why D#2434 ships as detect-and-tell rather than
revert-to-base: a hostile head substitutes no role card, template, or hook.

This file converts that measurement into a regression gate (AC-8) and pins
a related, previously-unasserted property: trust-set resolution reads the
operator checkout's config, not whatever `.autonomous-team/config.json` a
process's current working directory happens to contain (AC-9).

Neither AC requires a code change — both properties already hold. The
deliverable is the test that pins them, per the Implementation Notes: "If
the current implementation already has this property, the deliverable is
the test that pins it, not a rewrite."
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(_REPO_ROOT))

_SPAWN_AGENT_SH = (_REPO_ROOT / "scripts" / "spawn-agent.sh").read_text()


# ---------------------------------------------------------------------------
# AC-8 — instruction-asset anchoring resolves to the operator checkout,
# never to a PR tree. Four load-bearing sites, all cited by the
# technical-architect's Round 1 comment.
# ---------------------------------------------------------------------------


def test_repo_root_is_derived_from_this_scripts_own_location():
    """spawn-agent.sh:48-49 — REPO_ROOT comes from `dirname "${BASH_SOURCE[0]}"`,
    i.e. where this script itself lives on disk, never from a --pr argument
    or a worktree path handed in at spawn time. A PR tree cannot make this
    resolve anywhere else."""
    assert re.search(
        r'SCRIPT_DIR="\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)" && pwd\)"',
        _SPAWN_AGENT_SH,
    )
    assert re.search(r'REPO_ROOT="\$\(cd "\$SCRIPT_DIR/\.\." && pwd\)"', _SPAWN_AGENT_SH)


def test_role_cards_resolve_under_repo_root_not_a_pr_tree():
    """spawn-agent.sh:639,:1102 — both role-card read sites are anchored to
    ${REPO_ROOT}/.claude/agents/, not to any path derived from the PR being
    worked on."""
    assert '_ROLE_CARD="${REPO_ROOT}/.claude/agents/${ROLE}.md"' in _SPAWN_AGENT_SH
    assert '_ROLE_CARD_PATH="${REPO_ROOT}/.claude/agents/${ROLE}.md"' in _SPAWN_AGENT_SH


def test_prompt_assembly_runs_with_pythonpath_pinned_to_repo_root():
    """spawn-agent.sh:~1029,~1043 — backend.spawn_payload and
    backend.prompt_builder, which assemble the spawn prompt, both run with
    PYTHONPATH pinned to $REPO_ROOT, so the operator's own backend/ package
    is what gets imported regardless of what a PR tree contains at the same
    dotted path."""
    assert 'PYTHONPATH="$REPO_ROOT" python3 -m backend.spawn_payload' in _SPAWN_AGENT_SH
    assert 'PYTHONPATH="$REPO_ROOT" python3 -m backend.prompt_builder' in _SPAWN_AGENT_SH


def test_spawn_templates_resolve_relative_to_this_file_not_cwd():
    """backend/spawn_templates.py:42 — _TEMPLATES_DIR is Path(__file__).parent,
    so it always resolves to the operator's own backend/spawn_templates/,
    never to wherever the process's cwd happens to be when a template is
    rendered."""
    import backend.spawn_templates as spawn_templates  # noqa: PLC0415

    assert spawn_templates._TEMPLATES_DIR == Path(spawn_templates.__file__).parent / "spawn_templates"
    assert spawn_templates._TEMPLATES_DIR.is_relative_to(_REPO_ROOT)


def test_every_settings_hook_command_is_anchored_to_claude_project_dir():
    """.claude/settings.json — every `hooks/*.py` or `hooks/*.sh` hook
    command is spelled with $CLAUDE_PROJECT_DIR, never a relative or
    head-relative path. A relative spelling would resolve inside whatever
    tree the hook happens to run from, including a PR checkout."""
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())

    commands = []

    def _walk(node):
        if isinstance(node, dict):
            command = node.get("command")
            if isinstance(command, str):
                commands.append(command)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(settings.get("hooks", {}))

    hook_commands = [c for c in commands if "hooks/" in c]
    assert hook_commands, "expected at least one hooks/ reference in .claude/settings.json"
    for command in hook_commands:
        assert "$CLAUDE_PROJECT_DIR/hooks/" in command, (
            f"hook command is not anchored to $CLAUDE_PROJECT_DIR: {command!r} — this "
            "would resolve relative to whatever tree the hook process happens to run "
            "from, including a PR checkout, rather than the operator's own hooks/."
        )


# ---------------------------------------------------------------------------
# AC-9 — trust (`maintainer_allowlist`) is resolved from the operator
# checkout, never from a process's current working directory.
# ---------------------------------------------------------------------------


def test_maintainer_allowlist_config_path_is_anchored_to_repo_root():
    import external_intake_gate as eig  # noqa: PLC0415

    assert eig._REPO_ROOT == _REPO_ROOT
    assert eig._DEFAULT_CONFIG_PATH == _REPO_ROOT / ".autonomous-team" / "config.json"


def test_resolve_allowlist_ignores_a_config_dropped_at_the_process_cwd(monkeypatch, tmp_path):
    """Simulates a gate invoked with cwd inside a PR tree that ships its own
    .autonomous-team/config.json naming an attacker as a maintainer. The real
    config lookup must not be swayed by cwd — it always reads the operator
    checkout's own config, because _load_config()'s default path is the
    module-level _DEFAULT_CONFIG_PATH constant, computed once from
    Path(__file__).resolve() at import time, not from cwd at call time."""
    import external_intake_gate as eig  # noqa: PLC0415

    fake_pr_tree = tmp_path / "pr-tree"
    (fake_pr_tree / ".autonomous-team").mkdir(parents=True)
    (fake_pr_tree / ".autonomous-team" / "config.json").write_text(
        json.dumps({"maintainer_allowlist": ["attacker-controlled-login"]})
    )
    monkeypatch.chdir(fake_pr_tree)

    cfg = eig._load_config()
    assert "attacker-controlled-login" not in (cfg.get("maintainer_allowlist") or [])
