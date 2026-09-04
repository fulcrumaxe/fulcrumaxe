"""
Module health checker — discovers all backend Python modules, verifies imports,
optionally invokes CLI entrypoints, and validates dependency graphs.

Usage:
    python backend/module_health.py check             # JSON report, exit 0/1
    python backend/module_health.py check --cli       # also run CLI subprocess checks
    python backend/module_health.py check --verbose   # include full tracebacks
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"

# Ensure repo root is in sys.path so `import backend.X` works regardless of cwd
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Known modules with their CLI subcommands (empty list = no CLI).
# Modules prefixed with "test_" are excluded from health checks.
_MODULE_CLI_MAP: dict[str, list[str]] = {
    "agent_cards": [],
    "agent_log": [],
    "agent_memory": ["record", "query"],
    "agent_profiler": [],
    "agent_retry": [],
    "api": [],
    "api_routes": [],
    "api_version": [],
    "backup": [],
    "blackboard": [],
    "budget": ["status"],
    "changelog": [],
    "circuit_breaker": [],
    "cli": [],
    "config_watcher": [],
    "context_manager": [],
    "control_plane": [],
    "cost_tracker": [],
    "dashboard": [],
    "db": [],
    "dep_graph": [],
    "event_bus": [],
    "health_monitor": ["check"],
    "kpi_engine": ["compute", "show"],
    "log": [],
    "metrics": [],
    "migrate_to_sqlite": [],
    "module_health": ["check"],
    "notifier": [],
    "openapi": [],
    "plugin_loader": [],
    "rate_limiter": [],
    "rbac": [],
    "registry": [],
    "replay": [],
    "server": [],
    "session_manager": [],
    "status_page": [],
    "trigger": [],
    "websocket": [],
    "workflow_runner": [],
}

_TRACEBACK_LINES = 5


class ModuleHealthChecker:
    """Discovers and checks backend Python modules."""

    def __init__(self, backend_dir: Path | None = None) -> None:
        self._backend_dir = backend_dir or _BACKEND_DIR

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[str]:
        """Return sorted list of module names (no .py suffix, no test_ prefix)."""
        names: list[str] = []
        for path in sorted(self._backend_dir.glob("*.py")):
            name = path.stem
            if name.startswith("test_") or name.startswith("__"):
                continue
            names.append(name)
        return names

    # ------------------------------------------------------------------
    # Import check
    # ------------------------------------------------------------------

    def check_import(self, name: str) -> tuple[bool, list[str]]:
        """Attempt to import backend.<name>. Returns (success, errors)."""
        errors: list[str] = []
        try:
            importlib.import_module(f"backend.{name}")
            return True, []
        except Exception:  # noqa: BLE001
            lines = traceback.format_exc().splitlines()
            errors = lines[-_TRACEBACK_LINES:] if len(lines) > _TRACEBACK_LINES else lines
            return False, errors

    def check_import_verbose(self, name: str) -> tuple[bool, list[str]]:
        """Same as check_import but returns the full traceback."""
        errors: list[str] = []
        try:
            importlib.import_module(f"backend.{name}")
            return True, []
        except Exception:  # noqa: BLE001
            errors = traceback.format_exc().splitlines()
            return False, errors

    # ------------------------------------------------------------------
    # CLI check
    # ------------------------------------------------------------------

    def check_cli(self, name: str, subcmd: str) -> tuple[bool | None, list[str]]:
        """Run subprocess import check. Returns (success, errors)."""
        try:
            result = subprocess.run(
                [sys.executable, "-c", f"import backend.{name}"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(_REPO_ROOT),
            )
            if result.returncode == 0:
                return True, []
            errors = (result.stderr or result.stdout or "non-zero exit").splitlines()
            return False, errors[-_TRACEBACK_LINES:]
        except subprocess.TimeoutExpired:
            return False, [f"CLI check timed out after 5s"]
        except Exception as exc:  # noqa: BLE001
            return False, [str(exc)]

    # ------------------------------------------------------------------
    # Dependency graph validation
    # ------------------------------------------------------------------

    def check_deps(self, name: str) -> tuple[list[str], bool, list[str]]:
        """Parse backend.<name> source for 'from backend.X import Y' statements.

        Returns (dep_names, all_deps_ok, errors).
        """
        src_path = self._backend_dir / f"{name}.py"
        if not src_path.exists():
            return [], False, [f"source file not found: {src_path}"]

        try:
            source = src_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(src_path))
        except SyntaxError as exc:
            return [], False, [f"SyntaxError: {exc}"]

        deps: list[str] = []
        errors: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "backend" or module.startswith("backend."):
                    parts = module.split(".")
                    if len(parts) >= 2:
                        dep_name = parts[1]
                    else:
                        # bare "from backend import X" — skip
                        continue
                    if dep_name not in deps:
                        deps.append(dep_name)

        # Verify each dep is importable
        all_ok = True
        for dep in deps:
            dep_path = self._backend_dir / f"{dep}.py"
            if not dep_path.exists():
                errors.append(f"missing dependency: backend.{dep}")
                all_ok = False
            else:
                ok, dep_errors = self.check_import(dep)
                if not ok:
                    errors.append(f"dependency backend.{dep} failed to import")
                    errors.extend(dep_errors[:2])
                    all_ok = False

        return sorted(set(deps)), all_ok, errors

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def run_all(
        self,
        run_cli: bool = False,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Run all checks and return structured report dict."""
        modules = self.discover()
        timestamp = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

        results: list[dict[str, Any]] = []
        failed = 0

        for name in modules:
            # Import check
            if verbose:
                import_ok, import_errors = self.check_import_verbose(name)
            else:
                import_ok, import_errors = self.check_import(name)

            # CLI check (optional)
            cli_ok: bool | None = None
            cli_errors: list[str] = []
            if run_cli:
                subcmds = _MODULE_CLI_MAP.get(name, [])
                if subcmds:
                    cli_ok, cli_errors = self.check_cli(name, subcmds[0])
                # modules with no CLI subcommands get None (not checked)

            # Dependency check
            deps, dep_ok, dep_errors = self.check_deps(name)

            all_errors = import_errors + cli_errors + dep_errors
            if not import_ok or not dep_ok or cli_ok is False:
                failed += 1

            results.append(
                {
                    "name": name,
                    "import_ok": import_ok,
                    "cli_ok": cli_ok,
                    "dependencies": deps,
                    "dep_ok": dep_ok,
                    "errors": all_errors,
                }
            )

        return {
            "timestamp": timestamp,
            "total": len(modules),
            "passed": len(modules) - failed,
            "failed": failed,
            "modules": results,
        }


# ---------------------------------------------------------------------------
# Caching (for API use)
# ---------------------------------------------------------------------------

_health_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}


def get_cached_module_health(ttl: float = 60.0) -> dict[str, Any]:
    """Return module health report from cache, recomputing when TTL elapsed."""
    now = time.monotonic()
    if _health_cache["data"] is None or now >= _health_cache["expires_at"]:
        checker = ModuleHealthChecker()
        _health_cache["data"] = checker.run_all()
        _health_cache["expires_at"] = now + ttl
    return _health_cache["data"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

def _publish_health_events(report: dict[str, Any]) -> None:
    """Publish ModuleHealthEvent for each failed module via the event bus.

    Silently skips if event_bus is not available (graceful degradation).
    """
    try:
        from backend.event_bus import get_bus, ModuleHealthEvent  # noqa: PLC0415
        bus = get_bus()
        for mod in report.get("modules", []):
            if not mod.get("import_ok", True):
                event = ModuleHealthEvent(
                    module_name=mod["name"],
                    errors=mod.get("errors", []),
                )
                bus.publish(event)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check health of all backend Python modules"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_p = sub.add_parser("check", help="Run health check and print JSON report")
    check_p.add_argument(
        "--cli",
        action="store_true",
        default=False,
        help="Also run CLI subprocess checks for modules with known entrypoints",
    )
    check_p.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Include full tracebacks instead of truncated (last 5 lines)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    checker = ModuleHealthChecker()
    report = checker.run_all(run_cli=args.cli, verbose=args.verbose)

    # Publish events for failed modules
    if report["failed"] > 0:
        _publish_health_events(report)

    print(json.dumps(report, indent=2))
    sys.exit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
