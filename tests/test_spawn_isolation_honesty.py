"""Guards against the D#2078 isolation-claim regression.

Every rendered role body used to hardcode "You are running inside a
sandboxed git worktree." — a static sentence that is wrong for any
non-isolated spawn, which was every panel spawn measured on D#2078. The
fix collapsed the 17 duplicated copies into one fragment
(backend/spawn_templates/fragments/hard-stop-no-claude.md) that asserts
nothing about isolation state and instead tells the agent how to check
its own root at runtime.

These three tests are the AC1/AC2/AC3 checks from the D#2078 Spec, run as
pytest so they execute in CI rather than only by hand.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.spawn_templates import render_body, KNOWN_ROLES

_ISOLATION_CLAIM_RE = re.compile(
    r"(?i)sandboxed git worktree|you are (running )?(inside|in) a sandboxed"
)
_SELF_CHECK_SENTINEL = "Confirm your actual root before assuming containment:"


def test_no_rendered_body_asserts_isolation():
    """AC1 — no rendered role body claims worktree isolation as fact.

    Mutation that must turn this red: restore the old hardcoded sentence
    in any one of the 17 templates that carry the HARD STOP block.
    """
    bad = [
        role
        for role in sorted(KNOWN_ROLES)
        if _ISOLATION_CLAIM_RE.search(render_body(role, {}, ignore_unknown=True))
    ]
    assert bad == [], f"role bodies still assert isolation: {bad}"


def test_every_claude_spawn_forbidden_body_carries_self_check():
    """AC2 — every body with the deny-list also carries the self-check.

    This exists because AC1 alone could be satisfied by deleting the HARD
    STOP block outright. Mutation that must turn this red: delete the
    sentinel line from fragments/hard-stop-no-claude.md.
    """
    bodies = {role: render_body(role, {}, ignore_unknown=True) for role in sorted(KNOWN_ROLES)}
    carriers = [role for role, body in bodies.items() if "claude_spawn_forbidden" in body]
    missing = [role for role in carriers if _SELF_CHECK_SENTINEL not in bodies[role]]

    assert len(carriers) == 17, f"expected 17 carriers, got {len(carriers)}: {carriers}"
    assert missing == [], f"carriers missing the self-check sentinel: {missing}"


def test_hard_stop_block_is_a_single_fragment_not_reinlined():
    """AC3 — one source, and it stays one source.

    Mutation that must turn this red: re-inline the HARD STOP block into
    any one template instead of using the include directive.
    """
    templates_dir = Path(__file__).resolve().parents[1] / "backend" / "spawn_templates"
    tmpl_files = sorted(templates_dir.glob("*.tmpl"))

    inlined = [
        p.name for p in tmpl_files if "claude_spawn_forbidden" in p.read_text(encoding="utf-8")
    ]
    assert inlined == [], f"template(s) re-inline the deny-list directly: {inlined}"

    included = [
        p.name
        for p in tmpl_files
        if "{{include:hard-stop-no-claude}}" in p.read_text(encoding="utf-8")
    ]
    assert len(included) == 17, f"expected 17 templates to use the include, got {len(included)}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
