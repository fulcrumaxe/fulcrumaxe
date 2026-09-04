"""
Plugin loader — discovers and validates custom agent role definitions.

Plugin YAML files live in .autonomous-team/plugins/*.yaml. Each file defines
a custom agent role with a name, system prompt, tool access, and review pipeline
configuration. The loader discovers, validates, and registers plugins at startup.

Schema (all fields except name, description, system_prompt are optional):

    name: docs-writer           # unique identifier, used as agent role name
    description: "Generates and updates documentation"
    version: "1.0"
    system_prompt: |
      You are a documentation specialist...
    tools:                       # optional: restrict tool access (None = all tools)
      - read
      - write
      - glob
      - grep
    review_pipeline: code-only   # code-only | code+security | none
    triggers:                    # optional: auto-spawn conditions
      - on: discussion_label
        value: docs

Usage (library):
    from backend.plugin_loader import PluginLoader
    loader = PluginLoader()
    plugins = loader.list_plugins()        # ['docs-writer', ...]
    defn = loader.get_plugin("docs-writer")  # PluginDef or None
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PLUGINS_DIR = Path(".autonomous-team/plugins")

# Built-in role names -- plugins must not use these.
BUILTIN_ROLES: frozenset[str] = frozenset(
    {
        "executor",
        "code-reviewer",
        "security-reviewer",
        "project-manager",
        "acceptance-tester",
        "team-lead",
    }
)

# Valid plugin name pattern: lowercase letters, digits, hyphens; starts with a letter.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

# Valid review_pipeline values.
_VALID_PIPELINES: frozenset[str] = frozenset({"code-only", "code+security", "none"})


# ---------------------------------------------------------------------------
# PluginDef dataclass
# ---------------------------------------------------------------------------


@dataclass
class PluginDef:
    """Validated plugin definition loaded from a YAML file."""

    name: str
    description: str
    version: str
    system_prompt: str
    tools: Optional[list[str]]  # None = all tools allowed
    review_pipeline: str  # code-only | code+security | none
    triggers: list[dict]
    source_file: str  # absolute path to the YAML file


# ---------------------------------------------------------------------------
# PluginLoader
# ---------------------------------------------------------------------------


class PluginLoader:
    """
    Discovers and validates custom agent role YAML definitions.

    On init, scans *plugins_dir* for ``*.yaml`` files (skipping ``*.yaml.example``),
    validates each against the schema, and stores the valid set.

    Invalid files are skipped with a logged warning; they do not abort loading.
    """

    def __init__(self, plugins_dir: Path | str | None = None) -> None:
        if plugins_dir is None:
            here = Path(__file__).resolve().parent
            repo_root = here.parent
            self._dir = repo_root / _DEFAULT_PLUGINS_DIR
        else:
            self._dir = Path(plugins_dir).resolve()

        self._plugins: dict[str, PluginDef] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_plugins(self) -> list[str]:
        """Return sorted list of loaded plugin names."""
        return sorted(self._plugins.keys())

    def get_plugin(self, name: str) -> PluginDef | None:
        """Return the PluginDef for *name*, or None if not found."""
        return self._plugins.get(name)

    # ------------------------------------------------------------------
    # Internal loading logic
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Scan the plugins directory and load all valid YAML plugins."""
        if not self._dir.exists():
            logger.debug("Plugins directory does not exist: %s", self._dir)
            return

        for path in sorted(self._dir.glob("*.yaml")):
            # Skip .yaml.example files -- glob *.yaml won't match them anyway,
            # but guard explicitly for safety.
            if path.name.endswith(".yaml.example"):
                continue
            self._load_file(path)

    def _load_file(self, path: Path) -> None:
        """Attempt to load and validate a single plugin YAML file."""
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            logger.warning("Plugin file %s: YAML parse error -- %s", path, exc)
            return
        except OSError as exc:
            logger.warning("Plugin file %s: cannot read -- %s", path, exc)
            return

        if not isinstance(raw, dict):
            logger.warning("Plugin file %s: top-level must be a YAML mapping", path)
            return

        result = self._validate(raw, source_file=str(path))
        if result is None:
            return  # validation already logged a warning

        # Check for duplicate names (first writer wins -- warn and skip).
        if result.name in self._plugins:
            logger.warning(
                "Plugin file %s: duplicate plugin name '%s' (already loaded from %s) -- skipping",
                path,
                result.name,
                self._plugins[result.name].source_file,
            )
            return

        self._plugins[result.name] = result
        logger.debug("Loaded plugin '%s' from %s", result.name, path)

    def _validate(self, raw: dict, *, source_file: str) -> PluginDef | None:
        """
        Validate a parsed YAML dict against the plugin schema.

        Returns a PluginDef on success, or None (after logging) on failure.
        """
        # Required fields
        for required in ("name", "description", "system_prompt"):
            if not raw.get(required):
                logger.warning(
                    "Plugin file %s: missing required field '%s' -- skipping",
                    source_file,
                    required,
                )
                return None

        name = str(raw["name"]).strip()
        description = str(raw["description"]).strip()
        system_prompt = str(raw["system_prompt"]).strip()

        # Name format validation
        if not _NAME_PATTERN.match(name):
            logger.warning(
                "Plugin file %s: invalid plugin name '%s' -- must match [a-z][a-z0-9-]* -- skipping",
                source_file,
                name,
            )
            return None

        # Name collision with built-ins
        if name in BUILTIN_ROLES:
            logger.warning(
                "Plugin file %s: plugin name '%s' collides with built-in role -- skipping",
                source_file,
                name,
            )
            return None

        # Optional fields with defaults
        version = str(raw.get("version", "1.0")).strip()

        tools_raw = raw.get("tools")
        if tools_raw is not None:
            if not isinstance(tools_raw, list):
                logger.warning(
                    "Plugin file %s: 'tools' must be a list -- skipping",
                    source_file,
                )
                return None
            tools: list[str] | None = [str(t) for t in tools_raw]
        else:
            tools = None  # all tools allowed

        pipeline = str(raw.get("review_pipeline", "code-only")).strip()
        if pipeline not in _VALID_PIPELINES:
            logger.warning(
                "Plugin file %s: invalid review_pipeline '%s' -- must be one of %s -- skipping",
                source_file,
                pipeline,
                sorted(_VALID_PIPELINES),
            )
            return None

        triggers_raw = raw.get("triggers")
        if triggers_raw is None:
            triggers: list[dict] = []
        elif not isinstance(triggers_raw, list):
            logger.warning(
                "Plugin file %s: 'triggers' must be a list -- skipping",
                source_file,
            )
            return None
        else:
            triggers = [t if isinstance(t, dict) else {"raw": t} for t in triggers_raw]

        return PluginDef(
            name=name,
            description=description,
            version=version,
            system_prompt=system_prompt,
            tools=tools,
            review_pipeline=pipeline,
            triggers=triggers,
            source_file=source_file,
        )
