"""Tests for backend/dep_graph.py — fixture-based, deterministic.

All tests point DepGraph at a temporary directory of small .py files with
known import relationships.  We never point at the real backend/ tree, so
assertions stay stable regardless of how other modules evolve.

Import patterns that dep_graph.py understands:
    from backend.X import ...   → X is a dependency
    from backend import X       → X is a dependency (if X is a known module)
    import backend.X            → X is a dependency

Files whose name starts with test_ are skipped by the scanner.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make `backend` importable from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.dep_graph import DepGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, filename: str, content: str) -> None:
    """Write a .py fixture file into tmp_path."""
    (tmp_path / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Edge extraction — from backend.X import
# ---------------------------------------------------------------------------

class TestEdgeExtractionFromBackend:
    """DepGraph picks up 'from backend.X import ...' as an edge."""

    def test_single_edge(self, tmp_path: Path) -> None:
        _write(tmp_path, "alpha.py", "")
        _write(tmp_path, "beta.py", "from backend.alpha import something\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "alpha" in g.adj["beta"]

    def test_no_self_loop(self, tmp_path: Path) -> None:
        _write(tmp_path, "alpha.py", "from backend.alpha import something\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "alpha" not in g.adj["alpha"]

    def test_multiple_edges_from_one_module(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "")
        _write(tmp_path, "b.py", "")
        _write(tmp_path, "c.py", "from backend.a import x\nfrom backend.b import y\n")
        g = DepGraph(backend_dir=tmp_path)
        assert set(g.adj["c"]) == {"a", "b"}

    def test_reverse_adjacency_built(self, tmp_path: Path) -> None:
        _write(tmp_path, "lib.py", "")
        _write(tmp_path, "consumer.py", "from backend.lib import thing\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "consumer" in g.rev["lib"]

    def test_import_backend_x_syntax(self, tmp_path: Path) -> None:
        """import backend.X style is also recognised."""
        _write(tmp_path, "core.py", "")
        _write(tmp_path, "user.py", "import backend.core\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "core" in g.adj["user"]

    def test_from_backend_import_x_syntax(self, tmp_path: Path) -> None:
        """from backend import X syntax — X must be a known module."""
        _write(tmp_path, "utils.py", "")
        _write(tmp_path, "main.py", "from backend import utils\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "utils" in g.adj["main"]

    def test_unknown_dep_not_added(self, tmp_path: Path) -> None:
        """Imports of modules not in the scanned dir are ignored."""
        _write(tmp_path, "solo.py", "from backend.nonexistent import foo\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.adj["solo"] == []

    def test_test_prefix_files_skipped(self, tmp_path: Path) -> None:
        """Files starting with test_ are excluded from the graph."""
        _write(tmp_path, "real.py", "")
        _write(tmp_path, "test_real.py", "from backend.real import x\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "test_real" not in g.adj
        # real module still present with no deps
        assert g.adj["real"] == []

    def test_dedup_edges(self, tmp_path: Path) -> None:
        """Duplicate import lines don't produce duplicate edges."""
        _write(tmp_path, "base.py", "")
        _write(tmp_path, "dup.py",
               "from backend.base import x\nfrom backend.base import y\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.adj["dup"].count("base") == 1


# ---------------------------------------------------------------------------
# No-import edge case
# ---------------------------------------------------------------------------

class TestNoImports:
    def test_isolated_module_has_empty_adj(self, tmp_path: Path) -> None:
        _write(tmp_path, "isolated.py", "x = 1\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.adj["isolated"] == []

    def test_isolated_module_has_empty_rev(self, tmp_path: Path) -> None:
        _write(tmp_path, "isolated.py", "x = 1\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.rev["isolated"] == []

    def test_empty_directory(self, tmp_path: Path) -> None:
        g = DepGraph(backend_dir=tmp_path)
        assert g.adj == {}
        assert g.rev == {}


# ---------------------------------------------------------------------------
# Syntax error handling
# ---------------------------------------------------------------------------

class TestSyntaxErrorHandling:
    def test_broken_file_skipped_gracefully(self, tmp_path: Path) -> None:
        """A file with a SyntaxError doesn't raise — the module just has no deps."""
        _write(tmp_path, "good.py", "")
        _write(tmp_path, "broken.py", "def (\n")  # deliberate SyntaxError
        g = DepGraph(backend_dir=tmp_path)
        # broken.py is still discovered as a module (it passed glob), but has no edges
        assert "good" in g.adj
        # broken is in adj dict (initialised before parse), with no deps
        if "broken" in g.adj:
            assert g.adj["broken"] == []

    def test_good_module_unaffected_by_broken_sibling(self, tmp_path: Path) -> None:
        _write(tmp_path, "ok.py", "from backend.lib import x\n")
        _write(tmp_path, "lib.py", "")
        _write(tmp_path, "bad.py", "def bad syntax here ((\n")
        g = DepGraph(backend_dir=tmp_path)
        assert "lib" in g.adj["ok"]


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

class TestImpactAnalysis:
    def _build_chain(self, tmp_path: Path) -> DepGraph:
        """Build: a <- b <- c (c imports b imports a)."""
        _write(tmp_path, "a.py", "")
        _write(tmp_path, "b.py", "from backend.a import x\n")
        _write(tmp_path, "c.py", "from backend.b import y\n")
        return DepGraph(backend_dir=tmp_path)

    def test_direct_dependents(self, tmp_path: Path) -> None:
        g = self._build_chain(tmp_path)
        result = g.impact("a")
        assert "b" in result["direct_dependents"]

    def test_transitive_dependents(self, tmp_path: Path) -> None:
        g = self._build_chain(tmp_path)
        result = g.impact("a")
        assert "c" in result["transitive_dependents"]

    def test_depth_nonzero(self, tmp_path: Path) -> None:
        g = self._build_chain(tmp_path)
        result = g.impact("a")
        assert result["depth"] >= 1

    def test_leaf_has_no_dependents(self, tmp_path: Path) -> None:
        g = self._build_chain(tmp_path)
        result = g.impact("c")
        assert result["direct_dependents"] == []
        assert result["transitive_dependents"] == []
        assert result["depth"] == 0

    def test_unknown_module_returns_error_key(self, tmp_path: Path) -> None:
        _write(tmp_path, "x.py", "")
        g = DepGraph(backend_dir=tmp_path)
        result = g.impact("ghost")
        assert "error" in result
        assert result["module"] == "ghost"

    def test_diamond_dependency(self, tmp_path: Path) -> None:
        """b and c both import a; d imports both b and c."""
        _write(tmp_path, "a.py", "")
        _write(tmp_path, "b.py", "from backend.a import x\n")
        _write(tmp_path, "c.py", "from backend.a import y\n")
        _write(tmp_path, "d.py", "from backend.b import p\nfrom backend.c import q\n")
        g = DepGraph(backend_dir=tmp_path)
        result = g.impact("a")
        trans = set(result["transitive_dependents"])
        assert "b" in trans or "b" in result["direct_dependents"]
        assert "c" in trans or "c" in result["direct_dependents"]
        assert "d" in trans


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------

class TestOutputFormats:
    def _simple_graph(self, tmp_path: Path) -> DepGraph:
        _write(tmp_path, "node_a.py", "")
        _write(tmp_path, "node_b.py", "from backend.node_a import x\n")
        return DepGraph(backend_dir=tmp_path)

    # JSON ----------------------------------------------------------------

    def test_to_json_parses(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        data = g.to_json()
        # Must be JSON-serialisable
        dumped = json.dumps(data)
        loaded = json.loads(dumped)
        assert isinstance(loaded, dict)

    def test_to_json_has_required_keys(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        data = g.to_json()
        for key in ("modules", "cycles", "hubs", "stats"):
            assert key in data, f"missing key: {key}"

    def test_to_json_modules_list_contains_both(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        names = {m["name"] for m in g.to_json()["modules"]}
        assert "node_a" in names
        assert "node_b" in names

    def test_to_json_module_entry_has_deps(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        entry = next(m for m in g.to_json()["modules"] if m["name"] == "node_b")
        assert "node_a" in entry["deps"]

    def test_to_json_module_entry_fields(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        entry = g.to_json()["modules"][0]
        for field in ("name", "deps", "dependents", "in_degree", "out_degree", "category"):
            assert field in entry

    def test_to_json_stats_fields(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        stats = g.to_json()["stats"]
        for field in ("total_modules", "total_edges", "max_depth", "avg_degree"):
            assert field in stats

    def test_to_json_stats_counts(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        stats = g.to_json()["stats"]
        assert stats["total_modules"] == 2
        assert stats["total_edges"] == 1

    # DOT ------------------------------------------------------------------

    def test_to_dot_starts_with_digraph(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        dot = g.to_dot()
        assert dot.strip().startswith("digraph")

    def test_to_dot_contains_both_nodes(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        dot = g.to_dot()
        assert '"node_a"' in dot
        assert '"node_b"' in dot

    def test_to_dot_contains_edge(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        dot = g.to_dot()
        assert '"node_b" -> "node_a"' in dot

    def test_to_dot_closes_brace(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        dot = g.to_dot()
        assert dot.strip().endswith("}")

    def test_to_dot_empty_graph(self, tmp_path: Path) -> None:
        g = DepGraph(backend_dir=tmp_path)
        dot = g.to_dot()
        assert "digraph" in dot

    # ASCII ----------------------------------------------------------------

    def test_to_ascii_returns_string(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        result = g.to_ascii()
        assert isinstance(result, str)

    def test_to_ascii_contains_root(self, tmp_path: Path) -> None:
        """node_b has no incoming edges — it's a tree root."""
        g = self._simple_graph(tmp_path)
        result = g.to_ascii()
        assert "node_b" in result

    def test_to_ascii_with_explicit_root(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        result = g.to_ascii(root="node_b")
        assert "node_b" in result

    def test_to_ascii_unknown_root_returns_empty_ish(self, tmp_path: Path) -> None:
        g = self._simple_graph(tmp_path)
        result = g.to_ascii(root="does_not_exist")
        # Should return something (empty string or just whitespace)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_no_cycles_in_dag(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "")
        _write(tmp_path, "b.py", "from backend.a import x\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.cycles() == []

    def test_direct_cycle_detected(self, tmp_path: Path) -> None:
        """a imports b and b imports a — should be a cycle SCC."""
        _write(tmp_path, "a.py", "from backend.b import x\n")
        _write(tmp_path, "b.py", "from backend.a import y\n")
        g = DepGraph(backend_dir=tmp_path)
        cycs = g.cycles()
        assert len(cycs) >= 1
        members = set(cycs[0])
        assert members == {"a", "b"}

    def test_three_way_cycle(self, tmp_path: Path) -> None:
        _write(tmp_path, "x.py", "from backend.z import v\n")
        _write(tmp_path, "y.py", "from backend.x import v\n")
        _write(tmp_path, "z.py", "from backend.y import v\n")
        g = DepGraph(backend_dir=tmp_path)
        cycs = g.cycles()
        assert len(cycs) >= 1
        all_members = {m for scc in cycs for m in scc}
        assert {"x", "y", "z"}.issubset(all_members)

    def test_cycles_returns_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "standalone.py", "")
        g = DepGraph(backend_dir=tmp_path)
        assert isinstance(g.cycles(), list)


# ---------------------------------------------------------------------------
# Hub analysis
# ---------------------------------------------------------------------------

class TestHubAnalysis:
    def test_no_hubs_below_threshold(self, tmp_path: Path) -> None:
        """With only 2 modules and 1 edge, nothing reaches default threshold of 5."""
        _write(tmp_path, "base.py", "")
        _write(tmp_path, "user.py", "from backend.base import x\n")
        g = DepGraph(backend_dir=tmp_path)
        assert g.hubs(threshold=5) == []

    def test_hub_detected_at_threshold(self, tmp_path: Path) -> None:
        """Create 3 importers to make hub detectable at threshold=3."""
        _write(tmp_path, "hub.py", "")
        for i in range(3):
            _write(tmp_path, f"importer{i}.py", f"from backend.hub import x{i}\n")
        g = DepGraph(backend_dir=tmp_path)
        hubs = g.hubs(threshold=3)
        assert len(hubs) >= 1
        assert hubs[0]["name"] == "hub"
        assert hubs[0]["in_degree"] == 3

    def test_hub_entry_has_required_fields(self, tmp_path: Path) -> None:
        _write(tmp_path, "core.py", "")
        for i in range(2):
            _write(tmp_path, f"dep{i}.py", f"from backend.core import y{i}\n")
        g = DepGraph(backend_dir=tmp_path)
        hubs = g.hubs(threshold=2)
        assert len(hubs) >= 1
        entry = hubs[0]
        for field in ("name", "in_degree", "out_degree", "dependents"):
            assert field in entry

    def test_hubs_sorted_descending(self, tmp_path: Path) -> None:
        """hub_a has 3 dependents, hub_b has 2; hub_a must come first."""
        _write(tmp_path, "hub_a.py", "")
        _write(tmp_path, "hub_b.py", "")
        for i in range(3):
            _write(tmp_path, f"a_dep{i}.py", f"from backend.hub_a import z{i}\n")
        for i in range(2):
            _write(tmp_path, f"b_dep{i}.py", f"from backend.hub_b import w{i}\n")
        g = DepGraph(backend_dir=tmp_path)
        hubs = g.hubs(threshold=2)
        assert len(hubs) >= 2
        assert hubs[0]["in_degree"] >= hubs[1]["in_degree"]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_returns_dict(self, tmp_path: Path) -> None:
        _write(tmp_path, "m.py", "")
        g = DepGraph(backend_dir=tmp_path)
        assert isinstance(g.stats(), dict)

    def test_stats_correct_counts(self, tmp_path: Path) -> None:
        _write(tmp_path, "p.py", "")
        _write(tmp_path, "q.py", "from backend.p import x\n")
        _write(tmp_path, "r.py", "from backend.p import y\n")
        g = DepGraph(backend_dir=tmp_path)
        s = g.stats()
        assert s["total_modules"] == 3
        assert s["total_edges"] == 2

    def test_stats_empty_graph(self, tmp_path: Path) -> None:
        g = DepGraph(backend_dir=tmp_path)
        s = g.stats()
        assert s["total_modules"] == 0
        assert s["total_edges"] == 0
        assert s["avg_degree"] == 0.0
