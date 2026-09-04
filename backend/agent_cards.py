"""
Agent cards -- machine-readable role definitions for the autonomous team.

Each agent role has a JSON card file under .autonomous-team/agents/ that
describes its capabilities, typed inputs/outputs, and authorized tools.
This module provides a library API and CLI to list, show, and validate
agent cards against workflow definitions.

Usage (CLI):
    python backend/agent_cards.py list
    python backend/agent_cards.py show executor
    python backend/agent_cards.py show nonexistent        # exits 1
    python backend/agent_cards.py validate-workflow implement-discussion

Usage (library):
    from backend.agent_cards import AgentCards
    ac = AgentCards()
    roles = ac.list_agents()            # ['code-reviewer', 'executor', ...]
    card = ac.get_card("executor")      # dict with role, capabilities, etc.
    errors = ac.validate_workflow("implement-discussion")  # [] = valid

    # With plugin support:
    from backend.plugin_loader import PluginLoader
    loader = PluginLoader()
    ac = AgentCards(plugin_loader=loader)
    roles = ac.list_agents()   # includes built-in and plugin roles
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Allow running as a script from repo root: `python backend/agent_cards.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.workflow_runner import WorkflowRunner, WorkflowNotFoundError  # noqa: E402
from backend.plugin_loader import PluginLoader  # noqa: E402


# Default path to agent card files, relative to repo root.
_DEFAULT_AGENTS_DIR = Path(".autonomous-team/agents")


class AgentNotFoundError(FileNotFoundError):
    """Raised when a requested agent card does not exist."""


class AgentCards:
    """
    Library API for agent card files.

    Card files live under .autonomous-team/agents/<role>.json.
    Each file is a JSON object matching the agent-card schema.

    Optionally accepts a *plugin_loader* to merge plugin-defined agents
    alongside built-in ones. Plugin roles are returned from list_agents()
    and get_card() transparently.
    """

    def __init__(
        self,
        agents_dir: Path | str | None = None,
        plugin_loader: Optional[PluginLoader] = None,
    ) -> None:
        if agents_dir is None:
            here = Path(__file__).resolve().parent
            repo_root = here.parent
            self._dir = repo_root / _DEFAULT_AGENTS_DIR
        else:
            self._dir = Path(agents_dir).resolve()
        self._plugin_loader = plugin_loader

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_agents(self) -> list[str]:
        """
        Return sorted list of available agent role names.

        Includes both built-in agents (from .autonomous-team/agents/*.json)
        and plugin-defined agents (if a plugin_loader was provided).
        Returns an empty list if the directory does not exist.
        """
        builtin: list[str] = []
        if self._dir.exists():
            builtin = [p.stem for p in self._dir.glob("*.json")]

        plugin_names: list[str] = []
        if self._plugin_loader is not None:
            plugin_names = self._plugin_loader.list_plugins()

        return sorted(set(builtin) | set(plugin_names))

    def get_card(self, role: str) -> dict:
        """
        Load and return the card dict for *role*.

        Checks built-in cards first, then plugin-defined agents.
        Raises AgentNotFoundError if no card file or plugin exists for the role.
        """
        path = self._dir / f"{role}.json"
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)

        # Fall back to plugin-defined card.
        if self._plugin_loader is not None:
            plugin = self._plugin_loader.get_plugin(role)
            if plugin is not None:
                return {
                    "name": plugin.name,
                    "description": plugin.description,
                    "version": plugin.version,
                    "type": "plugin",
                    "review_pipeline": plugin.review_pipeline,
                    "has_triggers": bool(plugin.triggers),
                    "tools": plugin.tools,
                    "source_file": plugin.source_file,
                }

        raise AgentNotFoundError(
            f"No agent card found for role '{role}' "
            f"(looked in {self._dir})"
        )

    def validate_workflow(self, workflow_name: str) -> list[str]:
        """
        Check that every agent referenced in *workflow_name* has a card.

        Loads the workflow via WorkflowRunner and checks each step's `agent`
        field against the available card files.

        Returns a list of error strings -- empty list means all agents have cards.

        Raises WorkflowNotFoundError if the workflow file does not exist.
        """
        runner = WorkflowRunner()
        raw = runner._load_raw(workflow_name)

        steps = raw.get("steps", []) or []
        available = set(self.list_agents())

        errors: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            agent = step.get("agent")
            if agent is None:
                continue
            if agent not in available:
                step_id = step.get("id", "<unknown>")
                errors.append(
                    f"step '{step_id}': agent '{agent}' has no card file "
                    f"(available: {sorted(available)})"
                )
        return errors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def agents_dir(self) -> Path:
        """Path to the agents directory."""
        return self._dir


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_cards",
        description="Agent card CLI -- list, show, and validate agent role definitions.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="Print all available agent role names, one per line.")

    # show
    s = sub.add_parser("show", help="Print the full card JSON for an agent role.")
    s.add_argument("role", help="Agent role name (e.g. executor)")

    # validate-workflow
    v = sub.add_parser(
        "validate-workflow",
        help="Check that every agent referenced in a workflow has a card.",
    )
    v.add_argument(
        "workflow",
        help="Workflow name to validate (without .yaml extension, e.g. implement-discussion)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    loader = PluginLoader()
    ac = AgentCards(plugin_loader=loader)

    if args.command == "list":
        agents = ac.list_agents()
        if not agents:
            print("(no agent cards found)")
            return 0
        for name in agents:
            print(name)
        return 0

    if args.command == "show":
        try:
            card = ac.get_card(args.role)
        except AgentNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(card, indent=2))
        return 0

    if args.command == "validate-workflow":
        try:
            errors = ac.validate_workflow(args.workflow)
        except WorkflowNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if errors:
            for err in errors:
                print(f"error: {err}", file=sys.stderr)
            return 1
        print(f"valid: all agents in '{args.workflow}' have cards")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
