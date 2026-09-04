"""
dep_graph.py — static dependency graph analyzer for backend Python modules.

Parses all backend/*.py files using ast to extract import relationships,
then exposes multiple output formats: JSON, DOT (Graphviz), ASCII tree,
and impact analysis. Also provides GET /deps API endpoint integration.

Usage:
    python backend/dep_graph.py [--module NAME] [--format json|dot|ascii] [--cycles-only]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent

# Module category assignments for DOT coloring.
_CATEGORIES: dict[str, str] = {
    # Core
    "server": "core",
    "db": "core",
    "event_bus": "core",
    "blackboard": "core",
    "config_watcher": "core",
    # API
    "api": "api",
    "api_routes": "api",
    "api_version": "api",
    "openapi": "api",
    "dashboard": "api",
    # Agents
    "agent_cards": "agents",
    "agent_log": "agents",
    "agent_memory": "agents",
    "agent_profiler": "agents",
    "agent_retry": "agents",
    "spawn_queue": "agents",
    # Monitoring
    "metrics": "monitoring",
    "health_monitor": "monitoring",
    "module_health": "monitoring",
    "kpi_engine": "monitoring",
    "quality_scorer": "monitoring",
    # Security
    "rbac": "security",
    "rate_limiter": "security",
    "circuit_breaker": "security",
    # Infrastructure
    "budget": "infra",
    "cost_tracker": "infra",
    "control_plane": "infra",
    "registry": "infra",
    "backup": "infra",
    "session_manager": "infra",
    # Self
    "dep_graph": "self",
}

_CATEGORY_COLORS: dict[str, str] = {
    "core": "#ff6b6b",
    "api": "#4ecdc4",
    "agents": "#a8e6cf",
    "monitoring": "#3fb950",
    "security": "#f0883e",
    "infra": "#8b949e",
    "self": "#d29922",
    "other": "#c9d1d9",
}


class DepGraph:
    """Static dependency graph for backend Python modules."""

    def __init__(self, backend_dir: Path | None = None) -> None:
        self._dir = backend_dir or _BACKEND_DIR
        # module name -> list of backend module names it imports
        self.adj: dict[str, list[str]] = {}
        # module name -> list of backend module names that import it
        self.rev: dict[str, list[str]] = {}
        self._scan()

    # ------------------------------------------------------------------
    # Build graph
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Walk backend dir, parse each .py, extract backend imports."""
        modules: list[str] = []
        for path in sorted(self._dir.glob("*.py")):
            name = path.stem
            if name.startswith("test_"):
                continue
            modules.append(name)

        # Initialise both maps so every module appears even if it has no edges.
        for name in modules:
            self.adj.setdefault(name, [])
            self.rev.setdefault(name, [])

        for path in sorted(self._dir.glob("*.py")):
            name = path.stem
            if name.startswith("test_"):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, OSError):
                continue

            deps: list[str] = []
            for node in ast.walk(tree):
                dep: str | None = None
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    # from backend.X import ... or from backend import X
                    if mod.startswith("backend."):
                        dep = mod[len("backend."):]
                    elif mod == "backend":
                        for alias in node.names:
                            d = alias.name
                            if d in self.adj and d != name:
                                if d not in deps:
                                    deps.append(d)
                        continue
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        n = alias.name
                        if n.startswith("backend."):
                            dep = n[len("backend."):]
                        elif n == "backend":
                            # bare "import backend" — no specific dep
                            pass
                if dep and dep in self.adj and dep != name and dep not in deps:
                    deps.append(dep)

            self.adj[name] = deps
            for dep in deps:
                if name not in self.rev[dep]:
                    self.rev[dep].append(name)

    # ------------------------------------------------------------------
    # Cycle detection (Tarjan SCC)
    # ------------------------------------------------------------------

    def cycles(self) -> list[list[str]]:
        """Return all strongly connected components with size > 1 (cycles)."""
        index_counter = [0]
        stack: list[str] = []
        lowlink: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        sccs: list[list[str]] = []

        def strongconnect(v: str) -> None:
            index[v] = index_counter[0]
            lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack[v] = True

            for w in self.adj.get(v, []):
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif on_stack.get(w, False):
                    lowlink[v] = min(lowlink[v], index[w])

            if lowlink[v] == index[v]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(sorted(scc))

        # Use iterative DFS to avoid Python recursion limit on large graphs.
        # Re-implement Tarjan iteratively.
        index.clear()
        lowlink.clear()
        on_stack.clear()
        # Reset and use iterative approach
        index_counter[0] = 0
        stack.clear()
        sccs.clear()

        VISIT, POSTVISIT = 0, 1
        call_stack: list[tuple[int, str, int]] = []  # (action, node, iter_pos)
        adj_iters: dict[str, int] = {}

        for start in sorted(self.adj):
            if start in index:
                continue
            call_stack.append((VISIT, start, 0))
            while call_stack:
                action, v, _ = call_stack[-1]
                if action == VISIT:
                    if v not in index:
                        index[v] = index_counter[0]
                        lowlink[v] = index_counter[0]
                        index_counter[0] += 1
                        stack.append(v)
                        on_stack[v] = True
                        adj_iters[v] = 0
                    # Process next neighbour
                    neighbours = self.adj.get(v, [])
                    idx = adj_iters[v]
                    if idx < len(neighbours):
                        adj_iters[v] += 1
                        w = neighbours[idx]
                        if w not in index:
                            call_stack.append((VISIT, w, 0))
                        elif on_stack.get(w, False):
                            lowlink[v] = min(lowlink[v], index[w])
                    else:
                        # Post-visit
                        call_stack.pop()
                        if call_stack:
                            parent = call_stack[-1][1]
                            lowlink[parent] = min(lowlink[parent], lowlink[v])
                        if lowlink[v] == index[v]:
                            scc: list[str] = []
                            while True:
                                w = stack.pop()
                                on_stack[w] = False
                                scc.append(w)
                                if w == v:
                                    break
                            if len(scc) > 1:
                                sccs.append(sorted(scc))
                else:
                    call_stack.pop()

        return sccs

    # ------------------------------------------------------------------
    # Hub analysis
    # ------------------------------------------------------------------

    def hubs(self, threshold: int = 5) -> list[dict[str, Any]]:
        """Return modules where in_degree >= threshold, sorted descending."""
        result = []
        for mod in sorted(self.adj):
            in_deg = len(self.rev.get(mod, []))
            if in_deg >= threshold:
                result.append({
                    "name": mod,
                    "in_degree": in_deg,
                    "out_degree": len(self.adj.get(mod, [])),
                    "dependents": sorted(self.rev.get(mod, [])),
                })
        result.sort(key=lambda x: x["in_degree"], reverse=True)
        return result

    # ------------------------------------------------------------------
    # Impact analysis
    # ------------------------------------------------------------------

    def impact(self, module: str) -> dict[str, Any]:
        """BFS on reverse graph: find everything that depends on module."""
        if module not in self.adj:
            return {
                "module": module,
                "error": f"module '{module}' not found",
                "direct_dependents": [],
                "transitive_dependents": [],
                "depth": 0,
            }

        direct = sorted(self.rev.get(module, []))
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        max_depth = 0

        for dep in direct:
            queue.append((dep, 1))
            visited.add(dep)

        while queue:
            node, depth = queue.popleft()
            max_depth = max(max_depth, depth)
            for upstream in self.rev.get(node, []):
                if upstream not in visited:
                    visited.add(upstream)
                    queue.append((upstream, depth + 1))

        return {
            "module": module,
            "direct_dependents": direct,
            "transitive_dependents": sorted(visited),
            "depth": max_depth,
        }

    # ------------------------------------------------------------------
    # Output formats
    # ------------------------------------------------------------------

    def to_dot(self) -> str:
        """Generate Graphviz DOT representation."""
        lines = ["digraph dep_graph {", "  rankdir=LR;", "  node [shape=box fontname=monospace];", ""]

        # Node declarations with colors
        for mod in sorted(self.adj):
            cat = _CATEGORIES.get(mod, "other")
            color = _CATEGORY_COLORS.get(cat, _CATEGORY_COLORS["other"])
            in_deg = len(self.rev.get(mod, []))
            # Thicken border for hub nodes
            penwidth = "2.5" if in_deg >= 5 else "1.0"
            lines.append(
                f'  "{mod}" [fillcolor="{color}" style=filled fontcolor="#0d1117" penwidth={penwidth}];'
            )

        lines.append("")

        # Edges
        for mod in sorted(self.adj):
            for dep in sorted(self.adj[mod]):
                lines.append(f'  "{mod}" -> "{dep}";')

        lines.append("}")
        return "\n".join(lines)

    def to_ascii(self, root: str | None = None) -> str:
        """Generate indented ASCII tree from root (or all roots if None)."""
        roots: list[str]
        if root:
            roots = [root] if root in self.adj else []
        else:
            # Nodes with no incoming edges are tree roots
            roots = sorted(m for m in self.adj if not self.rev.get(m))
            if not roots:
                roots = sorted(self.adj)[:5]  # fallback: first 5 alphabetically

        lines: list[str] = []

        def render(node: str, prefix: str, child_prefix: str, visited: set[str]) -> None:
            if node in visited:
                lines.append(prefix + node + " [CYCLE]")
                return
            lines.append(prefix + node)
            visited = visited | {node}
            children = sorted(self.adj.get(node, []))
            for i, child in enumerate(children):
                is_last = i == len(children) - 1
                connector = "└── " if is_last else "├── "
                extension = "    " if is_last else "│   "
                render(child, child_prefix + connector, child_prefix + extension, visited)

        for r in roots:
            render(r, "", "", set())
            lines.append("")

        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        """Compute summary statistics."""
        total_modules = len(self.adj)
        total_edges = sum(len(deps) for deps in self.adj.values())

        # Max depth via BFS from all zero-indegree nodes
        roots = [m for m in self.adj if not self.rev.get(m)]
        if not roots:
            roots = list(self.adj)[:1]

        max_depth = 0
        for r in roots:
            visited: set[str] = set()
            q: deque[tuple[str, int]] = deque([(r, 0)])
            while q:
                node, d = q.popleft()
                if node in visited:
                    continue
                visited.add(node)
                max_depth = max(max_depth, d)
                for child in self.adj.get(node, []):
                    if child not in visited:
                        q.append((child, d + 1))

        avg_degree = round(total_edges / total_modules, 2) if total_modules else 0.0

        return {
            "total_modules": total_modules,
            "total_edges": total_edges,
            "max_depth": max_depth,
            "avg_degree": avg_degree,
        }

    def to_json(self) -> dict[str, Any]:
        """Full graph as JSON structure."""
        modules = []
        for mod in sorted(self.adj):
            modules.append({
                "name": mod,
                "deps": sorted(self.adj[mod]),
                "dependents": sorted(self.rev.get(mod, [])),
                "in_degree": len(self.rev.get(mod, [])),
                "out_degree": len(self.adj[mod]),
                "category": _CATEGORIES.get(mod, "other"),
            })

        return {
            "modules": modules,
            "cycles": self.cycles(),
            "hubs": self.hubs(),
            "stats": self.stats(),
        }


# ---------------------------------------------------------------------------
# Cache for API use
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {"graph": None, "expires_at": 0.0}


def get_cached_dep_graph() -> DepGraph:
    """Return a cached DepGraph instance, refreshing every 60 seconds."""
    import time  # noqa: PLC0415
    now = time.time()
    if _cache["graph"] is None or now >= _cache["expires_at"]:
        _cache["graph"] = DepGraph()
        _cache["expires_at"] = now + 60.0
    return _cache["graph"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Analyze backend module dependency graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--module", metavar="NAME", help="Show impact analysis for a single module")
    p.add_argument(
        "--format",
        choices=["json", "dot", "ascii"],
        default="json",
        help="Output format (default: json)",
    )
    p.add_argument("--cycles-only", action="store_true", help="Print only cycle information")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    g = DepGraph()

    if args.cycles_only:
        cycs = g.cycles()
        if not cycs:
            print("No cycles detected.")
        else:
            print(f"{len(cycs)} cycle(s) found:")
            for i, scc in enumerate(cycs, 1):
                print(f"  {i}. {' -> '.join(scc)}")
        return 0

    if args.module:
        data = g.impact(args.module)
        if args.format == "json":
            print(json.dumps(data, indent=2))
        elif args.format == "ascii":
            print(g.to_ascii(args.module))
        else:
            # DOT of subgraph rooted at module
            print(g.to_dot())
        return 0

    if args.format == "dot":
        print(g.to_dot())
    elif args.format == "ascii":
        print(g.to_ascii())
    else:
        print(json.dumps(g.to_json(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
