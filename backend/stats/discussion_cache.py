"""backend/stats/discussion_cache.py — hit-ratio stats for the Discussion cache.

Reads from the counters table written by backend/discussion_cache.py.
Follows the module-per-feature pattern (one stat, one file).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def hit_ratio() -> dict:
    """Return cache hit_ratio plus raw hit/miss/total counts.

    Returns a dict with keys: hit_ratio (float 0-1), hits (int), misses (int), total (int).
    Returns zeroed dict when the cache db does not exist yet.
    """
    from backend.discussion_cache import get_stats  # noqa: PLC0415
    return get_stats()
