"""Unit tests for scripts/coldstart-interview/generate.py

Covers Spec acceptance items 4-8, 10, 12:
    - generator produces both config.json and CLAUDE.project.md from a fixture
    - determinism (byte-identical output across repeated runs)
    - config.json dial keys are a subset of control_plane.py's dial names
    - engine-boundary guard: output paths never touch an engine-sync
      [include] path or the canonical CLAUDE.md
    - core-only answers -> runnable config
    - missing optional answers -> defaults applied
    - empty/partial answers -> runnable partial, no crash, no half-written file
"""
from __future__ import annotations

import filecmp
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
COLDSTART_DIR = HERE.parent
REPO_ROOT = COLDSTART_DIR.parent.parent

sys.path.insert(0, str(COLDSTART_DIR))
sys.path.insert(0, str(REPO_ROOT))

import generate  # noqa: E402
from backend.role_allowlist import is_role_active  # noqa: E402

FIXTURE_CORE = HERE / "fixtures" / "answers-core.json"
FIXTURE_PARTIAL = HERE / "fixtures" / "answers-partial.json"
FIXTURE_EMPTY = HERE / "fixtures" / "answers-empty.json"
FIXTURE_NEW = HERE / "fixtures" / "answers-new.json"
FIXTURE_MISSION = HERE / "fixtures" / "answers-mission.json"
FIXTURE_MISSION_PLACEHOLDER = HERE / "fixtures" / "answers-mission-placeholder.json"
FIXTURE_HEADLESS_NODEPLOY = HERE / "fixtures" / "answers-headless-nodeploy.json"

ROLES_MAP_PATH = COLDSTART_DIR / "roles-map.json"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# Real control_plane.py dial names -- checked at import time via subprocess
# is intentionally NOT done here (would make tests depend on runtime state);
# instead this list is the same static set generate.py's DIAL_CEILINGS mirrors.
CONTROL_PLANE_DIAL_NAMES = {
    "agent.spawn",
    "archive.move",
    "cost.spend",
    "deps.bump",
    "docs.write",
    "external.system",
    "intent.generate",
    "memory.write",
    "merge.fast-path",
    "merge.standard",
    "methodology.change",
    "sandbox.modify",
    "tests.add",
}

# Engine-sync [include] patterns from scripts/engine-sync/allowlist.txt, as of
# this writing. The guard test below checks generator output never lands on
# one of these paths (or the canonical CLAUDE.md).
ENGINE_SYNC_INCLUDE_GLOBS = [
    "scripts/*.sh",
    "scripts/lib/*.sh",
    "scripts/hooks/**/*",
    "hooks/*.py",
    ".claude/agents/*.md",
]


@pytest.fixture()
def empty_answers_fixture(tmp_path):
    """An answers.json with no topics at all -- the most extreme partial case."""
    p = tmp_path / "answers-empty.json"
    p.write_text(json.dumps({"session": "fixture-empty", "topics": {}}))
    return p


def test_generator_produces_both_output_files(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)
    assert (out_dir / "config.json").is_file()
    assert (out_dir / "CLAUDE.project.md").is_file()


def test_determinism_byte_identical(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    generate.generate(FIXTURE_CORE, out_a)
    generate.generate(FIXTURE_CORE, out_b)

    assert (out_a / "config.json").read_bytes() == (out_b / "config.json").read_bytes()
    assert (out_a / "CLAUDE.project.md").read_bytes() == (out_b / "CLAUDE.project.md").read_bytes()


def test_determinism_via_cli_subprocess(tmp_path):
    """Exercise the exact CLI invocation the Spec's verification block uses."""
    out_a = tmp_path / "cli_a"
    out_b = tmp_path / "cli_b"
    script = COLDSTART_DIR / "generate.py"

    for out_dir in (out_a, out_b):
        result = subprocess.run(
            [sys.executable, str(script), "--answers", str(FIXTURE_CORE), "--out", str(out_dir)],
            capture_output=True,
            text=True,
            cwd=str(COLDSTART_DIR),
        )
        assert result.returncode == 0, result.stderr

    cmp = filecmp.dircmp(str(out_a), str(out_b))
    assert not cmp.diff_files
    assert not cmp.left_only
    assert not cmp.right_only


def test_config_has_valid_dial_defaults(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)
    config = json.loads((out_dir / "config.json").read_text())

    assert "dials" in config
    assert config["dials"], "expected at least one dial entry from the fixture's autonomy answers"
    for dial_name, dial_value in config["dials"].items():
        assert dial_name in CONTROL_PLANE_DIAL_NAMES, f"unknown dial name {dial_name!r}"
        assert "level" in dial_value and "ceiling" in dial_value and "directives" in dial_value
        assert isinstance(dial_value["level"], int)
        assert 1 <= dial_value["level"] <= dial_value["ceiling"]


def test_engine_boundary_guard(tmp_path):
    """The generator must only ever write config.json / CLAUDE.project.md into
    the caller-supplied --out directory (which itself may be under the state
    dir), and must NEVER write to an engine-sync [include] path or touch the
    canonical CLAUDE.md."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)

    written = sorted(p.name for p in out_dir.iterdir())
    assert written == ["CLAUDE.project.md", "config.json"]

    import fnmatch

    for name in written:
        rel_as_if_repo_root = name  # generator never writes relative to repo root
        for pattern in ENGINE_SYNC_INCLUDE_GLOBS:
            assert not fnmatch.fnmatch(rel_as_if_repo_root, pattern), (
                f"generated file {name!r} matches engine-sync include pattern {pattern!r}"
            )
        assert name != "CLAUDE.md", "generator must never write the canonical CLAUDE.md"

    # Canonical CLAUDE.md at the repo root must be untouched by generation.
    canonical = REPO_ROOT / "CLAUDE.md"
    if canonical.is_file():
        before = canonical.read_bytes()
        generate.generate(FIXTURE_CORE, tmp_path / "out2")
        after = canonical.read_bytes()
        assert before == after


def test_engine_boundary_guard_covers_active_roles(tmp_path):
    """Spec item 10 (D#1622 Batch C1): extend the engine-boundary guard to
    prove the new active_roles output never causes a write/delete/git-mv of
    any .claude/agents/* path. Snapshot every agents/*.md file's bytes and
    mtime before and after generation -- none may change."""
    if not AGENTS_DIR.is_dir():
        pytest.skip(".claude/agents directory not present in this checkout")

    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(AGENTS_DIR.glob("*.md"))
    }
    assert before, "expected at least one .claude/agents/*.md file to snapshot"

    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)

    config = json.loads((out_dir / "config.json").read_text())
    assert "active_roles" in config and config["active_roles"], "active_roles must be populated"

    after = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(AGENTS_DIR.glob("*.md"))
    }
    assert before == after, "generation must never write/delete/touch .claude/agents/*.md files"


def test_core_only_answers_yield_runnable_config(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)
    config = json.loads((out_dir / "config.json").read_text())
    overlay = (out_dir / "CLAUDE.project.md").read_text()

    assert config["project"]["name"] == "widgetforge"
    assert "widgetforge" in overlay
    assert config["dials"]["agent.spawn"]["level"] == 4


def test_missing_optional_answers_apply_manifest_defaults(tmp_path):
    """FIXTURE_CORE never answers deploy.hosting_provider or deploy.domain's
    sibling optional questions in other topics (e.g. identity.target_users is
    answered, but a genuinely-missing optional should fall back cleanly)."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_PARTIAL, out_dir)
    config = json.loads((out_dir / "config.json").read_text())

    # stack/deploy/autonomy were never answered in the partial fixture --
    # every value must come from the manifest defaults.
    assert config["project"]["primary_language"] == "python"
    assert config["project"]["deploy_target"] == "self-hosted"
    assert config["dials"]["agent.spawn"]["level"] == 3


def test_partial_abandon_safety_no_crash_no_half_written_file(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_PARTIAL, out_dir)

    config_path = out_dir / "config.json"
    overlay_path = out_dir / "CLAUDE.project.md"
    assert config_path.is_file()
    assert overlay_path.is_file()

    # Both must be fully valid/parseable -- no half-written files.
    config = json.loads(config_path.read_text())
    assert "dials" in config
    overlay_text = overlay_path.read_text()
    assert overlay_text.strip(), "overlay must not be empty"
    assert "onlyidentity" in overlay_text


def test_empty_answers_no_crash(tmp_path, empty_answers_fixture):
    out_dir = tmp_path / "out"
    generate.generate(empty_answers_fixture, out_dir)

    config = json.loads((out_dir / "config.json").read_text())
    assert "dials" in config
    overlay_text = (out_dir / "CLAUDE.project.md").read_text()
    assert overlay_text.strip()


def test_core_question_count_within_shipped_bound():
    """D#1538's shipped test asserts 15 <= core <= 20. Adding project_kind
    as a core question lands exactly at the ceiling (20) -- this must keep
    passing, not be worked around by dropping project_kind."""
    manifest = json.loads((COLDSTART_DIR / "questions.json").read_text())
    core = [q for t in manifest["topics"] for q in t["questions"] if q.get("tier") == "core"]
    assert 15 <= len(core) <= 20, len(core)


def test_project_kind_is_first_topic_single_question():
    manifest = json.loads((COLDSTART_DIR / "questions.json").read_text())
    mode_topics = [t for t in manifest["topics"] if any(q.get("id") == "project_kind" for q in t["questions"])]
    assert len(mode_topics) == 1
    assert manifest["topics"][0] is mode_topics[0]
    project_kind_qs = [q for q in mode_topics[0]["questions"] if q["id"] == "project_kind"]
    assert len(project_kind_qs) == 1


def test_project_mode_new_from_fixture(tmp_path):
    """Spec item 14: a fixture answering project_kind=new produces
    config.json carrying project_mode: new."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_NEW, out_dir)
    config = json.loads((out_dir / "config.json").read_text())
    assert config.get("project_mode") == "new"


def test_project_mode_defaults_to_existing(tmp_path):
    """A fixture that never answers the mode topic (e.g. the pre-existing
    core fixture) falls back to the manifest default, existing."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)
    config = json.loads((out_dir / "config.json").read_text())
    assert config.get("project_mode") == "existing"


def test_new_fixture_determinism_byte_identical(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    generate.generate(FIXTURE_NEW, out_a)
    generate.generate(FIXTURE_NEW, out_b)
    assert (out_a / "config.json").read_bytes() == (out_b / "config.json").read_bytes()
    assert (out_a / "CLAUDE.project.md").read_bytes() == (out_b / "CLAUDE.project.md").read_bytes()


def test_dials_defaults_from_manifest_when_autonomy_topic_missing(tmp_path, empty_answers_fixture):
    out_dir = tmp_path / "out"
    generate.generate(empty_answers_fixture, out_dir)
    config = json.loads((out_dir / "config.json").read_text())

    # All four dial-mapped questions have manifest defaults, so all four
    # dials should still be present even with zero answers supplied.
    assert set(config["dials"].keys()) == {
        "agent.spawn",
        "merge.fast-path",
        "external.system",
        "intent.generate",
    }


def test_mission_topic_exists_with_free_text_fields():
    """Spec items 1-2: a core 'mission' topic exists with three free-text
    (no-choices) fields."""
    manifest = json.loads((COLDSTART_DIR / "questions.json").read_text())
    mission_topics = [t for t in manifest["topics"] if t["id"] == "mission"]
    assert len(mission_topics) == 1
    mission = mission_topics[0]
    ids = {q["id"] for q in mission["questions"]}
    assert {"product_vision", "why_now", "guiding_principles"} <= ids
    assert all("choices" not in q for q in mission["questions"])


def test_mission_defaults_are_honest_placeholders():
    """Spec item 3: mission field defaults are an obvious empty sentinel,
    never a fabricated plausible-sounding mission."""
    manifest = json.loads((COLDSTART_DIR / "questions.json").read_text())
    mission = next(t for t in manifest["topics"] if t["id"] == "mission")
    for q in mission["questions"]:
        default = q.get("default")
        assert default in ("(not captured)", "", None), (
            f"{q['id']!r} default {default!r} looks like fabricated content, not an empty sentinel"
        )


def test_mission_core_topic_id_registered():
    """Spec item 4: 'mission' is a CORE_TOPIC_IDS entry in generate.py."""
    assert "mission" in generate.CORE_TOPIC_IDS


def test_positive_render_includes_mission_and_constitution(tmp_path):
    """Spec item 5: real mission answers produce a '## Mission' section
    containing product_vision + why_now, and a '## Decision Constitution'
    section containing guiding_principles."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_MISSION, out_dir)
    overlay = (out_dir / "CLAUDE.project.md").read_text()

    assert "## Mission" in overlay
    assert "## Decision Constitution" in overlay

    fixture_answers = json.loads(FIXTURE_MISSION.read_text())["topics"]["mission"]
    assert fixture_answers["product_vision"] in overlay
    assert fixture_answers["why_now"] in overlay
    assert fixture_answers["guiding_principles"] in overlay


def test_negative_render_omits_sections_when_mission_absent(tmp_path):
    """Spec item 6: no fabrication -- a fixture with no mission topic at all
    (FIXTURE_CORE) must not emit '## Mission' or '## Decision Constitution'."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)
    overlay = (out_dir / "CLAUDE.project.md").read_text()

    assert "## Mission" not in overlay
    assert "## Decision Constitution" not in overlay


def test_negative_render_omits_sections_when_mission_is_placeholder(tmp_path):
    """Spec item 6: no fabrication -- a mission topic present but still at
    placeholder defaults (or blank) must not emit either section."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_MISSION_PLACEHOLDER, out_dir)
    overlay = (out_dir / "CLAUDE.project.md").read_text()

    assert "## Mission" not in overlay
    assert "## Decision Constitution" not in overlay


def test_mission_fixture_determinism_byte_identical(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    generate.generate(FIXTURE_MISSION, out_a)
    generate.generate(FIXTURE_MISSION, out_b)
    assert (out_a / "config.json").read_bytes() == (out_b / "config.json").read_bytes()
    assert (out_a / "CLAUDE.project.md").read_bytes() == (out_b / "CLAUDE.project.md").read_bytes()


# ---------------------------------------------------------------------------
# D#1622 Batch C1 -- role-roster trimming via config allowlist
# ---------------------------------------------------------------------------

ALWAYS_ON_CORE = {
    "executor",
    "code-reviewer",
    "project-manager",
    "technical-architect",
    "security-reviewer",
    "acceptance-tester",
    "docs-writer",
    "mission-analyst",
}

UI_ONLY_ROLES = {
    "browser-tester",
    "visual-verifier",
    "accessibility-reviewer",
    "ux-designer",
    "tui-tester",
}


def _roles_map():
    return json.loads(ROLES_MAP_PATH.read_text())


def test_roles_map_is_loadable_and_drives_output(tmp_path):
    """Spec item 2: the role-derivation mapping is data (roles-map.json), not
    hardcoded generator if/else branches. Assert it's loadable JSON and that
    generate.py actually consults it (changing the map changes the output)."""
    roles_map = _roles_map()
    assert isinstance(roles_map, dict)
    assert roles_map["always_on"], "roles-map.json must declare always_on roles"

    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir, roles_map_path=ROLES_MAP_PATH)
    config = json.loads((out_dir / "config.json").read_text())
    assert set(config["active_roles"]) >= set(roles_map["always_on"])

    # A trimmed-down map (only always_on, no UI/deploy gating at all) changes
    # the generated output -- proves generate.py is data-driven, not hardcoded.
    trimmed_map_path = tmp_path / "roles-map-trimmed.json"
    trimmed_map_path.write_text(json.dumps({
        "always_on": sorted(roles_map["always_on"]),
        "frontend_gated": [],
        "deploy_gated": [],
        "non_real_deploy_targets": roles_map.get("non_real_deploy_targets", []),
        "known_roles": roles_map["known_roles"],
    }))
    out_dir2 = tmp_path / "out2"
    generate.generate(FIXTURE_CORE, out_dir2, roles_map_path=trimmed_map_path)
    config2 = json.loads((out_dir2 / "config.json").read_text())
    assert set(config2["active_roles"]) == set(roles_map["always_on"])
    assert config2["active_roles"] != config["active_roles"]


def test_active_roles_excludes_ui_roles_when_no_frontend(tmp_path):
    """Spec item 3."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)
    config = json.loads((out_dir / "config.json").read_text())
    active = set(config["active_roles"])

    assert not (active & UI_ONLY_ROLES), f"UI-only roles leaked into headless active_roles: {active & UI_ONLY_ROLES}"
    assert ALWAYS_ON_CORE <= active


def test_active_roles_includes_ui_roles_when_has_frontend(tmp_path):
    """Spec item 4."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_CORE, out_dir)  # FIXTURE_CORE: stack.has_frontend == "yes"
    config = json.loads((out_dir / "config.json").read_text())
    active = set(config["active_roles"])

    assert UI_ONLY_ROLES <= active


def test_active_roles_is_subset_of_known_agent_files(tmp_path):
    """Spec item 5: active_roles is always a subset of the framework's known
    roles -- every entry corresponds to an existing .claude/agents/<role>.md
    file. No invented role names."""
    if not AGENTS_DIR.is_dir():
        pytest.skip(".claude/agents directory not present in this checkout")

    known_agent_files = {p.stem for p in AGENTS_DIR.glob("*.md")}
    roles_map = _roles_map()

    # Every role named in roles-map.json must correspond to a real agent file.
    all_mapped_roles = (
        set(roles_map["always_on"])
        | set(roles_map["frontend_gated"])
        | set(roles_map["deploy_gated"])
    )
    assert all_mapped_roles <= known_agent_files, all_mapped_roles - known_agent_files
    assert set(roles_map["known_roles"]) <= known_agent_files, set(roles_map["known_roles"]) - known_agent_files

    for fixture in (FIXTURE_CORE, FIXTURE_HEADLESS_NODEPLOY, FIXTURE_NEW):
        out_dir = tmp_path / f"out-{fixture.stem}"
        generate.generate(fixture, out_dir)
        config = json.loads((out_dir / "config.json").read_text())
        active = set(config["active_roles"])
        assert active <= known_agent_files, active - known_agent_files
        assert active <= set(roles_map["known_roles"]), active - set(roles_map["known_roles"])


def test_core_roles_always_present_regardless_of_answers(tmp_path):
    """Spec item 6: core roles are always present in active_roles regardless
    of answers -- assert with an answers fixture (headless, no-deploy,
    security_review_required=no) that exercises every gate in the "off"
    direction and still keeps the core set."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)
    config = json.loads((out_dir / "config.json").read_text())
    active = set(config["active_roles"])

    assert ALWAYS_ON_CORE <= active
    # security-reviewer stays always-on even though this fixture answers
    # security_review_required=no (safety default).
    assert "security-reviewer" in active


def test_active_roles_deploy_gated_roles_excluded_when_no_real_deploy_target(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)  # deploy_target: none
    config = json.loads((out_dir / "config.json").read_text())
    active = set(config["active_roles"])

    for role in ("release-manager", "runbook-writer", "incident-commander"):
        assert role not in active, f"{role} should be excluded when deploy_target is 'none'"


def test_active_roles_deploy_gated_roles_included_with_real_deploy_target(tmp_path):
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_NEW, out_dir)  # deploy_target: self-hosted (real)
    config = json.loads((out_dir / "config.json").read_text())
    active = set(config["active_roles"])

    for role in ("release-manager", "runbook-writer", "incident-commander"):
        assert role in active


def test_active_roles_is_sorted_and_deterministic(tmp_path):
    """Spec item 1: sorted array, byte-identical across repeat runs."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    generate.generate(FIXTURE_CORE, out_a)
    generate.generate(FIXTURE_CORE, out_b)

    config_a = json.loads((out_a / "config.json").read_text())
    config_b = json.loads((out_b / "config.json").read_text())

    assert config_a["active_roles"] == sorted(config_a["active_roles"])
    assert config_a["active_roles"] == config_b["active_roles"]
    assert (out_a / "config.json").read_bytes() == (out_b / "config.json").read_bytes()


def test_role_allowlist_is_role_active_absent_key_allows_all(tmp_path):
    """Spec item 7: absent active_roles key -- e.g. this repo's own
    .autonomous-team/config.json -- allows every role."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"project_mode": "existing"}))
    assert is_role_active("executor", config_path) is True
    assert is_role_active("some-made-up-role", config_path) is True


def test_role_allowlist_is_role_active_empty_list_allows_all(tmp_path):
    """Spec item 7: empty active_roles list -- also backward compatible."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"active_roles": []}))
    assert is_role_active("executor", config_path) is True


def test_role_allowlist_is_role_active_respects_populated_list(tmp_path):
    """Spec item 7: a populated active_roles list actually gates."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"active_roles": ["executor", "code-reviewer"]}))
    assert is_role_active("executor", config_path) is True
    assert is_role_active("docs-writer", config_path) is False


def test_role_allowlist_missing_config_fails_open(tmp_path):
    """A missing/unreadable config.json must never brick spawning."""
    assert is_role_active("executor", tmp_path / "does-not-exist.json") is True


def test_generated_config_is_respected_by_role_allowlist_helper(tmp_path):
    """End-to-end: generate.py's own output, read back through
    backend/role_allowlist.py::is_role_active, gates correctly."""
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)
    config_path = out_dir / "config.json"

    assert is_role_active("executor", config_path) is True
    assert is_role_active("browser-tester", config_path) is False


def test_pre_spawn_check_backward_compat_dry_run(tmp_path):
    """Spec item 9: this repo's OWN .autonomous-team/config.json (no
    active_roles key) is unaffected -- pre-spawn-check.sh --role <any-valid-
    role> --dry-run still passes the allowlist gate exactly as today."""
    script = REPO_ROOT / "scripts" / "pre-spawn-check.sh"
    if not script.is_file():
        pytest.skip("pre-spawn-check.sh not present in this checkout")

    own_config = REPO_ROOT / ".autonomous-team" / "config.json"
    if own_config.is_file():
        config = json.loads(own_config.read_text())
        assert not config.get("active_roles"), (
            "this test assumes the repo's own config.json has no active_roles key; "
            "if that has changed, this assertion documents the drift"
        )

    result = subprocess.run(
        ["bash", str(script), "--role", "executor", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("allowed") is True
