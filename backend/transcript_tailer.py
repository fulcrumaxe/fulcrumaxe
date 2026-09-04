"""backend/transcript_tailer.py — Shared transcript-tailing primitive (Discussion #835 PR-a).

Provides:
  tail_transcript(path, on_line, max_lines, scrub) -> None
    Read a JSONL transcript file from the beginning (or a given byte offset),
    emit each complete line via on_line(), buffer partial lines, scrub secrets,
    enforce a bounded drop-oldest queue.

Secret scrubbing strips the following BEFORE any emission:
  - $GH_TOKEN / GH_TOKEN=<value>
  - $ANTHROPIC_API_KEY / ANTHROPIC_API_KEY=<value>
  - Authorization: <value>
  - .env-style key=value pairs that look like secrets (long hex/base64 tokens)
  - JSON/YAML key+value pairs: "GH_TOKEN": "ghp_..." or GH_TOKEN: ghp_...
  - URL credentials: https://user:token@host/...
  - Standalone token prefixes: ghp_, sk-ant-, ghs_, gho_, xoxp-, AKIA...

Usage (daemon integration):
  from transcript_tailer import tail_transcript, scrub_secrets

Usage (CLI):
  python3 backend/transcript_tailer.py <path> [--max-lines N]
"""

from __future__ import annotations

import collections
import re
import threading
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

# Pattern order matters: more-specific patterns first.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # URL credentials: https://user:token@host/... — strip user:token portion
    # Replaces everything between :// and @ with <scrubbed>
    (re.compile(r"(https?://)([^/\s]*:[^@/\s]+)@", re.IGNORECASE),
     r"\1<scrubbed>@"),

    # Standalone token prefix patterns — match by shape regardless of key name.
    # GitHub tokens: ghp_ (personal), ghs_ (server-to-server), gho_ (OAuth), ghx_ (fine-grained)
    # Must be followed by underscore + alphanum (at least 10 chars) to avoid
    # false positives on things like "ghp_something" without a real token shape.
    (re.compile(r"\bghp_[A-Za-z0-9]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bghs_[A-Za-z0-9]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bgho_[A-Za-z0-9]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bghx_[A-Za-z0-9]{10,}\b"), "[REDACTED]"),
    # Anthropic API keys: sk-ant-api03-... or sk-ant-...
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}\b"), "[REDACTED]"),
    # Slack tokens: xoxp-, xoxb-, xoxe-, xoxa-
    (re.compile(r"\bxox[pbeoa]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED]"),
    # AWS access key IDs: AKIA... (20-char uppercase alphanumeric)
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[REDACTED]"),

    # JSON/YAML key+value secret patterns — named key followed by quoted or unquoted value.
    # JSON form:  "GH_TOKEN": "ghp_..."   or   "GH_TOKEN": "any-long-value"
    # YAML form:  GH_TOKEN: ghp_...       or   GH_TOKEN: any-long-value
    # Matches both quoted and unquoted values.
    (re.compile(
        r"""(?x)
        (?:
            "(?:GH_TOKEN|GITHUB_TOKEN|ANTHROPIC_API_KEY|API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)"
            \s*:\s*
            (?:"([^"]{4,})"|'([^']{4,})'|([A-Za-z0-9+/._\-]{8,}={0,2}))
        |
            \b(?:GH_TOKEN|GITHUB_TOKEN|ANTHROPIC_API_KEY|API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)\b
            \s*:\s+
            (?:"([^"]{4,})"|'([^']{4,})'|([A-Za-z0-9+/._\-]{8,}={0,2}))
        )
        """,
        re.IGNORECASE,
    ), "[REDACTED_KV]"),

    # GH_TOKEN env-style assignment: GH_TOKEN=ghp_abc123...
    (re.compile(r"GH_TOKEN\s*=\s*\S+", re.IGNORECASE), "GH_TOKEN=[REDACTED]"),
    # ANTHROPIC_API_KEY assignment
    (re.compile(r"ANTHROPIC_API_KEY\s*=\s*\S+", re.IGNORECASE), "ANTHROPIC_API_KEY=[REDACTED]"),
    # $GH_TOKEN variable reference (shell interpolated value appearing in output)
    # Matches dollar-sign followed by the var name and an optional token-looking value
    (re.compile(r"\$GH_TOKEN(?:=[^\s;|&\"']*)?", re.IGNORECASE), "$GH_TOKEN=[REDACTED]"),
    (re.compile(r"\$ANTHROPIC_API_KEY(?:=[^\s;|&\"']*)?", re.IGNORECASE), "$ANTHROPIC_API_KEY=[REDACTED]"),
    # Authorization header: "Authorization: Bearer <token>" or "Authorization: <token>"
    (re.compile(r"(Authorization\s*:\s*(?:Bearer\s+)?)\S{8,}", re.IGNORECASE),
     r"\1[REDACTED]"),
    # .env-style: KEY=<long hex/base64 token 16+ chars, not a common word)>
    # Matches UPPER_SNAKE=<value> where value looks like a token (16+ alphanum chars with
    # optional dashes/dots, NO spaces).  Avoids false-positives on short values like PORT=8080
    # and filesystem paths like REPO_ROOT=/some/absolute/path (values starting with / are paths).
    (re.compile(r"([A-Z][A-Z0-9_]{3,})\s*=\s*([A-Za-z0-9+._\-][A-Za-z0-9+/._\-]{15,}={0,2})", re.MULTILINE),
     r"\1=[REDACTED]"),
]


def scrub_secrets(line: str) -> str:
    """Return line with any detected secret values replaced by [REDACTED]."""
    for pattern, replacement in _SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


# ---------------------------------------------------------------------------
# Bounded queue (drop-oldest)
# ---------------------------------------------------------------------------

class _BoundedQueue:
    """Thread-safe FIFO queue that drops the oldest item when at capacity."""

    def __init__(self, maxlen: int) -> None:
        self._q: collections.deque[str] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def put(self, item: str) -> None:
        with self._lock:
            # deque with maxlen automatically drops from the left (oldest)
            self._q.append(item)

    def drain(self) -> list[str]:
        with self._lock:
            items = list(self._q)
            self._q.clear()
        return items

    def __len__(self) -> int:
        return len(self._q)


# ---------------------------------------------------------------------------
# Core tail function
# ---------------------------------------------------------------------------

def tail_transcript(
    path: str,
    on_line: Callable[[str], None],
    *,
    max_lines: int = 1000,
    scrub: bool = True,
    byte_offset: int = 0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Tail a transcript file and call on_line() for each complete line.

    Args:
        path: Path to the JSONL transcript file.
        on_line: Callback receiving one scrubbed (if scrub=True) line at a time.
                 Called synchronously; keep it fast.
        max_lines: Bounded queue capacity — when the queue is full, the oldest
                   un-emitted line is dropped. Prevents unbounded memory growth
                   under backpressure.
        scrub: If True, strip secrets before passing to on_line.
        byte_offset: Start reading from this byte position (for incremental tailing).
        stop_event: Optional threading.Event; when set, tail_transcript returns.

    Performance contract: p99 per-line overhead <5ms (scrub + queue + callback).
    """
    queue = _BoundedQueue(maxlen=max_lines)
    partial: str = ""

    file_path = Path(path)
    try:
        fh = file_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return  # file does not exist or unreadable — silent return

    try:
        if byte_offset > 0:
            fh.seek(byte_offset)

        while True:
            if stop_event is not None and stop_event.is_set():
                break

            chunk = fh.read(8192)
            if not chunk:
                break  # EOF — no follow mode; caller loops externally if needed

            partial += chunk
            # Split on newlines; last element is a partial line (or empty)
            lines = partial.split("\n")
            partial = lines[-1]  # keep the trailing fragment

            for raw_line in lines[:-1]:
                if not raw_line.strip():
                    continue
                if scrub:
                    raw_line = scrub_secrets(raw_line)
                queue.put(raw_line)

        # Drain queue and emit
        for line in queue.drain():
            on_line(line)

    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Spawn discovery
# ---------------------------------------------------------------------------

def _project_transcript_slug() -> str:
    """Claude Code encodes a project's absolute repo path as a directory slug
    under ~/.claude/projects/ by replacing every "/" with "-"
    (e.g. "/srv/checkouts/myrepo" -> "-srv-checkouts-myrepo").
    Computed from this file's actual location so it's correct for any clone.
    """
    repo_root = Path(__file__).resolve().parent.parent
    return str(repo_root).replace("/", "-")


def _subagent_glob_pattern() -> str:
    return str(
        Path.home() / ".claude" / "projects" / _project_transcript_slug()
        / "*" / "subagents" / "agent-*.jsonl"
    )


_MAX_ACTIVE_SPAWNS = 20


def discover_active_spawns(max_spawns: int = _MAX_ACTIVE_SPAWNS) -> list[str]:
    """Return up to max_spawns most-recently-modified JSONL transcript paths.

    Glob pattern:
      ~/.claude/projects/<slug-of-repo-root>/*/subagents/agent-*.jsonl

    Returns at most max_spawns paths, sorted newest-first by modification time.
    """
    import glob
    import os

    paths = glob.glob(_subagent_glob_pattern(), recursive=False)
    if not paths:
        return []

    # Sort by mtime descending (newest first)
    def _mtime(p: str) -> float:
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0.0

    paths.sort(key=_mtime, reverse=True)
    return paths[:max_spawns]


# ---------------------------------------------------------------------------
# Agent label extraction
# ---------------------------------------------------------------------------

def agent_label_from_path(path: str) -> str:
    """Extract a short label from a transcript path for display in CLI output.

    For subagent paths like:
      .../subagents/agent-ab15f3a6007edf168.jsonl
    returns "agent-ab15" (first 14 chars of the stem).
    """
    stem = Path(path).stem  # e.g. "agent-ab15f3a6007edf168"
    return stem[:14]


# ---------------------------------------------------------------------------
# CLI entry point (for direct testing / smoke test)
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Tail a single transcript file to stdout."
    )
    parser.add_argument("path", help="Path to JSONL transcript file")
    parser.add_argument("--max-lines", type=int, default=1000)
    parser.add_argument("--no-scrub", action="store_true", default=False)
    args = parser.parse_args()

    def emit(line: str) -> None:
        print(line, flush=True)

    tail_transcript(
        args.path,
        emit,
        max_lines=args.max_lines,
        scrub=not args.no_scrub,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
