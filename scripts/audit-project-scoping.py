#!/usr/bin/env python3
"""scripts/audit-project-scoping.py — audit project-scoping coverage.

Scans backend/rpc/*.py and backend/stats/*.py for handlers/producers that:
  1. Have hardcoded absolute paths to AF-specific directories, OR
  2. Are RPC handler wrappers in server.py that don't route through
     _with_project_stats_db() or _resolve_repo_for_project(), OR
  3. Read state-dir files without accepting a project param.

Exit code: 0 when zero OPEN offenders; 1 with count otherwise.

Usage:
    python3 scripts/audit-project-scoping.py
    python3 scripts/audit-project-scoping.py --json
    python3 scripts/audit-project-scoping.py --baseline path/to/known.json

Hardcoded FIXED entries represent handlers corrected by PRs #1050, #1063,
#1019, #1035, #1023, #1018, #993 — they appear in the report as [FIXED]
so the audit is self-documenting.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RPC_DIR = _REPO_ROOT / "backend" / "rpc"
_STATS_DIR = _REPO_ROOT / "backend" / "stats"
_SERVER_PY = _REPO_ROOT / "backend" / "server.py"

# ---------------------------------------------------------------------------
# Hardcoded absolute path markers that indicate AF-specific state
# (not project-general paths via state_paths)
# ---------------------------------------------------------------------------

_HARDCODED_MARKERS = (
    "autonomous-forever/.autonomous-team",
    # Assembled rather than written out, and not for style: this file is
    # scanned by scripts/check-no-hardcoded-checkout-paths.sh like every other
    # tracked file, and a scanner that spells its own needle out flags itself
    # on every run. The guard's header explains that it does the same thing to
    # its own examples. Splitting after the "/home/" prefix is enough, and
    # keeps the marker byte-identical to what it was.
    "/home/" + "agent/autonomous-forever",
    # Note: ".autonomous-forever-state" is the default AF state dir.
    # It IS a hardcoded AF path — other projects use different state dirs.
    ".autonomous-forever-state",
)

# ---------------------------------------------------------------------------
# Known-fixed table — handlers corrected by prior PRs.
# Keyed by "<file_stem>/<handler_name>" or "server.py/<handler_name>".
# ---------------------------------------------------------------------------

_FIXED: dict[str, dict] = {
    # PR #1050 — stats.weekly_velocity now routes project via _resolve_repo
    "stats_weekly_velocity/handle": {
        "pr": 1050,
        "note": "now calls _resolve_repo(project) to pick the right GH repo slug",
    },
    # PR #1063 — kpi.history/budget tile now calls for_project()
    "server.py/_rpc_kpi_history": {
        "pr": 1063,
        "note": "now calls for_project() and returns [] for unknown projects",
    },
    # PR #1019 — fleet.projects (activeAgents) reads discover_projects()
    "fleet_projects/handle": {
        "pr": 1019,
        "note": "now reads discover_projects() which scans all ~/.*-state dirs",
    },
    # PR #993 — per-project maxConcurrentAgents via fleet.concurrency
    "fleet_concurrency/handle": {
        "pr": 993,
        "note": "now iterates discover_projects() and calls count_project(name) per project",
    },
    # PR #1023 — fleet reap / discovery_ack stored in fleet-wide state
    "fleet_discovery_ack/handle": {
        "pr": 1023,
        "note": "acks stored in ~/.autonomous-fleet-state/ (fleet-wide, not AF-only)",
    },
    # PR #1018 — loop.timeline now resolves per-project loop-metrics.jsonl
    "server.py/_rpc_loop_timeline": {
        "pr": 1018,
        "note": "now resolves per-project loop-metrics.jsonl via for_project()",
    },
    # PR #1035 — discussions.list/get scoped via _resolve_repo_for_project
    "server.py/_rpc_discussions_list": {
        "pr": 1035,
        "note": "now calls _resolve_repo_for_project(project)",
    },
    "server.py/_rpc_discussions_get": {
        "pr": 1035,
        "note": "now calls _resolve_repo_for_project(project)",
    },
    # PR #1041 — rpcBaseUrl resolution
    "server.py/_rpc_kpi_cycle_time": {
        "pr": 1041,
        "note": "now calls for_project() and returns zeroed buckets for unknown projects",
    },
}

# ---------------------------------------------------------------------------
# Handlers in server.py that are intentionally AF-only or fleet-global
# (loop control, PR/Discussion lookups scoped to _GH_REPO, fleet discovery).
# We don't flag these — they have no per-project equivalent.
# ---------------------------------------------------------------------------

_SERVER_INTENTIONAL = {
    "_rpc_loop_start",
    "_rpc_loop_stop",
    "_rpc_loop_list",
    "_rpc_loop_events",
    "_rpc_agents_tail",
    "_rpc_circuit_breaker_summary",
    "_rpc_circuit_breaker_history",
    "_rpc_team_status_snapshot",
    "_rpc_claude_spawn_tracker_summary",
    "_rpc_pr_detail",
    "_rpc_pr_list",
    "_rpc_cost_per_discussion",
    "_rpc_cost_by_discussion",
    "_rpc_fleet_projects",
    "_rpc_fleet_cost",
    "_rpc_fleet_discovery_ack",
    "_rpc_fleet_concurrency",
    "_rpc_auth_retry_record",
    "_rpc_auth_retry_summary",
    "_rpc_a2a_list_active",
    "_rpc_a2a_tail",
}

# ---------------------------------------------------------------------------
# Stats modules that are pure-config or compute-only (no state-dir reads)
# ---------------------------------------------------------------------------

_STATS_CONFIG_ONLY = {
    "anomaly_config",   # only defines threshold constants
    "metric_order",     # only defines display-order list
    "__init__",
}

SEVERITY_RPC = "RPC"
SEVERITY_STATS = "stats"


def _finding(
    severity: str,
    file: str,
    line: int,
    handler: str,
    accepts_project: bool,
    uses_project: bool,
    hardcoded_path: str | None,
    note: str,
    status: str,
    fix_pr: int | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "handler": handler,
        "accepts_project": accepts_project,
        "uses_project": uses_project,
        "hardcoded_path": hardcoded_path,
        "note": note,
        "status": status,
        "fix_pr": fix_pr,
    }


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None


def _src_has_hardcoded_af_path(src: str) -> str | None:
    """Return the first hardcoded AF path found in source text, or None."""
    for marker in _HARDCODED_MARKERS:
        if marker in src:
            return marker
    return None


def _func_body_src(node: ast.FunctionDef) -> str:
    return ast.unparse(node)


def _func_accepts_project(node: ast.FunctionDef) -> bool:
    for arg in node.args.args + node.args.kwonlyargs:
        if arg.arg == "project":
            return True
    return False


def _body_uses_project(node: ast.FunctionDef) -> bool:
    src = _func_body_src(node)
    return (
        '"project"' in src
        or "'project'" in src
        or "project" in src
    )


# ---------------------------------------------------------------------------
# Check: does server.py wrapper route through _with_project_stats_db or
# _resolve_repo_for_project?  These are the two AF project-scoping helpers.
# ---------------------------------------------------------------------------

def _wrapper_is_project_scoped(node: ast.FunctionDef) -> bool:
    src = _func_body_src(node)
    return (
        "_with_project_stats_db" in src
        or "_resolve_repo_for_project" in src
        or "for_project" in src
        or ('"project"' in src and "params" in src)
        or ("'project'" in src and "params" in src)
    )


# ---------------------------------------------------------------------------
# Scan server.py @_rpc_method handlers
# ---------------------------------------------------------------------------

def _scan_server_py() -> list[dict]:
    findings: list[dict] = []
    tree = _parse(_SERVER_PY)
    if tree is None:
        return findings

    rel = str(_SERVER_PY.relative_to(_REPO_ROOT))

    rpc_funcs: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
                if deco.func.id == "_rpc_method":
                    rpc_funcs.append(node)

    for node in rpc_funcs:
        key = f"server.py/{node.name}"
        src = _func_body_src(node)
        hardcoded = _src_has_hardcoded_af_path(src)
        scoped = _wrapper_is_project_scoped(node)

        if key in _FIXED:
            findings.append(_finding(
                severity=SEVERITY_RPC,
                file=rel,
                line=node.lineno,
                handler=node.name,
                accepts_project=True,  # was fixed
                uses_project=True,
                hardcoded_path=None,
                note=_FIXED[key]["note"],
                status="FIXED",
                fix_pr=_FIXED[key]["pr"],
            ))
            continue

        if node.name in _SERVER_INTENTIONAL:
            continue  # AF-only by design

        if hardcoded:
            findings.append(_finding(
                severity=SEVERITY_RPC,
                file=rel,
                line=node.lineno,
                handler=node.name,
                accepts_project=False,
                uses_project=False,
                hardcoded_path=hardcoded,
                note=f"reads hardcoded AF path: {hardcoded!r}",
                status="OPEN",
                fix_pr=None,
            ))
        elif not scoped:
            findings.append(_finding(
                severity=SEVERITY_RPC,
                file=rel,
                line=node.lineno,
                handler=node.name,
                accepts_project=False,
                uses_project=False,
                hardcoded_path=None,
                note="no project routing (_with_project_stats_db / _resolve_repo_for_project / for_project)",
                status="OPEN",
                fix_pr=None,
            ))

    return findings


# ---------------------------------------------------------------------------
# Scan backend/rpc/*.py — check handle() functions for hardcoded paths.
# NOTE: most stats_* handlers are project-scoped at the server.py call-site
# (via _with_project_stats_db), so we only flag hardcoded absolute paths here.
# ---------------------------------------------------------------------------

def _scan_rpc_modules() -> list[dict]:
    findings: list[dict] = []

    for py_file in sorted(_RPC_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        stem = py_file.stem
        file_src = py_file.read_text(encoding="utf-8")
        tree = _parse(py_file)
        if tree is None:
            continue

        rel = str(py_file.relative_to(_REPO_ROOT))

        # Check module-level string literals for hardcoded AF paths
        module_hardcoded = _src_has_hardcoded_af_path(file_src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not (node.name == "handle" or node.name.startswith("handle_")):
                continue

            key = f"{stem}/{node.name}"
            src = _func_body_src(node)
            func_hardcoded = _src_has_hardcoded_af_path(src) or (
                module_hardcoded if any(
                    marker in src for marker in ("_DEFAULT_", "_db_path", ".autonomous")
                ) else None
            )

            if key in _FIXED:
                findings.append(_finding(
                    severity=SEVERITY_RPC,
                    file=rel,
                    line=node.lineno,
                    handler=node.name,
                    accepts_project=True,
                    uses_project=True,
                    hardcoded_path=None,
                    note=_FIXED[key]["note"],
                    status="FIXED",
                    fix_pr=_FIXED[key]["pr"],
                ))
            elif func_hardcoded:
                findings.append(_finding(
                    severity=SEVERITY_RPC,
                    file=rel,
                    line=node.lineno,
                    handler=node.name,
                    accepts_project=False,
                    uses_project=False,
                    hardcoded_path=func_hardcoded,
                    note=f"reads hardcoded AF path: {func_hardcoded!r}",
                    status="OPEN",
                    fix_pr=None,
                ))

    return findings


# ---------------------------------------------------------------------------
# Scan backend/stats/*.py — hardcoded absolute AF paths in default values
# ---------------------------------------------------------------------------

def _scan_stats_modules() -> list[dict]:
    findings: list[dict] = []

    for py_file in sorted(_STATS_DIR.glob("*.py")):
        stem = py_file.stem
        if stem in _STATS_CONFIG_ONLY:
            continue

        file_src = py_file.read_text(encoding="utf-8")
        rel = str(py_file.relative_to(_REPO_ROOT))
        key = f"{stem}/module"

        hardcoded = _src_has_hardcoded_af_path(file_src)

        if key in _FIXED:
            findings.append(_finding(
                severity=SEVERITY_STATS,
                file=rel,
                line=1,
                handler="(module-level)",
                accepts_project=True,
                uses_project=True,
                hardcoded_path=None,
                note=_FIXED[key]["note"],
                status="FIXED",
                fix_pr=_FIXED[key]["pr"],
            ))
        elif hardcoded:
            # Check if the path is in an env-overridable default (partial credit)
            env_overridable = (
                "os.environ.get" in file_src
                or "os.getenv" in file_src
            )
            if env_overridable:
                note = (
                    f"default path is hardcoded to AF ({hardcoded!r}) but overridable "
                    f"via env var — add explicit project param for multi-project use"
                )
            else:
                note = f"hardcoded AF path with no env override: {hardcoded!r}"

            findings.append(_finding(
                severity=SEVERITY_STATS,
                file=rel,
                line=1,
                handler="(module-level)",
                accepts_project=False,
                uses_project=False,
                hardcoded_path=hardcoded,
                note=note,
                status="OPEN",
                fix_pr=None,
            ))

    return findings


# ---------------------------------------------------------------------------
# Run full audit
# ---------------------------------------------------------------------------

def _run_audit() -> list[dict]:
    findings: list[dict] = []
    findings.extend(_scan_server_py())
    findings.extend(_scan_rpc_modules())
    findings.extend(_scan_stats_modules())

    # Sort: OPEN first, then by severity (RPC > stats), then file+handler
    findings.sort(key=lambda f: (
        0 if f["status"] == "OPEN" else 1,
        0 if f["severity"] == SEVERITY_RPC else 1,
        f["file"],
        f["handler"],
    ))
    return findings


def _apply_baseline(findings: list[dict], baseline_path: str) -> list[dict]:
    try:
        baseline_raw = json.loads(Path(baseline_path).read_text())
    except Exception as exc:
        print(f"WARNING: could not load baseline {baseline_path!r}: {exc}", file=sys.stderr)
        return findings

    baseline_keys: set[tuple] = set()
    for entry in baseline_raw if isinstance(baseline_raw, list) else []:
        baseline_keys.add((entry.get("file", ""), entry.get("handler", "")))

    return [f for f in findings if (f["file"], f["handler"]) not in baseline_keys]


# ---------------------------------------------------------------------------
# Render markdown
# ---------------------------------------------------------------------------

def _render_markdown(findings: list[dict]) -> str:
    open_findings = [f for f in findings if f["status"] == "OPEN"]
    fixed_findings = [f for f in findings if f["status"] == "FIXED"]

    lines: list[str] = [
        "# Project-Scoping Audit — 2026-05-18",
        "",
        "Auto-generated by `scripts/audit-project-scoping.py`.",
        "",
        f"**Summary**: {len(open_findings)} OPEN, {len(fixed_findings)} FIXED (already patched)",
        "",
        "Detection method: AST-based scan. Flags handlers with hardcoded AF-specific paths",
        "or RPC wrappers that don't route through `_with_project_stats_db` / `_resolve_repo_for_project`.",
        "False positives are possible for intentionally AF-only handlers (marked advisory).",
        "",
    ]

    # --- OPEN RPC ---
    open_rpc = [f for f in open_findings if f["severity"] == SEVERITY_RPC]
    if open_rpc:
        lines += [
            "## OPEN — RPC Handlers (server.py + rpc/*.py)",
            "",
            "| Handler | File:Line | Hardcoded Path | Note |",
            "|---------|-----------|----------------|------|",
        ]
        for f in open_rpc:
            loc = f"{f['file']}:{f['line']}"
            hp = f["hardcoded_path"] or ""
            lines.append(f"| `{f['handler']}` | `{loc}` | {hp} | {f['note']} |")
        lines.append("")

    # --- OPEN stats ---
    open_stats = [f for f in open_findings if f["severity"] == SEVERITY_STATS]
    if open_stats:
        lines += [
            "## OPEN — Stats Producers (backend/stats/*.py)",
            "",
            "| Module | File:Line | Hardcoded Path | Note |",
            "|--------|-----------|----------------|------|",
        ]
        for f in open_stats:
            loc = f"{f['file']}:{f['line']}"
            hp = f["hardcoded_path"] or ""
            lines.append(f"| `{f['handler']}` | `{loc}` | {hp} | {f['note']} |")
        lines.append("")

    if not open_findings:
        lines += ["## OPEN Offenders", "", "_None — all scanned handlers are project-scoped._", ""]

    # --- FIXED ---
    if fixed_findings:
        lines += [
            "## FIXED (already patched by prior PRs)",
            "",
            "| Handler | File:Line | Fix PR | Note |",
            "|---------|-----------|--------|------|",
        ]
        for f in fixed_findings:
            loc = f"{f['file']}:{f['line']}"
            pr_str = f"#{f['fix_pr']}" if f["fix_pr"] else ""
            lines.append(f"| `{f['handler']}` | `{loc}` | {pr_str} | {f['note']} |")
        lines.append("")

    # --- Recommended fixes ---
    if open_findings:
        lines += [
            "## Recommended Fixes",
            "",
            "### Hardcoded path offenders",
            "",
            "Replace hardcoded absolute-checkout `.autonomous-team/` references",
            "with `state_paths.for_project(project).state_dir / <relative-path>` (or equivalent).",
            "For stats modules using env-var overrides, add an explicit `project` param",
            "and resolve via `for_project()` so multi-project dashboards work without",
            "needing to restart the server with a different `AUTONOMOUS_TEAM_STATE_DIR`.",
            "",
            "### Server.py wrappers without project routing",
            "",
            "Wrappers that read state-dir data should either:",
            "  a. Route through `_with_project_stats_db(project, ...)` (for DuckDB stats), or",
            "  b. Call `_resolve_repo_for_project(project)` (for GitHub API calls), or",
            "  c. Call `state_paths.for_project(project)` directly (for file-path resolution).",
            "",
            "Handlers that are intentionally AF-only (loop control, PR/Discussion lookups)",
            "should be added to `_SERVER_INTENTIONAL` in this script to suppress the warning.",
            "",
        ]

    lines += [
        "---",
        "",
        f"_Generated 2026-05-18 by `scripts/audit-project-scoping.py`. {len(open_findings)} OPEN offenders._",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import time

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument("--baseline", metavar="PATH", help="filter out known-accepted gaps")
    args = parser.parse_args()

    t0 = time.monotonic()
    findings = _run_audit()
    elapsed = time.monotonic() - t0

    if args.baseline:
        findings = _apply_baseline(findings, args.baseline)

    open_count = sum(1 for f in findings if f["status"] == "OPEN")

    if args.json:
        print(json.dumps(
            {"findings": findings, "open_count": open_count, "elapsed_s": round(elapsed, 3)},
            indent=2,
        ))
    else:
        print(_render_markdown(findings))
        print(f"Elapsed: {elapsed:.2f}s  |  OPEN: {open_count}", file=sys.stderr)

    sys.exit(0 if open_count == 0 else 1)


if __name__ == "__main__":
    main()
