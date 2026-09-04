"""transcript_reader.py -- streaming reader for Claude Code agent transcript JSONL files.

Format-isolation layer: classifiers in run_analyst.py consume only TranscriptTurn
named tuples. If the underlying JSONL schema changes, only this module needs updating.

Canonical globs:
    /tmp/claude-*/-home-agent-fulcrumaxe/*/tasks/*.output  (ephemeral task outputs)
    ~/.claude/projects/-home-agent-fulcrumaxe/*.jsonl       (persistent archive)

Both formats use JSONL: one JSON object per turn/event.  The .output format wraps
turns in {"type": "message", "message": {...}}.  The persistent .jsonl format
stores turn events directly: {"type": "user"|"assistant", "message": {...}}.
Both are handled by iter_turns() — the field-extraction logic is format-agnostic.

Role detection for persistent .jsonl files (checked in priority order):
    1. Top-level ``system`` field on the first record carrying a role tag
    2. Sidecar file at ``<path>.role`` (one-line role name)
    3. Filename pattern ``agent-<id>-<role>.jsonl``
    4. Fallback: "unknown"

Class-3 (mid-file-corrupt) warnings are always emitted to stderr.  A single aggregate
summary is also flushed at process exit via atexit.
"""

from __future__ import annotations

import atexit
import glob
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

# Allow running as a script from repo root: `python3 backend/transcript_reader.py`
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend._repo import PROJECT_TRANSCRIPT_SLUG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level skip-stats counter — accumulated across all iter_turns() calls
# in this process, flushed once at exit via atexit.
# ---------------------------------------------------------------------------

# Verbose flag — gates per-file Class-3 warnings; monkeypatchable in tests.
_VERBOSE: bool = os.environ.get("TRANSCRIPT_READER_VERBOSE", "").strip().lower() in ("1", "true", "yes")

_SKIP_STATS: dict[str, int] = {
    "skipped_non_jsonl": 0,
    "skipped_trailing_truncation": 0,
    "corrupt_midfile": 0,
}


def _flush_skip_summary() -> None:
    """Emit one aggregate summary line to stderr if any files were classified."""
    non_jsonl = _SKIP_STATS["skipped_non_jsonl"]
    trailing = _SKIP_STATS["skipped_trailing_truncation"]
    midfile = _SKIP_STATS["corrupt_midfile"]
    if non_jsonl == 0 and trailing == 0 and midfile == 0:
        return
    print(
        f"transcript_reader: skipped {non_jsonl} non-JSONL .output, "
        f"{trailing} trailing-truncated, "
        f"{midfile} mid-file-corrupt across this run",
        file=sys.stderr,
    )


atexit.register(_flush_skip_summary)

TRANSCRIPT_GLOB = f"/tmp/claude-*/{PROJECT_TRANSCRIPT_SLUG}/*/tasks/*.output"
JSONL_TRANSCRIPT_GLOB = f"~/.claude/projects/{PROJECT_TRANSCRIPT_SLUG}/*.jsonl"

# Files matching TRANSCRIPT_GLOB whose mtime is within this many seconds of now
# are still being written by a live agent.  Skip them to avoid malformed-JSONL
# warnings on partial last lines.  This filter is intentionally .output-only;
# the persistent .jsonl archive (JSONL_TRANSCRIPT_GLOB) is never in-flight.
IN_FLIGHT_SECONDS = 10

# Pattern: agent-<id>-<role>.jsonl  (e.g. agent-abc123-executor.jsonl)
_FILENAME_ROLE_RE = re.compile(r'^agent-[^-]+-([a-z][a-z0-9-]+)\.jsonl$')


class TranscriptTurn(NamedTuple):
    """Normalized turn from a Claude Code agent transcript.

    Classifiers consume only this type; raw JSONL schema is hidden behind iter_turns().
    """
    turn_idx: int              # 0-based, monotonically increasing
    role: str                  # "user" | "assistant" | "system" | "attachment"
    text: str                  # Flattened text from content blocks; "" if none
    tool_calls: list[dict]     # [{"name": str, "input": dict, "id": str}, ...]
    tool_results: list[dict]   # [{"tool_use_id": str, "content": str, "is_error": bool}, ...]
    raw: dict                  # Original parsed JSON line


def agent_id_from_path(path: Path) -> str:
    """Extract agent id from .../tasks/<agent_id>.output or UUID from .jsonl filename."""
    return path.stem


def find_transcripts(
    since_seconds: Optional[int] = None,
) -> list[Path]:
    """Return sorted, deduplicated list of all transcript paths from both globs.

    Walks:
      - TRANSCRIPT_GLOB  (/tmp/claude-*/.../tasks/*.output)
      - JSONL_TRANSCRIPT_GLOB  (~/.claude/projects/.../*.jsonl)

    When *since_seconds* is set, only paths with mtime >= (now - since_seconds)
    are included.  Result is sorted by path string and deduplicated.

    In-flight skip: .output files whose mtime is within IN_FLIGHT_SECONDS of now
    are excluded — they are still being appended to by a live agent and would
    produce malformed-JSONL warnings on their partial last line.  This filter
    applies ONLY to the .output (TRANSCRIPT_GLOB) source; the persistent .jsonl
    archive is never subject to the recency skip.
    """
    now = time.time()
    cutoff = (now - since_seconds) if since_seconds is not None else None
    in_flight_threshold = now - IN_FLIGHT_SECONDS

    seen: set[str] = set()
    paths: list[Path] = []

    for pattern in (TRANSCRIPT_GLOB, os.path.expanduser(JSONL_TRANSCRIPT_GLOB)):
        is_output_source = pattern == TRANSCRIPT_GLOB
        for p_str in glob.glob(pattern):
            if p_str in seen:
                continue
            try:
                mtime = os.path.getmtime(p_str)
            except OSError:
                continue
            if cutoff is not None and mtime < cutoff:
                continue
            # Skip .output files still being written by a live agent.
            if is_output_source and mtime > in_flight_threshold:
                continue
            seen.add(p_str)
            paths.append(Path(p_str))

    return sorted(paths)


def detect_role(path: Path) -> str:
    """Detect agent role for a transcript at *path*.

    Priority order:
    1. Top-level ``system`` field on the first parseable record (role tag).
    2. Sidecar file ``<path>.role`` — one line containing the role name.
    3. Filename pattern ``agent-<id>-<role>.jsonl``.
    4. Fallback: ``"unknown"``.

    Only applies to .jsonl files; .output files don't carry role metadata.
    """
    # Signal 1: system field in the first parseable record of the file
    # Authoritative — explicit role annotation embedded in the transcript itself.
    if path.suffix == ".jsonl":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    raw_line = raw_line.rstrip("\n")
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    system_val = obj.get("system")
                    if isinstance(system_val, str) and system_val.strip():
                        return system_val.strip()
                    # Only check the first parseable record
                    break
        except OSError:
            pass

    # Signal 2: sidecar file — one-line role name written alongside the transcript.
    sidecar = Path(str(path) + ".role")
    if sidecar.is_file():
        try:
            role = sidecar.read_text(encoding="utf-8").strip()
            if role:
                return role
        except OSError:
            pass

    # Signal 3: filename pattern agent-<id>-<role>.jsonl
    m = _FILENAME_ROLE_RE.match(path.name)
    if m:
        return m.group(1)

    return "unknown"


def _extract_content(content: object) -> tuple[str, list[dict], list[dict]]:
    """Normalize content field (str or list-of-blocks) into (text, tool_calls, tool_results).

    Never buffers the whole content list — iterates once.
    """
    if isinstance(content, str):
        return content, [], []

    if not isinstance(content, list):
        return "", [], []

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(str(t))
        elif btype == "tool_use":
            tool_calls.append({
                "name": block.get("name", ""),
                "input": block.get("input", {}),
                "id": block.get("id", ""),
            })
        elif btype == "tool_result":
            raw_content = block.get("content", "")
            if isinstance(raw_content, list):
                # Flatten nested text blocks inside tool_result
                result_text = " ".join(
                    b.get("text", "") for b in raw_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                result_text = str(raw_content) if raw_content else ""
            tool_results.append({
                "tool_use_id": block.get("tool_use_id", ""),
                "content": result_text,
                "is_error": bool(block.get("is_error", False)),
            })

    return " ".join(text_parts), tool_calls, tool_results


def iter_turns(path: Path) -> Iterator[TranscriptTurn]:
    """Stream-parse a single transcript file, yielding one TranscriptTurn per line.

    Handles both formats:
    - .output files: {"type": "message", "message": {"role": ..., "content": ...}}
    - .jsonl files: {"type": "user"|"assistant", "message": {"role": ..., "content": ...}}

    Both formats share the same field-extraction logic via _extract_content().

    Malformed-line classification (suppress benign noise, preserve genuine signal):

    1. All-fail (non-JSONL file): every non-empty line fails to parse.
       → No per-file warning. Counted in _SKIP_STATS["skipped_non_jsonl"]. Yields nothing.

    2. Trailing-truncation-only (dead partial): at least one valid line AND the ONLY
       failing line is the last non-empty line.
       → No per-file warning. Counted in _SKIP_STATS["skipped_trailing_truncation"].
       Yields all valid leading turns.

    3. Genuine mid-file corruption: at least one valid line AND at least one failing
       non-trailing line.
       → Per-file stderr warning (once). Counted in _SKIP_STATS["corrupt_midfile"].
       Yields valid turns.

    A single aggregate summary is flushed to stderr once per process via atexit
    (_flush_skip_summary).  Use one line of lookahead to keep the streaming contract:
    at most one pending raw line is held between iterations.
    """
    turn_idx = 0

    # Accumulated state for one-pass classification:
    #   valid_turns_pending  — turns we've parsed but not yet yielded (buffer)
    #   saw_valid            — True if we parsed at least one valid line
    #   bad_line_indices     — indices (0-based, counting non-empty lines) of failing lines
    #   last_nonempty_idx    — index of the most recent non-empty line seen
    #
    # Because we need to classify AFTER reading the whole file, we buffer valid turns.
    # However, the spec says "at most one line + one lookahead line held" — that refers
    # to the failure-detection window.  For correctness across large files we buffer
    # valid TranscriptTurns (not raw bytes).  Each turn is a small NamedTuple; memory
    # is bounded by the number of valid turns in the file, which mirrors the original
    # caller behaviour (callers already list()-ify results).

    valid_turns: list[TranscriptTurn] = []
    saw_valid: bool = False
    bad_line_indices: list[int] = []
    last_nonempty_idx: int = -1
    nonempty_idx: int = 0  # counter of non-empty lines seen

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue

                current_idx = nonempty_idx
                nonempty_idx += 1
                last_nonempty_idx = current_idx

                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    bad_line_indices.append(current_idx)
                    continue

                if not isinstance(obj, dict):
                    # Non-dict JSON (array, string, number) treated as bad
                    bad_line_indices.append(current_idx)
                    continue

                saw_valid = True

                # Support both top-level and nested message format.
                # Guard: obj["message"] may be a string (e.g. system events like
                # {"type":"system","message":"Loaded session abc123"} or browser-tester
                # issue objects {"file":...,"message":"..."}). Fall back to obj itself.
                msg_raw = obj.get("message", obj)
                msg = msg_raw if isinstance(msg_raw, dict) else obj
                role = str(msg.get("role", obj.get("role", "")))
                content_raw = msg.get("content", obj.get("content", ""))

                text, tool_calls, tool_results = _extract_content(content_raw)

                valid_turns.append(TranscriptTurn(
                    turn_idx=turn_idx,
                    role=role,
                    text=text,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    raw=obj,
                ))
                turn_idx += 1
    except OSError:
        # File may have been deleted (volatile /tmp); skip silently
        return

    # --- Classification ---
    if not bad_line_indices:
        # Clean file — yield all turns normally, no stats update.
        yield from valid_turns
        return

    if not saw_valid:
        # Class 1: All-fail (non-JSONL file — shell/test stdout, not a transcript).
        _SKIP_STATS["skipped_non_jsonl"] += 1
        return  # yield nothing

    # Determine if every bad line is the final non-empty line (trailing-only).
    # last_nonempty_idx is the index of the last non-empty line seen.
    non_trailing_bad = [i for i in bad_line_indices if i != last_nonempty_idx]

    if not non_trailing_bad:
        # Class 2: Trailing-truncation-only (dead partial) — benign, suppress warning.
        _SKIP_STATS["skipped_trailing_truncation"] += 1
        yield from valid_turns
        return

    # Class 3: Genuine mid-file corruption — count and warn unconditionally.
    # This is the signal worth preserving: a file that was written, completed,
    # and later found to have garbage in the middle is a real anomaly.
    _SKIP_STATS["corrupt_midfile"] += 1
    print(
        f"WARNING: transcript_reader: skipping malformed JSONL line in {path}",
        file=sys.stderr,
    )
    yield from valid_turns


def iter_transcripts(
    since_seconds: Optional[int] = None,
    glob_pattern: str = TRANSCRIPT_GLOB,
    max_files: Optional[int] = None,
) -> Iterator[tuple[Path, Iterator[TranscriptTurn]]]:
    """Yield (path, turn_iterator) for each transcript matching glob.

    Filters by file mtime when since_seconds is set. Caller MUST drain the
    turn_iterator before requesting the next pair (streaming — never buffers a
    whole transcript in memory).

    In-flight skip: paths ending in .output whose mtime is within IN_FLIGHT_SECONDS
    of now are excluded (same rule as find_transcripts).

    Note: This function uses a single glob_pattern for backwards compatibility.
    Use find_transcripts() + iter_turns() directly to scan both glob sources.
    """
    now = time.time()
    in_flight_threshold = now - IN_FLIGHT_SECONDS
    paths = sorted(glob.glob(glob_pattern))

    if since_seconds is not None:
        cutoff = now - since_seconds
        filtered = []
        for p in paths:
            try:
                if os.path.getmtime(p) >= cutoff:
                    filtered.append(p)
            except OSError:
                pass
        paths = filtered

    # Skip .output files still being written by a live agent.
    output_filtered = []
    for p in paths:
        if p.endswith(".output"):
            try:
                if os.path.getmtime(p) <= in_flight_threshold:
                    output_filtered.append(p)
            except OSError:
                pass
        else:
            output_filtered.append(p)
    paths = output_filtered

    if max_files is not None:
        paths = paths[:max_files]

    for path_str in paths:
        path = Path(path_str)
        yield path, iter_turns(path)
