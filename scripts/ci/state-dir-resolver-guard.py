#!/usr/bin/env python3
"""state-dir-resolver-guard.py — keep backend/state_paths.py the ONLY module
that turns AUTONOMOUS_TEAM_STATE_DIR into a filesystem path (D#2183).

Background
----------
D#2183 set out to fix the two readers named in its filing and found four —
backend/worktree_state_watcher.py (the worst — it also bypassed the pytest
sandbox guard), backend/corpus_drift/claims/dial_directive_emission.py and
its byte-identical clone scripts/corpus-drift-audit.py, and scripts/
coldstart-interview/seed-backlog.py (a different shape, falling back to the
relative literal ".autonomous-team"). Writing THIS guard and running it
against the repo found three more the Discussion's own re-measurement had
missed: backend/orchestrator/cost_verification.py, credit_tracker.py and
sdk_status.py — proof of the Discussion's own point, that a line-oriented
count keeps drifting and a lint answers it once. None of the seven
validated a relative or set-but-empty value the way state_paths does, so
the same input raised in one module and silently "succeeded" (into the
wrong directory) in another. Two modules, backend/_repo.py and
backend/_repo_planes.py, read the variable directly ON PURPOSE —
state_paths itself raises under pytest when the variable is unset, and
repo-slug resolution must stay importable everywhere, including at
collection time.

Detection is AST-based, not line-oriented (Implementation Notes, D#2183):
a regex/grep approach missed both deliberate readers because they use the
multi-line ``os.environ.get(\\n    "AUTONOMOUS_TEAM_STATE_DIR",\\n    default,\\n)``
form — the string constant is never on the same line as ``os.environ.get(``.
This walks the parsed tree instead, so formatting cannot hide a match.

What counts as a "resolve"
---------------------------
``os.environ.get("AUTONOMOUS_TEAM_STATE_DIR", ...)``, ``os.getenv(...)`` and
a Load-context ``os.environ["AUTONOMOUS_TEAM_STATE_DIR"]`` are all resolver
reads and get flagged (unless allowlisted). A Store-context subscript
assignment (``os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = ...``) or a
``.pop("AUTONOMOUS_TEAM_STATE_DIR", ...)`` call is a WRITE, not a resolve,
and is never flagged on its own.

One structural exemption, found while writing this guard: a function that
both reads AND writes the variable (save the old value, set a new one,
restore the old value on exit — see backend/rpc_project_scope.py's
``_EnvScope`` context manager) is an override utility, not a resolver, so a
read is excluded when a write to the same key exists in the SAME immediate
function scope. This is deliberately narrow — a read in one function paired
with a write in a different function is still flagged — because every real
case in this repo's corpus at time of writing has the read and the write in
the same function body, and a narrower exemption is the safer direction (it
can miss an unusual save/restore shape; it cannot manufacture a false
exemption for an unrelated resolver).

``archive/`` is excluded entirely (frozen snapshots, not live code).

Allowlist
---------
Modelled on scripts/check-tests-fixed-tmp-paths.sh (D#2254) and
scripts/check-tests-live-state-paths.sh (D#2267): a file-based allowlist of
shape ``<path>:<literal>:<reason>`` (one entry per file; ``<literal>`` is
always ``AUTONOMOUS_TEAM_STATE_DIR`` here since this guard watches exactly
one variable, kept as its own field for parity with the sibling format and
in case a future guard reuses this shape for a different variable), with
banned-reason validation and dangling/stale-entry detection so the
allowlist can't silently rot: an entry naming a file that no longer reads
the variable, or a file `git ls-files` no longer tracks, is a hard failure.

Run from the repo root:

    python3 scripts/ci/state-dir-resolver-guard.py
    python3 scripts/ci/state-dir-resolver-guard.py --allowlist /path/to/other.txt

Exit 0: no unlisted direct reader; every allowlist entry is live.
Exit 1: an unlisted reader, a malformed/banned/duplicate allowlist entry, or
        a dangling/stale allowlist entry.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TARGET_VAR = "AUTONOMOUS_TEAM_STATE_DIR"
DEFAULT_ALLOWLIST = REPO_ROOT / "scripts" / "fixtures" / "allowed_state_dir_resolvers.txt"
SCAN_DIRS = ("backend", "scripts")
EXCLUDE_DIR_NAMES = {"archive"}
# The canonical resolver itself is the one file allowed to read the
# variable without an allowlist entry — it is what every other file in
# EXCLUDE_FILES below is required to route through instead.
EXCLUDE_FILES = {"backend/state_paths.py"}

_BANNED_REASON_SUBSTRINGS = [
    "todo",
    "tbd",
    "n/a",
    "not sure",
    "fix later",
    "temporary",
]

_FAILED = False


def _fail(msg: str) -> None:
    global _FAILED
    print(f"FAIL: {msg}", file=sys.stderr)
    _FAILED = True


# ---------------------------------------------------------------------------
# AST matching
# ---------------------------------------------------------------------------


def _is_target_const(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == TARGET_VAR


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_env_get_or_getenv(node: ast.Call) -> bool:
    """os.environ.get(TARGET, ...) or os.getenv(TARGET, ...)"""
    if not node.args or not _is_target_const(node.args[0]):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
        return True
    if isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(func.value, ast.Name):
        return func.value.id == "os"
    return False


def _is_env_pop(node: ast.Call) -> bool:
    """os.environ.pop(TARGET, ...) — a write (removal), not a resolve."""
    if not node.args or not _is_target_const(node.args[0]):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "pop" and _is_os_environ(func.value)


def _subscript_index(node: ast.Subscript) -> ast.expr:
    idx = node.slice
    # Python 3.8 wraps the subscript index in ast.Index; 3.9+ does not.
    if hasattr(ast, "Index") and isinstance(idx, ast.Index):  # noqa: SIM108 - py3.8 compat
        return idx.value  # type: ignore[attr-defined]
    return idx


def _is_env_target_subscript(node: ast.Subscript) -> bool:
    return _is_os_environ(node.value) and _is_target_const(_subscript_index(node))


class _ScopeVisitor(ast.NodeVisitor):
    """Records every TARGET_VAR read, tagged with its innermost enclosing
    function (or "<module>"), and every function scope that also contains a
    write to TARGET_VAR — see module docstring for why reads in a
    read+write scope are exempt.
    """

    def __init__(self) -> None:
        self._scope_stack: list[str] = ["<module>"]
        self.reads: list[tuple[tuple[str, ...], int]] = []
        self.write_scopes: set[tuple[str, ...]] = set()

    def _scope_key(self) -> tuple[str, ...]:
        return tuple(self._scope_stack)

    def _visit_function(self, node: ast.AST) -> None:
        self._scope_stack.append(f"{node.name}:{node.lineno}")  # type: ignore[attr-defined]
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if _is_env_get_or_getenv(node):
            self.reads.append((self._scope_key(), node.lineno))
        elif _is_env_pop(node):
            self.write_scopes.add(self._scope_key())
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_env_target_subscript(node):
            if isinstance(node.ctx, ast.Load):
                self.reads.append((self._scope_key(), node.lineno))
            else:  # Store or Del
                self.write_scopes.add(self._scope_key())
        self.generic_visit(node)


def scan_file(path: Path) -> list[int]:
    """Return sorted line numbers of unexempted TARGET_VAR reads in *path*."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        _fail(f"could not parse {path}: {exc}")
        return []
    visitor = _ScopeVisitor()
    visitor.visit(tree)
    return sorted(
        lineno for scope, lineno in visitor.reads if scope not in visitor.write_scopes
    )


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------


def iter_candidate_files() -> list[str]:
    files: list[str] = []
    for base in SCAN_DIRS:
        base_path = REPO_ROOT / base
        if not base_path.is_dir():
            continue
        for p in sorted(base_path.rglob("*.py")):
            rel = p.relative_to(REPO_ROOT)
            if EXCLUDE_DIR_NAMES & set(rel.parts):
                continue
            rel_str = str(rel)
            if rel_str in EXCLUDE_FILES:
                continue
            files.append(rel_str)
    return files


def _git_tracked_files() -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return set()
    return {line for line in out.splitlines() if line}


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def load_allowlist(path: Path) -> dict[tuple[str, str], str]:
    """Format: <path>:<literal>:<reason>, one entry per line. Blank lines
    and lines starting with # are ignored.
    """
    entries: dict[tuple[str, str], str] = {}
    if not path.exists():
        return entries
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            _fail(f"malformed allowlist entry (need path:literal:reason) at {path}:{lineno}: {stripped}")
            continue
        entry_path, literal, reason = (p.strip() for p in parts)
        reason_lc = reason.lower()
        banned = next((b for b in _BANNED_REASON_SUBSTRINGS if b in reason_lc), None)
        if banned is not None:
            _fail(f"banned reason ('{banned}') in allowlist entry '{entry_path}:{literal}' — {reason}")
            continue
        key = (entry_path, literal)
        if key in entries:
            _fail(f"duplicate allowlist entry '{entry_path}:{literal}' at {path}:{lineno}")
            continue
        entries[key] = reason
    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Override the allowlist file path (default: scripts/fixtures/allowed_state_dir_resolvers.txt)",
    )
    args = parser.parse_args(argv)
    allowlist_path = args.allowlist if args.allowlist is not None else DEFAULT_ALLOWLIST

    allow_entries = load_allowlist(allowlist_path)
    allow_seen = {key: False for key in allow_entries}
    tracked = _git_tracked_files()

    violations: dict[str, list[int]] = {}
    for rel in iter_candidate_files():
        lines = scan_file(REPO_ROOT / rel)
        if not lines:
            continue
        key = (rel, TARGET_VAR)
        if key in allow_entries:
            allow_seen[key] = True
            continue
        violations[rel] = lines

    for path, lines in sorted(violations.items()):
        line_list = ", ".join(str(n) for n in lines)
        _fail(
            f"{path}: reads {TARGET_VAR} directly at line(s) {line_list} — "
            f"route through backend.state_paths or add an allowlist entry with a reason"
        )

    for (path, literal), seen in sorted(allow_seen.items()):
        if tracked and path not in tracked:
            _fail(f"dangling allowlist entry '{path}:{literal}' — {path} is not in git ls-files")
            continue
        if not seen:
            _fail(
                f"stale allowlist entry '{path}:{literal}' — no current unallowlisted "
                f"read of {literal} in {path}"
            )

    if _FAILED:
        return 1

    print(
        f"OK: {TARGET_VAR} resolved only by backend.state_paths.py "
        f"({len(allow_entries)} allowlisted reader(s), reasons recorded)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
