"""
spec_external_docs.py — loud-fail enforcement of the external_docs: requirement.

Spec bodies for [Feature] and [Critical] Discussions must include an
``external_docs:`` block that anchors every non-stdlib import referenced in
the spec's technical solution with at least one URL.  This module provides
the check function used by three gates:

  Stage 1 — PM/spec render gate (spawn_templates.py render path)
  Stage 2 — Code-reviewer enforcement (code-reviewer.md step)
  Stage 3 — Spawn-script pre-spawn gate (spawn-agent.sh)

Usage::

    from backend.spec_external_docs import check_imports_have_docs, ExternalDocsError

    missing = check_imports_have_docs(diff="import requests\\n...", spec_body=spec_text)
    # Returns list of module names missing a URL anchor in spec_body.
    # Empty list means all external imports are documented.

    # Or use the raising wrapper for Stage 1 hard-fail:
    check_imports_have_docs_or_raise(diff=diff, spec_body=spec_text)

Override::

    Set ALLOW_MISSING_EXTERNAL_DOCS=1 in environment to bypass the check.
    ALLOW_MISSING_EXTERNAL_DOCS_REASON must also be set; it is written to audit.jsonl.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWLIST_PATH = Path(__file__).parent / "spec_external_docs_allowlist.txt"

# Regex: Python stdlib module names (sys.stdlib_module_names available in 3.10+)
# Falls back to a small hard-coded set for older Python.
def _stdlib_names() -> frozenset[str]:
    if hasattr(sys, "stdlib_module_names"):
        return frozenset(sys.stdlib_module_names)
    # Minimal fallback — Python 3.10+ is assumed in CI.
    return frozenset({
        "abc", "ast", "asyncio", "base64", "builtins", "collections", "copy",
        "csv", "dataclasses", "datetime", "enum", "functools", "hashlib",
        "http", "importlib", "inspect", "io", "itertools", "json", "logging",
        "math", "operator", "os", "pathlib", "pickle", "platform", "pprint",
        "queue", "random", "re", "shutil", "signal", "socket", "sqlite3",
        "ssl", "stat", "string", "struct", "subprocess", "sys", "tempfile",
        "textwrap", "threading", "time", "traceback", "typing", "unittest",
        "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
    })


_STDLIB_NAMES: frozenset[str] = _stdlib_names()

# Directories that never contribute first-party names. `archive/` matters most:
# archived modules must NOT stay "first-party" forever, or this would quietly
# re-open the hole the tree scan is meant to close.
_FIRST_PARTY_EXCLUDED_DIRS = frozenset({"archive", "node_modules", ".git"})

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _first_party_names() -> frozenset[str]:
    """Return module/package names resolvable from files that exist in the repo tree.

    A name counts as first-party if either:
      - some `<name>.py` file exists anywhere under the repo root (its basename,
        without the extension) — covers `scripts/lib/route_discussion.py` imported
        via `sys.path.insert` as a bare `import route_discussion`; or
      - `<name>/` is a top-level directory at the repo root — covers `import backend`,
        `import scripts`, `import hooks`, `import testsupport`, none of which need an
        `__init__.py` to be importable (implicit namespace packages).

    This is a filesystem question with a definite answer, so it needs no manual
    upkeep the way the allowlist does. Built once and cached at module level: this
    module is imported by the reviewer's inline check, and walking ~800 `.py` files
    per call would be felt.
    """
    names: set[str] = set()

    for path in _REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(_REPO_ROOT).parts
        if any(part in _FIRST_PARTY_EXCLUDED_DIRS for part in rel_parts):
            continue
        names.add(path.stem)

    for entry in _REPO_ROOT.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in _FIRST_PARTY_EXCLUDED_DIRS or entry.name.startswith("."):
            continue
        names.add(entry.name)

    return frozenset(names)


_FIRST_PARTY_NAMES: frozenset[str] = _first_party_names()

# Grammar-accurate Python import-statement matcher, applied per line (not as
# a `finditer` over the whole text — see _extract_python_externals) against
# the line's code portion after a trailing comment and a leading diff '+'
# are stripped. Two shapes:
#   import <dotted>[ as name][, <dotted>[ as name]]*   -- anchored to the end
#     of the (cleaned) line, so a comma-list or `as` alias is allowed but
#     nothing else may follow.
#   from <dotted> import ...                            -- anchored only at
#     the start; the "import ..." tail may be anything (a name, a `(` opening
#     a multi-line list, a comma list, etc).
# Requiring the line to actually BE the import statement — rather than
# matching "import"/"from" as the first word of any line — is what keeps
# prose like "from the `~` case above" or "import stays lazy" from reading
# as import statements: a genuine import always resolves to end-of-line (for
# `import`) or is immediately followed by the literal keyword `import` (for
# `from`), and prose almost never happens to satisfy either shape.
_DOTTED_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_PY_IMPORT_STMT_RE = re.compile(
    r"^(?:"
    rf"import\s+(?P<imp>{_DOTTED_NAME})(?:\s+as\s+\w+)?"
    rf"(?:\s*,\s*{_DOTTED_NAME}(?:\s+as\s+\w+)?)*\s*;?\s*$"
    r"|"
    rf"from\s+(?P<frm>{_DOTTED_NAME})\s+import\b"
    r")"
)

# TypeScript/JS import patterns: import ... from 'module' or require('module')
# We treat any module without ./, ../, or node: prefix as external.
_TS_IMPORT_FROM_RE = re.compile(
    r"""(?:import\s[^'"]*from\s|require\s*\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Node.js built-in modules — mirrors _stdlib_names()'s role on the Python
# side. Generated from `require('module').builtinModules` on Node v22.23.1
# (68 names). Static rather than shelled out to `node` at import time: this
# module has no runtime Node dependency and must not acquire one just to
# classify imports. Regenerate with:
#   node -e "console.log(JSON.stringify(require('module').builtinModules))"
_NODE_BUILTIN_MODULES: frozenset[str] = frozenset({
    "_http_agent", "_http_client", "_http_common", "_http_incoming",
    "_http_outgoing", "_http_server", "_stream_duplex", "_stream_passthrough",
    "_stream_readable", "_stream_transform", "_stream_wrap",
    "_stream_writable", "_tls_common", "_tls_wrap", "assert", "assert/strict",
    "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns",
    "dns/promises", "domain", "events", "fs", "fs/promises", "http", "http2",
    "https", "inspector", "inspector/promises", "module", "net", "os",
    "path", "path/posix", "path/win32", "perf_hooks", "process", "punycode",
    "querystring", "readline", "readline/promises", "repl", "stream",
    "stream/consumers", "stream/promises", "stream/web", "string_decoder",
    "sys", "timers", "timers/promises", "tls", "trace_events", "tty", "url",
    "util", "util/types", "v8", "vm", "wasi", "worker_threads", "zlib",
})

# URL pattern used to verify that external_docs: block contains real URLs, not inline content.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# HTML tag pattern for external_docs: block content check (AC-PARENT-9)
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")

# Code fence inside external_docs: block
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def _load_allowlist() -> frozenset[str]:
    """Read backend/spec_external_docs_allowlist.txt. Returns empty set if missing."""
    if not _ALLOWLIST_PATH.exists():
        return frozenset()
    lines = _ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

def _strip_python_line_comment(line: str) -> str:
    """Return *line* with any trailing unquoted ``#`` comment removed.

    A ``#`` inside a single- or double-quoted string is not a comment start
    (e.g. a URL fragment quoted in a trailing comment, or a string literal
    containing ``#``) — this walks the line tracking quote state so only an
    unquoted ``#`` truncates it. Only used to narrow the TS-shaped-line guard
    below to the code portion of a line; it is not a general Python tokenizer.
    """
    in_single = False
    in_double = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#":
            return line[:i]
    return line


def _extract_python_externals(text: str) -> list[str]:
    """Return non-stdlib top-level Python module names found in *text*."""
    stdlib = _STDLIB_NAMES
    allowlist = _load_allowlist()
    first_party = _FIRST_PARTY_NAMES
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw_line in text.split("\n"):
        # A leading diff '+' marks an added line; strip it before looking at
        # the code. A '-' (removed) or unprefixed context line is left as-is,
        # same as before: unprefixed text is scanned directly (the Stage 1
        # PM path passes Spec prose, not a diff), and a '-' line simply won't
        # match the import grammar at column 0.
        line = raw_line[1:] if raw_line.startswith("+") else raw_line
        code = _strip_python_line_comment(line).strip()
        if not code:
            continue
        # TS-shaped-line guard: a real Python import line's *code* never also
        # carries a quoted module specifier after from/require. Skip matches
        # on a line whose code portion also looks like a TS/JS import — this
        # is what keeps `type` out of `import type { P } from "./types"` and
        # `something` out of `import something from '@anthropic-ai/sdk'`. In
        # practice the grammar-accurate match below already rejects these
        # shapes (a TS import line never resolves to end-of-line the way the
        # `import` form requires), but the guard is kept as an explicit,
        # documented safety net. It can only remove false positives; a
        # genuine Python import's code never matches _TS_IMPORT_FROM_RE.
        if _TS_IMPORT_FROM_RE.search(code):
            continue
        m = _PY_IMPORT_STMT_RE.match(code)
        if not m:
            continue
        dotted = m.group("imp") or m.group("frm")
        name = dotted.split(".")[0]
        if name in stdlib:
            continue
        if name in allowlist:
            continue
        if name in first_party:
            continue
        if name not in seen_set:
            seen.append(name)
            seen_set.add(name)
    return seen


def _extract_ts_externals(text: str) -> list[str]:
    """Return non-builtin TypeScript/JS module names found in *text*."""
    allowlist = _load_allowlist()
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _TS_IMPORT_FROM_RE.finditer(text):
        specifier = m.group(1)
        # Relative imports and node: built-ins are not external docs candidates.
        if specifier.startswith(("./", "../", "node:")):
            continue
        # Strip any sub-path to get the package name.
        name = specifier.split("/")[0]
        # Scoped packages: @scope/pkg → @scope/pkg
        if specifier.startswith("@"):
            parts = specifier.split("/")
            name = "/".join(parts[:2]) if len(parts) >= 2 else specifier
        # Node built-ins imported without the `node:` prefix (`fs`, `path`,
        # `url`, ...) are not external dependencies either — mirrors the
        # Python side's _STDLIB_NAMES check. Checked against the un-prefixed
        # specifier's top segment, e.g. `fs/promises` -> `fs`.
        if specifier.split("/")[0] in _NODE_BUILTIN_MODULES:
            continue
        if name in allowlist:
            continue
        if name not in seen_set:
            seen.append(name)
            seen_set.add(name)
    return seen


# ---------------------------------------------------------------------------
# Per-file diff splitting and language auto-dispatch
# ---------------------------------------------------------------------------

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(?:\S+) b/(\S+)\s*$")
_PLUS_HEADER_RE = re.compile(r"^\+\+\+ (\S+)")

_PY_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _split_diff_by_path(text: str) -> list[tuple[str, str]]:
    """Split unified-diff *text* into ``(post_image_path, section_text)`` pairs.

    Recognizes ``diff --git a/<p> b/<p>`` as the section boundary, and falls
    back to ``+++ b/<p>`` when no ``diff --git`` line is present (some
    ``gh pr diff`` output and hand-made fixtures omit it). Returns ``[]`` when
    the text has no recognizable file header at all — that absence is the
    signal for headerless source text (e.g. Spec prose, not a diff).
    """
    lines = text.splitlines(keepends=True)

    git_starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _DIFF_GIT_HEADER_RE.match(line)
        if m:
            git_starts.append((i, m.group(1)))

    if git_starts:
        sections: list[tuple[str, str]] = []
        for idx, (start, path) in enumerate(git_starts):
            end = git_starts[idx + 1][0] if idx + 1 < len(git_starts) else len(lines)
            section_text = "".join(lines[start:end])
            if re.search(r"^\+\+\+ /dev/null\s*$", section_text, re.MULTILINE):
                continue  # deleted file — no post-image, nothing to scan
            sections.append((path, section_text))
        return sections

    # Fallback: no `diff --git` headers at all — split on `+++ b/<path>` lines.
    plus_starts: list[tuple[int, Optional[str]]] = []
    for i, line in enumerate(lines):
        m = _PLUS_HEADER_RE.match(line)
        if m:
            target = m.group(1)
            if target == "/dev/null":
                plus_starts.append((i, None))
            else:
                path = target[2:] if target.startswith("b/") else target
                plus_starts.append((i, path))

    if not plus_starts:
        return []

    sections = []
    for idx, (start, path) in enumerate(plus_starts):
        if path is None:
            continue  # deleted file section, no post-image
        end = plus_starts[idx + 1][0] if idx + 1 < len(plus_starts) else len(lines)
        section_text = "".join(lines[start:end])
        sections.append((path, section_text))
    return sections


def _auto_externals(text: str) -> list[str]:
    """Dispatch per file section to the matching extractor and union results.

    Headerless text (no recognizable diff file header) runs BOTH extractors
    over the whole text, since it may be Spec prose describing either
    language's imports (the Stage 1 PM path passes Spec prose, not a diff).
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add_all(names: list[str]) -> None:
        for name in names:
            if name not in seen_set:
                seen.append(name)
                seen_set.add(name)

    sections = _split_diff_by_path(text)
    if sections:
        for path, section_text in sections:
            suffix = Path(path).suffix.lower()
            if suffix in _PY_SUFFIXES:
                _add_all(_extract_python_externals(section_text))
            elif suffix in _TS_SUFFIXES:
                _add_all(_extract_ts_externals(section_text))
            # else: unrecognized suffix — skip this section
    else:
        _add_all(_extract_python_externals(text))
        _add_all(_extract_ts_externals(text))

    return seen


# ---------------------------------------------------------------------------
# external_docs: block extraction and validation
# ---------------------------------------------------------------------------

def _extract_external_docs_block(spec_body: str) -> Optional[str]:
    """Return the text of the external_docs: section from spec_body, or None."""
    # Matches "external_docs:" (or "### external_docs") heading, captures until next
    # heading at the same or higher level or end of string.
    patterns = [
        re.compile(
            r"(?:^|\n)(?:###\s*)?external_docs\s*:?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"(?:^|\n)\*\*external_docs\*\*\s*:?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
            re.IGNORECASE | re.DOTALL,
        ),
    ]
    for pat in patterns:
        m = pat.search(spec_body)
        if m:
            return m.group(1).strip()
    return None


def _validate_external_docs_content(block_text: str) -> list[str]:
    """Return error strings if block contains inline content rather than URLs.

    Rules (AC-PARENT-9):
    - Code fences (```) inside the block → error
    - HTML tags inside the block → error
    """
    errors: list[str] = []
    if _CODE_FENCE_RE.search(block_text):
        errors.append(
            "external_docs block contains a code fence — must contain URLs only, not inline fetched content"
        )
    if _HTML_TAG_RE.search(block_text):
        errors.append(
            "external_docs block contains an HTML tag — must contain URLs only, not inline fetched content"
        )
    return errors


def _module_has_url_in_docs(module: str, block_text: str) -> bool:
    """Return True if *module* is mentioned alongside a URL in *block_text*."""
    # Find all lines that mention the module name.
    # A match means: module name appears on the same bullet/line as an http URL.
    for line in block_text.splitlines():
        if re.search(re.escape(module), line, re.IGNORECASE):
            if _URL_RE.search(line):
                return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ExternalDocsError(ValueError):
    """Raised by check_imports_have_docs_or_raise when external_docs are missing."""

    def __init__(self, missing: list[str], content_errors: list[str] | None = None) -> None:
        self.missing = missing
        self.content_errors = content_errors or []
        parts: list[str] = []
        if missing:
            parts.append(f"Spec is missing external_docs for: {', '.join(missing)}")
        if self.content_errors:
            parts.extend(self.content_errors)
        super().__init__(" | ".join(parts))


def check_imports_have_docs(
    diff: str,
    spec_body: str,
    language: str = "auto",
) -> list[str]:
    """Return the list of non-stdlib module names in *diff* that lack a URL anchor
    in *spec_body*'s ``external_docs:`` block.

    Parameters
    ----------
    diff:
        The diff text (or any Python/TypeScript source text) whose imports
        should be checked.
    spec_body:
        The full Discussion body containing the Spec, including (optionally)
        an ``external_docs:`` section.
    language:
        ``"auto"`` (default) dispatches per file: a diff is split on its file
        headers and each section is scanned with the extractor matching its
        post-image path suffix, and results are unioned across sections. Text
        with no recognizable diff header (e.g. Spec prose, not a diff) runs
        BOTH extractors and unions the result. Pass ``"python"`` or
        ``"typescript"``/``"javascript"`` to force a single extractor over
        the whole text — this keeps its exact prior behavior.

    Returns
    -------
    list[str]
        Module names that are referenced in *diff* but have no corresponding
        URL in the ``external_docs:`` block.  Returns ``[]`` when all imports
        are covered or when there are no external imports.
    """
    if language == "auto":
        externals = _auto_externals(diff)
    elif language == "python":
        externals = _extract_python_externals(diff)
    elif language in ("typescript", "javascript"):
        externals = _extract_ts_externals(diff)
    else:
        raise ValueError(f"Unsupported language: {language!r}. Use 'auto', 'python' or 'typescript'.")

    if not externals:
        return []

    block = _extract_external_docs_block(spec_body)
    if block is None:
        # No external_docs section at all — all externals are missing.
        return list(externals)

    missing = [m for m in externals if not _module_has_url_in_docs(m, block)]
    return missing


def check_imports_have_docs_or_raise(
    diff: str,
    spec_body: str,
    language: str = "auto",
    context: str = "",
) -> None:
    """Raise ExternalDocsError if any non-stdlib imports lack external_docs URLs.

    Also raises if the external_docs block contains inline content (code fences
    or HTML tags) rather than plain URLs (AC-PARENT-9).

    Parameters
    ----------
    diff:
        Source text (diff or file) to scan for imports.
    spec_body:
        Full Discussion body containing the Spec + optional external_docs block.
    language:
        ``"auto"`` (default), or ``"python"``/``"typescript"`` to force a
        single extractor. See ``check_imports_have_docs`` for dispatch rules.
    context:
        Optional description of where this check is running (e.g. ``"Stage 1"``)
        included in raised error for diagnostics.

    Raises
    ------
    ExternalDocsError
        When missing modules are found or the external_docs block is malformed.
    """
    missing = check_imports_have_docs(diff, spec_body, language)

    # Also validate block content (AC-PARENT-9)
    block = _extract_external_docs_block(spec_body)
    content_errors: list[str] = []
    if block is not None:
        content_errors = _validate_external_docs_content(block)

    if missing or content_errors:
        prefix = f"[{context}] " if context else ""
        err = ExternalDocsError(missing, content_errors)
        raise ExternalDocsError(
            missing,
            [f"{prefix}{e}" for e in content_errors] if content_errors else [],
        )


# ---------------------------------------------------------------------------
# Override path with audit logging
# ---------------------------------------------------------------------------

def write_override_audit(
    agent_name: str,
    discussion: str,
    reason: str,
    missing_modules: list[str] | None = None,
) -> None:
    """Append an override record to audit.jsonl.

    Called when ALLOW_MISSING_EXTERNAL_DOCS=1 bypasses a Stage 3 refusal.

    Parameters
    ----------
    agent_name:
        Name/role of the spawned agent.
    discussion:
        Discussion number (string or int).
    reason:
        Human-readable reason from ALLOW_MISSING_EXTERNAL_DOCS_REASON env var.
    missing_modules:
        List of module names that lacked docs (may be empty/None if not known).
    """
    import json
    from datetime import datetime, timezone

    from backend import state_paths
    audit_path = state_paths.AUDIT_LOG

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "spec_external_docs",
        "action": "override",
        "actor": agent_name,
        "discussion": str(discussion),
        "reason": reason,
        "missing_modules": missing_modules or [],
        "override_env": "ALLOW_MISSING_EXTERNAL_DOCS=1",
    }
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        # Non-fatal — audit write must not block spawning.
        pass
