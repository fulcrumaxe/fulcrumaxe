#!/usr/bin/env python3
"""hooks/_retry_common.py

Shared helpers for bash-retry detection hooks.

Extracted from hooks/bash_retry_warn.py so that both the warn hook and the
circuit-breaker hook share the same normalization and parsing logic.
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------------------
# Normalization patterns
# ---------------------------------------------------------------------------

_CD_PREFIX = re.compile(r"^(?:cd\s+\S+\s*&&\s*)+")
_REDIRECT_SUFFIX = re.compile(r"\s+2>&1\b")
_PIPE_FILTER = re.compile(r"\s*\|\s*(?:head|tail|grep|wc|cut|awk|sed)(?:\s+[^|;]+)?")
_WHITESPACE = re.compile(r"\s+")
_QUOTE_NORMALIZE = re.compile(r"['\"]")


def normalize(cmd: str) -> str:
    """Strip cosmetic variations from a Bash command.

    Strips leading `cd /path &&` chains, trailing `2>&1`, pipe-to-filter
    suffixes, quotes, and collapses whitespace. Lowercases the result.
    """
    cmd = _CD_PREFIX.sub("", cmd)
    cmd = _REDIRECT_SUFFIX.sub("", cmd)
    cmd = _PIPE_FILTER.sub("", cmd)
    cmd = _QUOTE_NORMALIZE.sub("", cmd)
    cmd = _WHITESPACE.sub(" ", cmd).strip().lower()
    return cmd


def tokenize(cmd: str) -> set[str]:
    """Split a normalized command into a token set for Jaccard similarity."""
    return set(cmd.split())


def jaccard(a: set[str], b: set[str]) -> float:
    """Token-set Jaccard similarity. O(n). Returns 0.0 if both empty."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def command_stem(cmd: str) -> tuple[str, str]:
    """Return the first 2 tokens of a normalized command as (tok0, tok1).

    If fewer than 2 tokens exist, pads with empty strings.
    """
    parts = cmd.split()
    t0 = parts[0] if len(parts) > 0 else ""
    t1 = parts[1] if len(parts) > 1 else ""
    return t0, t1


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def parse_bash_history(transcript_tail: str) -> "list[tuple[str, bool]]":
    """Parse transcript tail; return list of (command, failed) tuples.

    'failed' means the tool_result had is_error=True.
    """
    lines = transcript_tail.splitlines()
    id_to_command: "dict[str, str]" = {}
    id_to_failed: "dict[str, bool]" = {}
    ordered_ids: "list[str]" = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type == "tool_use" and item.get("name") == "Bash":
                tid = item.get("id", "")
                cmd = item.get("input", {}).get("command", "")
                if tid and cmd:
                    id_to_command[tid] = cmd
                    if tid not in ordered_ids:
                        ordered_ids.append(tid)

            elif item_type == "tool_result":
                tid = item.get("tool_use_id", "")
                if tid in id_to_command:
                    id_to_failed[tid] = bool(item.get("is_error", False))

    return [(id_to_command[tid], id_to_failed.get(tid, False)) for tid in ordered_ids]
