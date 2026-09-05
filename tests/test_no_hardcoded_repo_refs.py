"""tests/test_no_hardcoded_repo_refs.py

Sweep test: ensure no operative hardcoded "autonomous-agent-7/autonomous-forever"
references remain in backend Python modules or scripts.

ALLOWED (excluded from sweep):
  - The _repo.py resolver itself (it IS the fallback)
  - os.environ.get(..., "autonomous-agent-7/autonomous-forever") — safe fallbacks
  - spawn_prompt.py docstring examples
  - spawn_templates.py comment + fallback default
  - Test fixture strings (test files)
  - Documentation markdown files
  - Loop-bootstrap snapshot mirrors
  - Comments (lines starting with # or inside triple-quoted strings)

The test uses line-level analysis to distinguish operative from non-operative refs.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HARDCODED = "autonomous-agent-7/autonomous-forever"

# ──────────────────────────────────────────────────────────────────────────────
# Files/dirs that are entirely allowed to contain the hardcoded string
# ──────────────────────────────────────────────────────────────────────────────
ALLOWED_PATHS: list[str] = [
    # Canonical resolvers — they ARE the fallback, by design
    "backend/_repo.py",
    "scripts/lib/repo-resolve.sh",
    # Docstring examples that reference the real repo as illustration
    "backend/spawn_prompt.py",
    "backend/spawn_templates.py",
    # Snapshot mirrors — kept in sync with originals, checked separately
    "loop-bootstrap/",
    # Test fixtures — reference the real repo for test assertions
    "tests/",
    "backend/tests/",
    # Inline test files in backend/ (test_*.py files co-located with modules)
    # These are test fixtures/assertions and legitimately reference the real repo slug.
    "backend/test_",
    # Training scripts that reference the model/adapter by full name, not repo slug
    # (only the git-clone URL should be dynamic; model adapter names are not repo slugs)
    # Documentation and memory files
    "scripts/memory-triage/",
    "wiki/",
    ".autonomous-team/",
    # Script that explicitly uses ${REPO:-fallback} idiom
    "scripts/test-post-merge-hook-autoclose.sh",
]

# ──────────────────────────────────────────────────────────────────────────────
# Line-level patterns that are ALLOWED even in swept files
# ──────────────────────────────────────────────────────────────────────────────
ALLOWED_LINE_PATTERNS: list[re.Pattern] = [
    # Python env fallback: os.environ.get("...", "autonomous-agent-7/autonomous-forever")
    re.compile(r'os\.environ\.get\([^)]+,\s*["\']autonomous-agent-7/autonomous-forever["\']'),
    # Python .autonomous-team/project.json fallback load pattern
    re.compile(r'return\s+os\.environ\.get\(.*autonomous-agent-7/autonomous-forever'),
    # Shell fallback: ${VAR:-autonomous-agent-7/autonomous-forever} or ${2:-...} positional
    re.compile(r'\$\{[A-Z_0-9]+:-autonomous-agent-7/autonomous-forever\}'),
    # Comment-only lines (shell #, Python #)
    re.compile(r'^\s*#'),
    # Python docstring / triple-quote context (heuristic: line starts with spaces+""")
    re.compile(r'^\s*"""'),
    # Post-merge hook _PMH_REPO env fallback
    re.compile(r'_PMH_REPO.*autonomous-agent-7/autonomous-forever'),
    # Rotate-team-log comment
    re.compile(r'#.*autonomous-agent-7/autonomous-forever'),
]


def _is_allowed_path(rel: str) -> bool:
    for prefix in ALLOWED_PATHS:
        if rel.startswith(prefix):
            return True
    return False


def _is_allowed_line(line: str) -> bool:
    for pat in ALLOWED_LINE_PATTERNS:
        if pat.search(line):
            return True
    return False


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (line_number, line) for operative hardcoded refs."""
    violations: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations

    for lineno, line in enumerate(text.splitlines(), start=1):
        if HARDCODED not in line:
            continue
        if _is_allowed_line(line):
            continue
        violations.append((lineno, line.rstrip()))
    return violations


def _collect_files(extensions: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for ext in extensions:
        files.extend(REPO_ROOT.rglob(f"*{ext}"))
    # Exclude hidden dirs (.git, .claude, __pycache__) and loop-bootstrap snapshots
    out = []
    for f in files:
        rel = str(f.relative_to(REPO_ROOT))
        if any(part.startswith(".") for part in f.parts[len(REPO_ROOT.parts):]):
            continue
        if "__pycache__" in rel:
            continue
        if _is_allowed_path(rel):
            continue
        out.append(f)
    return out


def test_no_hardcoded_repo_in_backend_python():
    """All backend/*.py files (except allowed) must not contain operative hardcoded repo refs."""
    files = _collect_files((".py",))
    backend_files = [f for f in files if str(f.relative_to(REPO_ROOT)).startswith("backend/")]

    all_violations: list[str] = []
    for path in sorted(backend_files):
        rel = str(path.relative_to(REPO_ROOT))
        violations = _scan_file(path)
        for lineno, line in violations:
            all_violations.append(f"{rel}:{lineno}: {line}")

    assert not all_violations, (
        f"Found {len(all_violations)} operative hardcoded repo ref(s) in backend/:\n"
        + "\n".join(f"  {v}" for v in all_violations)
    )


def test_no_hardcoded_repo_in_scripts():
    """All scripts/*.sh and scripts/**/*.sh files must not contain operative hardcoded repo refs."""
    files = _collect_files((".sh",))
    script_files = [f for f in files if str(f.relative_to(REPO_ROOT)).startswith("scripts/")]

    all_violations: list[str] = []
    for path in sorted(script_files):
        rel = str(path.relative_to(REPO_ROOT))
        violations = _scan_file(path)
        for lineno, line in violations:
            all_violations.append(f"{rel}:{lineno}: {line}")

    assert not all_violations, (
        f"Found {len(all_violations)} operative hardcoded repo ref(s) in scripts/:\n"
        + "\n".join(f"  {v}" for v in all_violations)
    )


def test_no_hardcoded_repo_in_spawn_templates():
    """All backend/spawn_templates/*.tmpl files must use {{REPO}} not hardcoded slug."""
    tmpl_dir = REPO_ROOT / "backend" / "spawn_templates"
    all_violations: list[str] = []

    for path in sorted(tmpl_dir.rglob("*.tmpl")) + sorted(tmpl_dir.rglob("*.md")):
        rel = str(path.relative_to(REPO_ROOT))
        violations = _scan_file(path)
        for lineno, line in violations:
            all_violations.append(f"{rel}:{lineno}: {line}")

    assert not all_violations, (
        f"Found {len(all_violations)} operative hardcoded repo ref(s) in spawn_templates/:\n"
        + "\n".join(f"  {v}" for v in all_violations)
    )


def test_no_hardcoded_repo_in_scripts_python():
    """All scripts/**/*.py files (except allowed) must not contain operative hardcoded repo refs."""
    files = _collect_files((".py",))
    script_py_files = [f for f in files if str(f.relative_to(REPO_ROOT)).startswith("scripts/")]

    all_violations: list[str] = []
    for path in sorted(script_py_files):
        rel = str(path.relative_to(REPO_ROOT))
        violations = _scan_file(path)
        for lineno, line in violations:
            all_violations.append(f"{rel}:{lineno}: {line}")

    assert not all_violations, (
        f"Found {len(all_violations)} operative hardcoded repo ref(s) in scripts/*.py:\n"
        + "\n".join(f"  {v}" for v in all_violations)
    )
