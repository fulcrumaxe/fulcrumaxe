"""tests/test_codebase_index.py

Regression guard for scripts/codebase-index.py (S3).

Before the fix, RUNTIME_ROOT/OUTER_ROOT pointed into the archived component
directory (0 tracked files), so the Python index was always empty: the
script exited 0 reporting "Indexed 0 Python files" — a silent, green,
useless result. The exit code alone was never the signal; the count was.

This test asserts the script actually indexes something from the tree it
ships with (backend/, scripts/, hooks/).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEXER = REPO_ROOT / "scripts" / "codebase-index.py"


def _run_indexer(output_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INDEXER), "--output", str(output_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_indexes_more_than_zero_python_files(tmp_path):
    """The substantive check: N > 0, not just exit code 0.

    A script that indexes an empty/absent directory can still exit 0 while
    reporting N == 0 — that split (green exit code, useless count) is
    exactly what made the pre-fix version silent and useless.
    """
    out = tmp_path / "index.json"
    result = _run_indexer(out)

    assert result.returncode == 0, f"non-zero exit: {result.stderr}"

    m = re.search(r"Indexed (\d+) Python files", result.stdout)
    assert m, f"expected 'Indexed N Python files' in stdout, got: {result.stdout!r}"
    n = int(m.group(1))
    assert n > 0, f"expected N > 0 Python files indexed, got N={n}"

    data = json.loads(out.read_text())
    assert len(data["python"]) == n


def test_indexes_backend_and_scripts_files(tmp_path):
    """The index should contain real, current files from backend/ and scripts/,
    not just an empty dict that happens to report a nonzero-looking shape."""
    out = tmp_path / "index.json"
    result = _run_indexer(out)
    assert result.returncode == 0

    data = json.loads(out.read_text())
    py_paths = list(data["python"].keys())

    assert any(p.startswith("backend/") for p in py_paths), py_paths[:10]
    assert any(p.startswith("scripts/") for p in py_paths), py_paths[:10]
    # This file itself should be indexed.
    assert "scripts/codebase-index.py" in py_paths


def test_no_duplicates_key_left_over(tmp_path):
    """The two-root duplicate-comparison machinery was removed along with the
    archived runtime/outer split — its output key should not linger."""
    out = tmp_path / "index.json"
    result = _run_indexer(out)
    assert result.returncode == 0

    data = json.loads(out.read_text())
    assert "duplicates" not in data
