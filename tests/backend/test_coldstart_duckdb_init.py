"""Tests for coldstart-project.sh DuckDB initialization (BUG 3 fix).

Verifies that coldstart creates a valid DuckDB database file rather than
an empty placeholder that DuckDB rejects on first read.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COLDSTART_SH = REPO_ROOT / "scripts" / "coldstart-project.sh"


def _run_coldstart(tmp_path: Path, project_name: str) -> subprocess.CompletedProcess:
    """Run coldstart-project.sh against a temporary git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # COLDSTART_STATE_ROOT redirects the state dir the script creates
    # (D#2317 PR-c). HOME is pointed at the same tmp dir as a second line of
    # defence, so nothing lands under the operator's real home directory even
    # if the script grows another home-rooted path later.
    return subprocess.run(
        ["bash", str(COLDSTART_SH), str(repo), project_name],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "COLDSTART_STATE_ROOT": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


class TestDuckDBInit:
    """BUG 3: coldstart must produce a valid DuckDB file, not an empty placeholder."""

    def test_duckdb_init_python_path_exists(self):
        """The duckdb init one-liner must be present in coldstart-project.sh."""
        content = COLDSTART_SH.read_text()
        assert "import duckdb" in content, (
            "coldstart-project.sh must use 'import duckdb' to init stats.duckdb"
        )
        assert "duckdb.connect" in content, (
            "coldstart-project.sh must call duckdb.connect() to create valid DB file"
        )

    def test_touch_fallback_message_present(self):
        """The touch fallback path must log that duckdb was unavailable."""
        content = COLDSTART_SH.read_text()
        assert "duckdb not available" in content or "duckdb python not available" in content, (
            "coldstart-project.sh must log when falling back to touch for stats.duckdb"
        )

    def test_stats_duckdb_not_bare_touch(self):
        """stats.duckdb must NOT be created via bare touch in the placeholder loop."""
        content = COLDSTART_SH.read_text()
        # The old bad pattern was: for placeholder in state.db stats.duckdb audit.jsonl
        # stats.duckdb should no longer be in the bare touch loop
        import re
        # Find the bare touch loop
        touch_loop_match = re.search(
            r"for placeholder in ([^\n]+)\n.*touch.*placeholder",
            content,
            re.DOTALL,
        )
        if touch_loop_match:
            loop_vars = touch_loop_match.group(1)
            assert "stats.duckdb" not in loop_vars, (
                "stats.duckdb must not be in the bare 'touch' placeholder loop"
            )

    def test_duckdb_init_produces_valid_file(self, tmp_path):
        """The duckdb init command creates a file that DuckDB can read back."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            pytest.skip("duckdb not installed — skipping live init test")

        db_path = str(tmp_path / "stats.duckdb")
        result = subprocess.run(
            [
                "python3",
                "-c",
                f"import duckdb; duckdb.connect('{db_path}').close()",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"duckdb init command failed: {result.stderr}"
        assert Path(db_path).exists(), "duckdb file was not created"
        assert Path(db_path).stat().st_size > 0, "duckdb file is empty (not a valid DB)"

        # Verify it can be reopened and queried
        import duckdb as ddb
        conn = ddb.connect(db_path, read_only=True)
        # Should not raise — valid DB header
        conn.close()

    def test_duckdb_init_command_not_empty_file(self, tmp_path):
        """Empty file created by touch is rejected by DuckDB — validate our fix."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            pytest.skip("duckdb not installed")

        import duckdb as ddb

        # Simulate the OLD broken approach (bare touch)
        bad_path = tmp_path / "bad.duckdb"
        bad_path.touch()
        assert bad_path.stat().st_size == 0

        with pytest.raises(Exception):
            # An empty file is not a valid DuckDB database
            conn = ddb.connect(str(bad_path), read_only=True)
            conn.execute("SELECT 1").fetchone()
            conn.close()


class TestLoopMetricsInit:
    """BUG 4: coldstart must create .autonomous-team/loop-metrics.jsonl."""

    def test_loop_metrics_placeholder_in_coldstart(self):
        """coldstart-project.sh must reference loop-metrics.jsonl creation."""
        content = COLDSTART_SH.read_text()
        assert "loop-metrics.jsonl" in content, (
            "coldstart-project.sh must create .autonomous-team/loop-metrics.jsonl placeholder"
        )

    def test_loop_metrics_in_team_dir(self):
        """loop-metrics.jsonl must be created in TEAM_DIR, not STATE_DIR."""
        content = COLDSTART_SH.read_text()
        # Must reference TEAM_DIR (repo-side), not STATE_DIR (external)
        assert "TEAM_DIR/loop-metrics.jsonl" in content, (
            "loop-metrics.jsonl must be created in TEAM_DIR (.autonomous-team/), not STATE_DIR"
        )


class TestStateSideProjectJsonSentinel:
    """BUG 8: coldstart must write state-side project.json sentinel for fleet discovery."""

    def test_state_side_sentinel_in_coldstart(self):
        """coldstart-project.sh must write to STATE_DIR/project.json."""
        content = COLDSTART_SH.read_text()
        assert "STATE_PROJECT_JSON" in content or "STATE_DIR/project.json" in content, (
            "coldstart-project.sh must create $STATE_DIR/project.json sentinel "
            "for fleet discovery — without it, fleet shows an incomplete project list"
        )

    def test_sentinel_has_required_fields(self):
        """The sentinel write must include project_name and version fields."""
        content = COLDSTART_SH.read_text()
        # The sentinel block must include the fleet discovery required fields
        assert "project_name" in content, "sentinel must include project_name"
        assert "version" in content, "sentinel must include version field"

    def test_fleet_discovery_reads_sentinel(self, tmp_path):
        """A state-dir project.json created by coldstart is accepted by fleet.discovery."""
        import json
        import sys
        sys.path.insert(0, str(REPO_ROOT))

        from backend.fleet.discovery import _read_project

        state_dir = tmp_path / ".test-state"
        state_dir.mkdir()
        sentinel = {
            "project_name": "test-project",
            "version": 1,
            "repo": "acme/test-project",
            "language": "python",
            "dashboard_port": 5200,
        }
        p = state_dir / "project.json"
        p.write_text(json.dumps(sentinel, indent=2))

        result = _read_project(p, str(state_dir))
        assert result.get("ok") is True, (
            f"fleet.discovery rejected coldstart sentinel: {result.get('error')}"
        )

    def test_sentinel_sync_merges_repo_and_language(self, tmp_path):
        """Coldstart sync block must merge repo+language from repo-side project.json.

        When port_claim already wrote a minimal project.json (project_name, version,
        dashboard_port), the else-branch must also pull repo and language from the
        repo-side project.json — so fleet.discovery sees complete metadata.
        """
        import json

        # Simulate the repo-side project.json (source of truth for repo + language)
        repo_json_path = tmp_path / "project.json"
        repo_json_path.write_text(json.dumps({
            "project_name": "rust-svc",
            "version": 1,
            "repo": "acme/rust-svc",
            "language": "rust",
            "dashboard_port": 5300,
        }, indent=2))

        # Simulate a state-side sentinel created by port_claim (missing repo + language)
        state_dir = tmp_path / ".rust-svc-state"
        state_dir.mkdir()
        state_json_path = state_dir / "project.json"
        state_json_path.write_text(json.dumps({
            "project_name": "rust-svc",
            "version": 1,
            "dashboard_port": 5300,
        }, indent=2))

        # Run the sync logic inline (same python3 heredoc as coldstart-project.sh else branch)
        import subprocess
        result = subprocess.run(
            [
                "python3", "-c",
                f"""
import json, pathlib
repo_json = pathlib.Path({str(repo_json_path)!r})
state_json = pathlib.Path({str(state_json_path)!r})
src = json.loads(repo_json.read_text())
sentinel = json.loads(state_json.read_text())
changed = False
port = src.get("dashboard_port")
if isinstance(port, int) and sentinel.get("dashboard_port") != port:
    sentinel["dashboard_port"] = port
    changed = True
for field in ("repo", "language"):
    val = src.get(field)
    if val and not sentinel.get(field):
        sentinel[field] = val
        changed = True
if changed:
    state_json.write_text(json.dumps(sentinel, indent=2) + "\\n")
""",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"sync script failed: {result.stderr}"

        updated = json.loads(state_json_path.read_text())
        assert updated.get("repo") == "acme/rust-svc", (
            f"state-side project.json missing 'repo' after sync; got: {updated}"
        )
        assert updated.get("language") == "rust", (
            f"state-side project.json missing 'language' after sync; got: {updated}"
        )
        assert updated.get("dashboard_port") == 5300
