"""backend/loop_log_references.py — extract Discussion and PR references from loop-run logs.

Usage:
    from backend.loop_log_references import extract_references
    refs = extract_references(log_text)
    # {"discussions": [412, 835], "prs": [1141, 1162]}
"""
from __future__ import annotations

import re

_D_PATTERN = re.compile(r"\bD#(\d+)\b", re.IGNORECASE)
_PR_PATTERN = re.compile(r"\bPR\s*#(\d+)\b", re.IGNORECASE)

_CAP = 50  # max references of each kind to return


def extract_references(log_text: str) -> dict:
    """Extract D#N and PR #N references from loop log text.

    Returns a dict with:
        discussions: sorted, deduplicated list of Discussion numbers (int), capped at 50
        prs: sorted, deduplicated list of PR numbers (int), capped at 50
    """
    if not log_text:
        return {"discussions": [], "prs": []}

    discussions = sorted({int(m.group(1)) for m in _D_PATTERN.finditer(log_text)})[:_CAP]
    prs = sorted({int(m.group(1)) for m in _PR_PATTERN.finditer(log_text)})[:_CAP]

    return {"discussions": discussions, "prs": prs}
