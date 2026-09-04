"""backend/spec_file_list.py — extract declared file list from a Discussion Spec body.

Supports two discovery strategies (tried in order):

1. ``acceptance_files:`` YAML key in Spec frontmatter (opt-in convention):

   ```
   ---
   estimated_hours: 2
   acceptance_files: ["src/foo.py", "src/bar.py"]
   ---
   ```

2. Code-block file headers anywhere in the Spec body:

   ```ts src/foo.ts
   ```py src/bar.py
   ```bash scripts/baz.sh

3. If neither is present → returns empty list (caller should skip the check).

CLI (used by post-agent-hook.sh):
    python3 backend/spec_file_list.py <discussion_number>
    Prints one file path per line.  Exit 0 even when the list is empty.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Strategy 1 — acceptance_files YAML key in any --- frontmatter block
# ---------------------------------------------------------------------------

# Matches the first YAML frontmatter block in the text (--- ... ---).
# We only need the content between the fences.
_FRONTMATTER_RE = re.compile(
    r"(?:^|\n)---\s*\n(.*?)\n---", re.DOTALL
)

# Matches:  acceptance_files: ["a.py", "b.py"]
# or:       acceptance_files:\n  - a.py\n  - b.py
_AF_INLINE_RE = re.compile(
    r"^\s*acceptance_files\s*:\s*\[([^\]]*)\]", re.MULTILINE
)
_AF_LIST_ITEM_RE = re.compile(r"[\"\']?([^\"\',\[\]\s]+)[\"\']?")

_AF_BLOCK_HEADER_RE = re.compile(
    r"^\s*acceptance_files\s*:\s*$", re.MULTILINE
)
_AF_BLOCK_ITEM_RE = re.compile(r"^\s+-\s+['\"]?([^'\"\s]+)['\"]?", re.MULTILINE)


def _parse_frontmatter(body: str) -> List[str]:
    """Return acceptance_files list from the first YAML frontmatter block, or []."""
    match = _FRONTMATTER_RE.search(body)
    if not match:
        return []
    fm = match.group(1)

    # Inline array:  acceptance_files: ["a.py", "b.py"]
    inline = _AF_INLINE_RE.search(fm)
    if inline:
        raw = inline.group(1)
        return [m.group(1) for m in _AF_LIST_ITEM_RE.finditer(raw) if m.group(1)]

    # Block list:
    #   acceptance_files:
    #     - a.py
    #     - b.py
    header = _AF_BLOCK_HEADER_RE.search(fm)
    if header:
        # Collect subsequent list items
        after = fm[header.end():]
        items = []
        for line in after.splitlines():
            item_match = re.match(r"^\s+-\s+['\"]?([^'\"\s]+)['\"]?", line)
            if item_match:
                items.append(item_match.group(1))
            elif line.strip() and not line.lstrip().startswith("-"):
                # End of the block (another key started)
                break
        if items:
            return items

    return []


# ---------------------------------------------------------------------------
# Strategy 2 — code-block file headers
# ---------------------------------------------------------------------------

# Matches: ```<lang> path/to/file.ext  (with optional whitespace around path)
# Languages to recognise (extensible).
_CODE_BLOCK_FILE_RE = re.compile(
    r"^```(?:ts|tsx|js|jsx|py|python|sh|bash|rs|go|rb|java|cpp|c|yaml|yml|json|toml|md|txt)"
    r"\s+(\S+)\s*$",
    re.MULTILINE,
)


def _parse_code_blocks(body: str) -> List[str]:
    """Return file paths from ```<lang> path markers in body, or []."""
    return _CODE_BLOCK_FILE_RE.findall(body)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_file_list(spec_body: str) -> List[str]:
    """Return the declared file list from a Spec body.

    Tries acceptance_files frontmatter first, then code-block headers.
    Returns [] if neither is present (caller should skip the scope check).
    """
    files = _parse_frontmatter(spec_body)
    if files:
        return files
    return _parse_code_blocks(spec_body)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli(discussion_number: str) -> None:
    """Fetch Discussion body and print file list, one per line."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from backend.discussion_cache import get_body  # type: ignore[import]
    except ImportError as exc:
        print(f"[spec_file_list] import error: {exc}", file=sys.stderr)
        sys.exit(1)

    # fresh=True: this is the reader that actually drives the scope-drift PR
    # comment, so a stale cache row here silently computes the declared file
    # list against the previous revision of the Spec (D#1778 Blocking Issue 1).
    # Called once per finished executor PR (scope-drift-check.sh) — infrequent
    # enough that the extra live fetch is not worth threading a --fresh flag
    # through the CLI for.
    body = get_body(int(discussion_number), fresh=True)
    if not body:
        # No body or fetch failed — print nothing (empty file list → skip check)
        sys.exit(0)

    for path in extract_file_list(body):
        print(path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 backend/spec_file_list.py <discussion_number>", file=sys.stderr)
        sys.exit(1)
    _cli(sys.argv[1])
