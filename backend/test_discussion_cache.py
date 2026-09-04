"""
Tests for backend/discussion_cache.py repo resolution.

The old _load_repo() / _FALLBACK_REPO dead code was removed — repo resolution
is now handled entirely by backend._repo (imported as _REPO at module load time).
These tests verify that _REPO is a valid slug.

Run with:
    python -m pytest backend/test_discussion_cache.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.discussion_cache import _REPO  # noqa: E402


def test_repo_constant_is_string():
    """_REPO (from backend._repo) is a non-empty string with a slash."""
    assert isinstance(_REPO, str)
    assert "/" in _REPO


def test_repo_constant_non_empty():
    """_REPO is not empty."""
    assert _REPO
