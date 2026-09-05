"""
Guard against KNOWN_ROLES drift: every .tmpl file in backend/spawn_templates/
must be discoverable as a valid role.

If this test fails, a new .tmpl was added without being reflected in
KNOWN_ROLES — or KNOWN_ROLES was re-hardcoded somewhere.
"""
import pathlib

import pytest

TMPL_DIR = pathlib.Path(__file__).resolve().parents[1] / "backend" / "spawn_templates"


def _tmpl_roles() -> set[str]:
    return {p.stem for p in TMPL_DIR.glob("*.tmpl")}


def test_all_tmpl_files_in_known_roles():
    """Every .tmpl file must appear in KNOWN_ROLES."""
    from backend.spawn_templates import KNOWN_ROLES

    on_disk = _tmpl_roles()
    missing_from_known = on_disk - set(KNOWN_ROLES)
    assert not missing_from_known, (
        f"Roles have .tmpl files but are not in KNOWN_ROLES: {sorted(missing_from_known)}\n"
        "KNOWN_ROLES is now filesystem-derived — this means the derivation is broken."
    )


def test_known_roles_not_larger_than_tmpl_files():
    """KNOWN_ROLES must not contain roles that have no .tmpl file."""
    from backend.spawn_templates import KNOWN_ROLES

    on_disk = _tmpl_roles()
    phantom_roles = set(KNOWN_ROLES) - on_disk
    assert not phantom_roles, (
        f"Roles are in KNOWN_ROLES but have no .tmpl file: {sorted(phantom_roles)}\n"
        "Remove the role from KNOWN_ROLES or add the missing .tmpl file."
    )


@pytest.mark.parametrize("role", sorted(_tmpl_roles()))
def test_render_body_succeeds_for_every_tmpl(role):
    """render_body() must succeed (exit without ValueError) for every .tmpl file."""
    from backend.spawn_templates import render_body

    result = render_body(role, {}, ignore_unknown=True)
    assert result, f"render_body returned empty output for role '{role}'"


@pytest.mark.parametrize("role", sorted(_tmpl_roles()))
def test_render_succeeds_for_every_tmpl(role):
    """render() must succeed (exit without KeyError or ValueError) for every .tmpl file.

    Uses ignore_unknown=True so callers can smoke-test without supplying real
    variable values.  This guards against _ENVELOPE_BY_ROLE / _GATE_CHECKS_BY_ROLE
    dicts being smaller than KNOWN_ROLES (which caused KeyError for 8 roles).
    """
    from backend.spawn_templates import _REPO, render

    result = render(role, {}, ignore_unknown=True)
    assert result, f"render returned empty output for role '{role}'"
    # Must include the mandatory appendices that render() adds.
    assert "AGENT_OUTPUT" in result, (
        f"render() output for '{role}' is missing AGENT_OUTPUT envelope section"
    )
    # Checks against the module's own resolved repo slug rather than a
    # hard-coded literal — a hard-coded copy here silently went stale once
    # already (D#1870: this assertion still expected the pre-rename
    # "autonomous-forever" slug after the resolver was fixed).
    assert _REPO in result, (
        f"render() output for '{role}' is missing repo scope appendix"
    )
