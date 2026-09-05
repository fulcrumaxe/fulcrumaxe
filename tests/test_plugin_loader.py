"""
Tests for backend/plugin_loader.py -- PluginLoader class and PluginDef dataclass.

Also covers integration with AgentCards so plugin agents appear alongside built-ins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.plugin_loader import BUILTIN_ROLES, PluginDef, PluginLoader
from backend.agent_cards import AgentCards, AgentNotFoundError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plugin(d: Path, name: str, extra: dict | None = None) -> Path:
    """Write a minimal valid plugin YAML to *d/<name>.yaml*."""
    data: dict = {
        "name": name,
        "description": f"Test plugin {name}",
        "system_prompt": f"You are the {name} agent.",
    }
    if extra:
        data.update(extra)
    p = d / f"{name}.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


def _write_agent_card(d: Path, role: str) -> Path:
    """Write a minimal built-in agent card JSON to *d/<role>.json*."""
    content = {
        "role": role,
        "description": f"Built-in {role}",
        "capabilities": [],
        "authorized_tools": [],
    }
    p = d / f"{role}.json"
    p.write_text(json.dumps(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Discovery -- basic scan
# ---------------------------------------------------------------------------


def test_discovery_finds_yaml_files(tmp_path):
    """PluginLoader discovers all *.yaml files in the plugins directory."""
    _write_plugin(tmp_path, "plugin-a")
    _write_plugin(tmp_path, "plugin-b")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == ["plugin-a", "plugin-b"]


def test_discovery_skips_example_files(tmp_path):
    """Files named *.yaml.example are NOT loaded."""
    _write_plugin(tmp_path, "docs-writer")
    example = tmp_path / "example-thing.yaml.example"
    example.write_text("name: should-not-load\ndescription: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert "should-not-load" not in loader.list_plugins()
    assert "docs-writer" in loader.list_plugins()


def test_discovery_empty_directory(tmp_path):
    """An empty plugins directory results in an empty plugin list."""
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_discovery_missing_directory(tmp_path):
    """A non-existent plugins directory is handled gracefully (no crash)."""
    missing = tmp_path / "nonexistent"
    loader = PluginLoader(plugins_dir=missing)
    assert loader.list_plugins() == []


# ---------------------------------------------------------------------------
# 2. Validation -- missing required fields
# ---------------------------------------------------------------------------


def test_validation_missing_name(tmp_path):
    """Plugin missing 'name' field is skipped."""
    p = tmp_path / "bad.yaml"
    p.write_text("description: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_validation_missing_description(tmp_path):
    """Plugin missing 'description' field is skipped."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: bad-plugin\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_validation_missing_system_prompt(tmp_path):
    """Plugin missing 'system_prompt' field is skipped."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: bad-plugin\ndescription: x\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


# ---------------------------------------------------------------------------
# 3. Validation -- name format
# ---------------------------------------------------------------------------


def test_validation_invalid_name_uppercase(tmp_path):
    """Plugin name with uppercase letters is rejected."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: MyPlugin\ndescription: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_validation_invalid_name_starts_with_digit(tmp_path):
    """Plugin name starting with a digit is rejected."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: 1bad\ndescription: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_validation_invalid_name_underscores(tmp_path):
    """Plugin name with underscores is rejected (hyphens only)."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: bad_plugin\ndescription: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


def test_validation_valid_name_with_hyphens(tmp_path):
    """Plugin name with hyphens and digits is accepted."""
    _write_plugin(tmp_path, "my-plugin-42")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert "my-plugin-42" in loader.list_plugins()


# ---------------------------------------------------------------------------
# 4. Validation -- name collision with built-ins
# ---------------------------------------------------------------------------


def test_validation_builtin_name_collision(tmp_path):
    """Plugin attempting to use a built-in role name is rejected."""
    for builtin in ("executor", "code-reviewer", "security-reviewer"):
        p = tmp_path / f"{builtin}.yaml"
        p.write_text(f"name: {builtin}\ndescription: x\nsystem_prompt: y\n")
    loader = PluginLoader(plugins_dir=tmp_path)
    for builtin in ("executor", "code-reviewer", "security-reviewer"):
        assert builtin not in loader.list_plugins()


def test_builtin_roles_constant_contains_expected_roles():
    """BUILTIN_ROLES frozenset contains all known built-in agent roles."""
    for role in (
        "executor",
        "code-reviewer",
        "security-reviewer",
        "project-manager",
    ):
        assert role in BUILTIN_ROLES


# ---------------------------------------------------------------------------
# 5. get_plugin / list_plugins operations
# ---------------------------------------------------------------------------


def test_get_plugin_returns_plugindef(tmp_path):
    """get_plugin returns a PluginDef with correct fields."""
    _write_plugin(tmp_path, "my-agent", {
        "version": "2.0",
        "tools": ["read", "write"],
        "review_pipeline": "code+security",
    })
    loader = PluginLoader(plugins_dir=tmp_path)
    p = loader.get_plugin("my-agent")
    assert isinstance(p, PluginDef)
    assert p.name == "my-agent"
    assert p.description == "Test plugin my-agent"
    assert p.version == "2.0"
    assert p.tools == ["read", "write"]
    assert p.review_pipeline == "code+security"
    assert "my-agent" in p.source_file


def test_get_plugin_returns_none_for_unknown(tmp_path):
    """get_plugin returns None for a name that was not loaded."""
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.get_plugin("does-not-exist") is None


def test_list_plugins_returns_sorted(tmp_path):
    """list_plugins returns plugin names in sorted order."""
    for name in ("zebra-agent", "alpha-agent", "middle-agent"):
        _write_plugin(tmp_path, name)
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == ["alpha-agent", "middle-agent", "zebra-agent"]


def test_plugin_defaults(tmp_path):
    """Optional fields have correct defaults when omitted."""
    _write_plugin(tmp_path, "minimal")
    loader = PluginLoader(plugins_dir=tmp_path)
    p = loader.get_plugin("minimal")
    assert p is not None
    assert p.version == "1.0"
    assert p.tools is None  # None means all tools
    assert p.review_pipeline == "code-only"
    assert p.triggers == []


# ---------------------------------------------------------------------------
# 6. Integration with AgentCards
# ---------------------------------------------------------------------------


def test_agent_cards_list_includes_plugins(tmp_path):
    """AgentCards.list_agents() returns built-in + plugin names."""
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    _write_agent_card(agents_d, "executor")

    plugins_d = tmp_path / "plugins"
    plugins_d.mkdir()
    _write_plugin(plugins_d, "docs-writer")

    loader = PluginLoader(plugins_dir=plugins_d)
    ac = AgentCards(agents_dir=agents_d, plugin_loader=loader)
    agents = ac.list_agents()
    assert "executor" in agents
    assert "docs-writer" in agents


def test_agent_cards_get_card_returns_plugin_card(tmp_path):
    """AgentCards.get_card() returns a plugin card for plugin roles."""
    agents_d = tmp_path / "agents"
    agents_d.mkdir()

    plugins_d = tmp_path / "plugins"
    plugins_d.mkdir()
    _write_plugin(plugins_d, "perf-profiler", {"review_pipeline": "code+security"})

    loader = PluginLoader(plugins_dir=plugins_d)
    ac = AgentCards(agents_dir=agents_d, plugin_loader=loader)
    card = ac.get_card("perf-profiler")
    assert card["type"] == "plugin"
    assert card["name"] == "perf-profiler"
    assert card["review_pipeline"] == "code+security"


def test_agent_cards_get_card_raises_for_unknown_plugin(tmp_path):
    """AgentCards.get_card() raises AgentNotFoundError when role is unknown."""
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    plugins_d = tmp_path / "plugins"
    plugins_d.mkdir()
    loader = PluginLoader(plugins_dir=plugins_d)
    ac = AgentCards(agents_dir=agents_d, plugin_loader=loader)
    with pytest.raises(AgentNotFoundError):
        ac.get_card("nonexistent-role")


def test_agent_cards_builtin_takes_precedence_over_plugin(tmp_path):
    """Built-in card file takes precedence over a plugin with the same name."""
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    # Use a name not in BUILTIN_ROLES to bypass the collision check in PluginLoader.
    _write_agent_card(agents_d, "custom-role")

    plugins_d = tmp_path / "plugins"
    plugins_d.mkdir()
    _write_plugin(plugins_d, "custom-role")

    loader = PluginLoader(plugins_dir=plugins_d)
    ac = AgentCards(agents_dir=agents_d, plugin_loader=loader)
    card = ac.get_card("custom-role")
    # Should come from the JSON card (has "role" key), not plugin (has "type": "plugin")
    assert "role" in card
    assert card.get("type") != "plugin"


def test_agent_cards_without_plugin_loader_unchanged(tmp_path):
    """AgentCards without a plugin_loader behaves exactly as before."""
    agents_d = tmp_path / "agents"
    agents_d.mkdir()
    _write_agent_card(agents_d, "executor")
    ac = AgentCards(agents_dir=agents_d)
    assert ac.list_agents() == ["executor"]


# ---------------------------------------------------------------------------
# 7. Invalid YAML file -- no crash
# ---------------------------------------------------------------------------


def test_invalid_yaml_does_not_crash_loader(tmp_path):
    """A YAML parse error in one file does not prevent others from loading."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [\nbroken yaml{{{", encoding="utf-8")
    _write_plugin(tmp_path, "good-plugin")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert "good-plugin" in loader.list_plugins()
    assert loader.list_plugins() == ["good-plugin"]


# ---------------------------------------------------------------------------
# 8. Invalid review_pipeline value
# ---------------------------------------------------------------------------


def test_invalid_review_pipeline_rejected(tmp_path):
    """Plugin with an invalid review_pipeline value is skipped."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: bad-pipeline\ndescription: x\nsystem_prompt: y\nreview_pipeline: invalid-value\n"
    )
    loader = PluginLoader(plugins_dir=tmp_path)
    assert loader.list_plugins() == []


# ---------------------------------------------------------------------------
# 9. Triggers stored correctly
# ---------------------------------------------------------------------------


def test_triggers_stored_correctly(tmp_path):
    """Triggers from YAML are stored as a list of dicts on PluginDef."""
    data = {
        "name": "trigger-agent",
        "description": "test",
        "system_prompt": "prompt",
        "triggers": [
            {"on": "discussion_label", "value": "docs"},
            {"on": "pr_label", "value": "needs-docs"},
        ],
    }
    (tmp_path / "trigger-agent.yaml").write_text(yaml.dump(data))
    loader = PluginLoader(plugins_dir=tmp_path)
    p = loader.get_plugin("trigger-agent")
    assert p is not None
    assert len(p.triggers) == 2
    assert p.triggers[0] == {"on": "discussion_label", "value": "docs"}


# ---------------------------------------------------------------------------
# 10. Example plugin file exists and would pass validation when renamed
# ---------------------------------------------------------------------------


def test_example_plugin_file_exists():
    """The example plugin file exists in the repo."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / ".autonomous-team" / "plugins" / "example-docs-writer.yaml.example"
    assert example.exists(), f"Example plugin file not found at {example}"


def test_example_plugin_passes_validation_when_renamed(tmp_path):
    """The example plugin file is valid when loaded as a .yaml file."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / ".autonomous-team" / "plugins" / "example-docs-writer.yaml.example"
    # Copy it as a .yaml file in tmp_path
    dest = tmp_path / "example-docs-writer.yaml"
    dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    loader = PluginLoader(plugins_dir=tmp_path)
    assert "docs-writer" in loader.list_plugins()
    p = loader.get_plugin("docs-writer")
    assert p is not None
    assert p.system_prompt  # not empty
