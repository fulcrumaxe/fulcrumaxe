"""
task_specs.py — YAML frontmatter helpers for Discussion bodies.

Provides:
  - _parse_frontmatter(body) -> dict
  - _parse_completion_summary(body) -> dict
  - format_frontmatter(...) -> str
  - format_completion_summary(...) -> str

These are used by registry.py (parsing) and by project-manager
(formatting). Keeping them here avoids circular imports.

Frontmatter format (placed immediately after the STATUS HTML comment):
    <!-- STATUS:DISCUSSING SINCE:... -->
    ---
    type: feature
    complexity_points: 3
    estimated_hours: 2.0
    depends_on: []
    tags: [workflow, orchestration]
    ---

Completion summary format (appended to Discussion body after merge):
    ## Completion Summary
    - actual_hours: 1.5
    - files_changed: 4
    - lines_added: 280
    - lines_removed: 15
    - pr_number: 123
    - merged_at: 2026-04-10T20:00:00Z
"""

from __future__ import annotations

import re
from typing import Any

try:
    import yaml  # type: ignore
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# Match the YAML block between --- delimiters that appear after the STATUS comment.
# The STATUS comment must precede the block (possibly with whitespace/newlines).
_FRONTMATTER_RE = re.compile(
    r"<!--\s*STATUS:[^>]*-->\s*\n---\n(.*?)\n---",
    re.DOTALL,
)

# Match a completion summary section (## Completion Summary followed by bullet lines).
_COMPLETION_RE = re.compile(
    r"##\s*Completion Summary\s*\n((?:\s*-\s*\w+:\s*[^\n]*\n?)+)",
    re.IGNORECASE,
)

# Match the <!-- COMPLETION --> ... <!-- /COMPLETION --> HTML-comment block written
# by post-merge-hook.  Lines inside are plain "key: value" (no leading dash).
_COMPLETION_BLOCK_RE = re.compile(
    r"<!--\s*COMPLETION\s*-->(.*?)<!--\s*/COMPLETION\s*-->",
    re.DOTALL,
)

# Parse individual key: value lines in the completion summary.
_KV_RE = re.compile(r"-\s*(\w+):\s*(.+)")

# Parse plain key: value lines (no leading dash) — used for the HTML-comment block.
_KV_PLAIN_RE = re.compile(r"^(\w+):\s*(.+)")


def _parse_frontmatter(body: str) -> dict:
    """
    Extract and parse YAML frontmatter from a Discussion body.

    Returns a dict with the parsed fields, or an empty dict if no valid
    frontmatter block is found or YAML is malformed.

    Expected fields (all optional): type, complexity_points, estimated_hours,
    depends_on, tags.
    """
    m = _FRONTMATTER_RE.search(body)
    if not m:
        return {}
    raw_yaml = m.group(1)
    if not raw_yaml.strip():
        return {}
    if _YAML_AVAILABLE:
        try:
            parsed = yaml.safe_load(raw_yaml)
        except yaml.YAMLError:
            return {}
    else:
        # Minimal fallback: parse simple key: value lines (no nested YAML).
        parsed = _minimal_yaml_parse(raw_yaml)
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _parse_completion_block(body: str) -> dict:
    """
    Extract completion data from a ``<!-- COMPLETION --> ... <!-- /COMPLETION -->``
    HTML-comment block written by post-merge-hook.sh.

    Lines inside the block are plain ``key: value`` (no leading dash):
        actual_hours: 4.2
        merged_at: 2026-05-11T14:30:00Z
        merged_pr: 588

    Returns a dict with parsed fields, or an empty dict if the block is absent.
    """
    m = _COMPLETION_BLOCK_RE.search(body)
    if not m:
        return {}
    block = m.group(1)
    result: dict[str, Any] = {}
    for line in block.splitlines():
        kv = _KV_PLAIN_RE.match(line.strip())
        if not kv:
            continue
        key, raw_val = kv.group(1).strip(), kv.group(2).strip()
        result[key] = _coerce_value(key, raw_val)
    return result


def _parse_completion_summary(body: str) -> dict:
    """
    Extract completion summary data from a Discussion body.

    Tries the ``<!-- COMPLETION -->`` HTML-comment block first (written by
    post-merge-hook.sh), then falls back to the older ``## Completion Summary``
    markdown section format.

    Returns a dict with parsed fields (actual_hours, files_changed, lines_added,
    lines_removed, pr_number / merged_pr, merged_at), or an empty dict if not present.
    """
    # Prefer the new HTML-comment block format (richer, machine-written)
    block_result = _parse_completion_block(body)
    if block_result:
        return block_result

    # Fall back to the older markdown section format
    m = _COMPLETION_RE.search(body)
    if not m:
        return {}
    block = m.group(1)
    result: dict[str, Any] = {}
    for line in block.splitlines():
        kv = _KV_RE.match(line.strip())
        if not kv:
            continue
        key, raw_val = kv.group(1).strip(), kv.group(2).strip()
        result[key] = _coerce_value(key, raw_val)
    return result


def _coerce_value(key: str, raw: str) -> Any:
    """Coerce a raw string value to an appropriate Python type based on field name."""
    int_fields = {"files_changed", "lines_added", "lines_removed", "pr_number", "merged_pr"}
    float_fields = {"actual_hours"}
    if key in int_fields:
        try:
            return int(raw)
        except ValueError:
            return raw
    if key in float_fields:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _minimal_yaml_parse(text: str) -> dict:
    """
    Ultra-minimal YAML parser for simple key: value and key: [list] lines.
    Used only when PyYAML is not installed.
    """
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val_raw = line.partition(":")
        key = key.strip()
        val_raw = val_raw.strip()
        if val_raw.startswith("[") and val_raw.endswith("]"):
            inner = val_raw[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [v.strip() for v in inner.split(",") if v.strip()]
        elif val_raw in ("true", "True"):
            result[key] = True
        elif val_raw in ("false", "False"):
            result[key] = False
        elif val_raw in ("null", "None", "~", ""):
            result[key] = None
        else:
            try:
                result[key] = int(val_raw)
            except ValueError:
                try:
                    result[key] = float(val_raw)
                except ValueError:
                    result[key] = val_raw
    return result


def format_frontmatter(
    type: str = "feature",
    complexity_points: int = 3,
    estimated_hours: float = 2.0,
    depends_on: list[int] | None = None,
    tags: list[str] | None = None,
) -> str:
    """
    Produce a YAML frontmatter block for project-manager to insert after
    the STATUS comment when creating or updating a Discussion.

    Example output:
        ---
        type: feature
        complexity_points: 3
        estimated_hours: 2.0
        depends_on: []
        tags: [workflow, orchestration]
        ---
    """
    if depends_on is None:
        depends_on = []
    if tags is None:
        tags = []

    depends_str = _format_list(depends_on)
    tags_str = _format_list(tags)

    return (
        "---\n"
        f"type: {type}\n"
        f"complexity_points: {complexity_points}\n"
        f"estimated_hours: {float(estimated_hours)}\n"
        f"depends_on: {depends_str}\n"
        f"tags: {tags_str}\n"
        "---"
    )


def format_completion_summary(
    actual_hours: float,
    files_changed: int,
    lines_added: int,
    lines_removed: int,
    pr_number: int,
    merged_at: str,
) -> str:
    """
    Produce a completion summary block to append to a
    Discussion body after a PR is merged.

    Example output:
        ## Completion Summary
        - actual_hours: 1.5
        - files_changed: 4
        - lines_added: 280
        - lines_removed: 15
        - pr_number: 123
        - merged_at: 2026-04-10T20:00:00Z
    """
    return (
        "## Completion Summary\n"
        f"- actual_hours: {float(actual_hours)}\n"
        f"- files_changed: {int(files_changed)}\n"
        f"- lines_added: {int(lines_added)}\n"
        f"- lines_removed: {int(lines_removed)}\n"
        f"- pr_number: {int(pr_number)}\n"
        f"- merged_at: {merged_at}\n"
    )


def _format_list(items: list) -> str:
    """Format a Python list as a compact YAML inline sequence."""
    if not items:
        return "[]"
    inner = ", ".join(str(i) for i in items)
    return f"[{inner}]"
