#!/usr/bin/env python3
"""
cross-file-detector.py — pure diff → findings converter.

Usage:
    python3 scripts/lib/cross-file-detector.py --pr <N>
    python3 scripts/lib/cross-file-detector.py --diff <file.diff>

Reads a unified diff (from gh pr diff or a file), extracts modified symbol
names, then ripgreps the repo for each symbol in files other than the one
that was modified.  When a sibling file contains the same symbol name but
with different content around it, emits a finding.

Output: JSON array to stdout, one finding per sibling match.

Each finding:
  {
    "symbol": str,
    "primary_file": str,
    "sibling_files": [str, ...],
    "signature_hash": str,
    "snippet_primary": str,   # ≤200 chars
    "snippet_sibling": str,   # ≤200 chars
    "sibling_file": str       # convenience: first sibling
  }

No side effects.  No network calls.  No GitHub API calls.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

SNIPPET_MAX = 200
SYMBOL_CAP = 50     # max symbols to scan per invocation
# PARALLEL_WORKERS is intentionally absent: detect() scans symbols sequentially.
# Parallel ripgrep dispatch (ThreadPoolExecutor, nproc/2) is deferred to v2 —
# the sequential path is fast enough for SYMBOL_CAP=50 on CI machines.
DISMISSED_CACHE = ".autonomous-team/cross-file-dismissed.jsonl"
DISMISSED_WINDOW_DAYS = 7

# Files matching these globs are NEVER quoted in output
PATH_DENYLIST = [
    re.compile(r'(^|/)\.env'),
    re.compile(r'(^|/)secrets/'),
    re.compile(r'(^|/)hooks/sandbox'),
    re.compile(r'(^|/)settings'),
    re.compile(r'\.pem$'),
    re.compile(r'\.key$'),
]

# Language-agnostic symbol declaration patterns.
# Captures: function / def / class / struct / const / interface / type declarations.
DECL_PATTERNS = [
    # Python: def foo, async def foo, class Foo
    re.compile(r'^\+\s*(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    re.compile(r'^\+\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    # JavaScript/TypeScript: function foo, async function foo, const foo =, export function foo
    re.compile(r'^\+\s*(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    re.compile(r'^\+\s*(?:export\s+)?const\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*[=:]'),
    re.compile(r'^\+\s*(?:export\s+)?(?:interface|type|class|struct|enum)\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    # Go: func Foo / func (r *T) Foo
    re.compile(r'^\+\s*func\s+(?:\([^)]+\)\s+)?([a-zA-Z_][a-zA-Z0-9_]*)'),
    # Rust: fn foo, struct Foo, const FOO, enum Foo, impl Foo
    re.compile(r'^\+\s*(?:pub\s+)?(?:async\s+)?fn\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    re.compile(r'^\+\s*(?:pub\s+)?(?:struct|enum|trait|impl|const|static)\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    # Shell: foo() or function foo
    re.compile(r'^\+\s*function\s+([a-zA-Z_][a-zA-Z0-9_]*)'),
    re.compile(r'^\+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\)\s*\{'),
]


def is_denied_path(path: str) -> bool:
    for pat in PATH_DENYLIST:
        if pat.search(path):
            return True
    return False


# LLM control tokens that must be stripped from snippets before embedding in
# Discussion bodies.  A malicious commit can plant these in source files to
# hijack agent context windows that read the Discussion.
_CONTROL_TOKEN_PATTERNS = re.compile(
    r'</?system>|'
    r'<\|im_(start|end)\|>|'
    r'\[/?(role)\]|'
    r'<!--.*?-->|'
    r'<!--',
    re.IGNORECASE | re.DOTALL,
)


def cap_snippet(text: str) -> str:
    text = text.strip()
    # Strip LLM control tokens before embedding in any Discussion body.
    text = _CONTROL_TOKEN_PATTERNS.sub("[REDACTED]", text)
    if len(text) > SNIPPET_MAX:
        return text[:SNIPPET_MAX - 3] + "..."
    return text


def signature_hash(line: str) -> str:
    """Normalize a declaration line and hash it — strips leading/trailing whitespace."""
    normalized = " ".join(line.strip().split())
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


def extract_symbols_from_diff(diff_text: str) -> dict[str, str]:
    """
    Parse unified diff; return {symbol_name: primary_file_path}.
    Processes only added/changed lines (+) in each hunk.
    Caps at SYMBOL_CAP total symbols.
    """
    symbols: dict[str, str] = {}
    current_file = None

    for line in diff_text.splitlines():
        # Track which file we're in
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        if current_file is None:
            continue
        if is_denied_path(current_file):
            continue

        # Only look at added/changed lines
        if not line.startswith("+"):
            continue

        for pat in DECL_PATTERNS:
            m = pat.match(line)
            if m:
                name = m.group(1)
                # Skip very short names — too noisy
                if len(name) < 3:
                    continue
                if name not in symbols:
                    symbols[name] = current_file
                if len(symbols) >= SYMBOL_CAP:
                    return symbols
                break

    return symbols


def load_dismissed_pairs(repo_root: str) -> set[tuple[str, str, str]]:
    """
    Load dismissed pairs from the cache file.
    Returns set of (symbol, primary_file, sibling_file) tuples active within 7 days.
    """
    cache_path = os.path.join(repo_root, DISMISSED_CACHE)
    if not os.path.exists(cache_path):
        return set()

    cutoff = time.time() - DISMISSED_WINDOW_DAYS * 86400
    pairs: set[tuple[str, str, str]] = set()
    valid_lines = []

    try:
        with open(cache_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    dismissed_at = entry.get("dismissed_at", 0)
                    if isinstance(dismissed_at, str):
                        # ISO timestamp — convert
                        import datetime
                        dt = datetime.datetime.fromisoformat(dismissed_at.replace("Z", "+00:00"))
                        dismissed_at = dt.timestamp()
                    if dismissed_at >= cutoff:
                        valid_lines.append(line)
                        pairs.add((
                            entry.get("symbol", ""),
                            entry.get("primary_file", ""),
                            entry.get("sibling_file", ""),
                        ))
                except (json.JSONDecodeError, KeyError):
                    pass

        # Prune stale entries — atomic rename so concurrent appenders don't lose entries.
        if len(valid_lines) < sum(1 for _ in open(cache_path) if _.strip()):
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w") as f:
                for vl in valid_lines:
                    f.write(vl + "\n")
            os.replace(tmp_path, cache_path)
    except OSError:
        pass

    return pairs


def _python_grep_files(symbol: str, repo_root: str) -> list[str]:
    """
    Pure-Python fallback: walk repo_root and return relative paths of files
    containing a whole-word match for symbol.
    Skips binary files, node_modules, .git, __pycache__.
    """
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    results = []
    skip_dirs = {".git", "node_modules", "__pycache__", ".tox", "dist", "build", ".venv", ".claude", "archive"}

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, errors="replace") as f:
                    # Read in chunks to avoid loading huge files
                    chunk = f.read(1_000_000)
                    if pattern.search(chunk):
                        results.append(fpath)
            except OSError:
                pass
    return results


def ripgrep_symbol(symbol: str, primary_file: str, repo_root: str) -> list[str]:
    """
    Search repo_root for files containing the symbol as a whole word.
    Tries rg first; falls back to Python search if rg is not available.
    Returns relative paths excluding primary_file and denied paths.
    """
    # Try rg via shell (works in bash context where rg may be a shell function)
    rg_args = ["rg", "--files-with-matches", "-w", symbol, repo_root]
    try:
        result = subprocess.run(
            rg_args,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode in (0, 1):  # 0=match found, 1=no match
            raw_files = result.stdout.splitlines()
        else:
            raise FileNotFoundError("rg returned unexpected exit code")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # rg not available as subprocess — use Python fallback
        raw_files = _python_grep_files(symbol, repo_root)

    files = []
    for line in raw_files:
        rel = os.path.relpath(line.strip(), repo_root)
        if rel == primary_file:
            continue
        if is_denied_path(rel):
            continue
        files.append(rel)
    return files


def extract_snippet(file_path: str, symbol: str, repo_root: str) -> tuple[str, str]:
    """
    Extract the declaration line containing symbol from file_path,
    plus up to 3 following lines for context.
    Returns (combined_snippet, signature_hash_of_decl_line).
    """
    full_path = os.path.join(repo_root, file_path)
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    try:
        with open(full_path, errors="replace") as f:
            lines = f.readlines()
        for idx, line in enumerate(lines):
            if pattern.search(line):
                # Grab declaration line + 3 context lines
                context = lines[idx:idx + 4]
                combined = "".join(context)
                return cap_snippet(combined), signature_hash(line)
    except OSError:
        pass
    return "", ""


def extract_primary_snippet(diff_text: str, symbol: str, primary_file: str) -> tuple[str, str]:
    """
    Extract added lines containing the symbol from the diff (up to 4 lines of context).
    Returns (snippet, signature_hash).
    """
    in_file = False
    diff_lines = diff_text.splitlines()
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')

    for idx, line in enumerate(diff_lines):
        if line.startswith("+++ b/"):
            in_file = (line[6:].strip() == primary_file)
            continue
        if not in_file:
            continue
        if not line.startswith("+"):
            continue
        if pattern.search(line):
            content = line[1:]  # strip leading +
            # Gather up to 4 following added/context lines for comparison
            context_lines = [content]
            for nxt in diff_lines[idx + 1:idx + 4]:
                if nxt.startswith("+"):
                    context_lines.append(nxt[1:])
                elif not nxt.startswith("-") and not nxt.startswith("@@"):
                    context_lines.append(nxt)
            combined = "\n".join(context_lines)
            return cap_snippet(combined), signature_hash(content)
    return "", ""


def detect(diff_text: str, repo_root: str) -> list[dict]:
    """
    Core detection function.
    Input:  unified diff text + repo root path.
    Output: list of finding dicts (may be empty).
    """
    symbols = extract_symbols_from_diff(diff_text)
    if not symbols:
        return []

    dismissed = load_dismissed_pairs(repo_root)
    findings = []

    for symbol, primary_file in symbols.items():
        sibling_files = ripgrep_symbol(symbol, primary_file, repo_root)
        if not sibling_files:
            continue

        snippet_primary, sig_primary = extract_primary_snippet(diff_text, symbol, primary_file)

        confirmed_siblings = []
        first_sibling_snippet = ""
        for sib in sibling_files:
            if (symbol, primary_file, sib) in dismissed:
                continue
            snippet_sib, sig_sib = extract_snippet(sib, symbol, repo_root)
            # Flag: sibling has same name but different signature hash
            # OR has the symbol but content differs from what was just changed
            if snippet_sib and snippet_sib != snippet_primary:
                confirmed_siblings.append(sib)
                if not first_sibling_snippet:
                    first_sibling_snippet = snippet_sib

        if confirmed_siblings:
            findings.append({
                "symbol": symbol,
                "primary_file": primary_file,
                "sibling_file": confirmed_siblings[0],
                "sibling_files": confirmed_siblings,
                "signature_hash": sig_primary,
                "snippet_primary": snippet_primary,
                "snippet_sibling": first_sibling_snippet,
            })

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect cross-file pattern deviations from a PR diff."
    )
    parser.add_argument("--pr", type=int, help="PR number (fetches diff via gh pr diff)")
    parser.add_argument("--diff", type=str, help="Path to a unified diff file")
    parser.add_argument("--repo-root", type=str, default=".",
                        help="Repo root directory (default: current dir)")
    parser.add_argument("--repo", type=str, default=None,
                        help="owner/name slug for 'gh pr diff --repo' "
                        "(falls back to AUTONOMOUS_TEAM_REPO env var)")
    args = parser.parse_args()

    if args.pr and args.diff:
        print("Error: provide --pr OR --diff, not both", file=sys.stderr)
        return 1

    if args.pr:
        _repo = args.repo or os.environ.get("AUTONOMOUS_TEAM_REPO")
        if not _repo:
            print(
                "Error: --pr requires --repo or AUTONOMOUS_TEAM_REPO to be set "
                "(no hard-coded fallback — see D#1870)",
                file=sys.stderr,
            )
            return 1
        result = subprocess.run(
            ["gh", "pr", "diff", str(args.pr),
             "--repo", _repo],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error fetching diff for PR #{args.pr}: {result.stderr}", file=sys.stderr)
            return 1
        diff_text = result.stdout
    elif args.diff:
        try:
            with open(args.diff) as f:
                diff_text = f.read()
        except OSError as e:
            print(f"Error reading diff file: {e}", file=sys.stderr)
            return 1
    else:
        diff_text = sys.stdin.read()

    repo_root = os.path.abspath(args.repo_root)
    findings = detect(diff_text, repo_root)
    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
