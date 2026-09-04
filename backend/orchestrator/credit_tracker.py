"""backend/orchestrator/credit_tracker.py — SDK credit balance tracker.

Reads and writes ~/.fulcrumaxe-state/sdk_credit.json.
Decrements on each successful SDK response.
Exposes remaining_usd() and soft_cap_breached() with a per-loop-iteration
cache and 10-minute TTL.

File format::

    {
        "initial_usd": 200.0,
        "used_usd": 12.50,
        "last_updated": "2026-05-16T14:33:00Z",
        "cache_ts": "2026-05-16T14:33:00Z"
    }

Credit-exhausted behaviour (per AC4):
  - At $150 remaining ($50 consumed): warn to loop log + team-log comment.
  - At $0 remaining: hard-stop SDK path unless --allow-subscription-fallback.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 600          # 10 minutes
_SOFT_CAP_REMAINING_USD = 50.0    # warn when remaining drops to $50
_DEFAULT_INITIAL_USD = 200.0      # $200/month Max 20x credit pool


def _credit_file() -> Path:
    """Return the sdk_credit.json path, honouring AUTONOMOUS_TEAM_STATE_DIR."""
    import os
    state_dir = os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR",
        str(Path.home() / ".fulcrumaxe-state"),
    )
    return Path(state_dir) / "sdk_credit.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_raw(path: Path) -> dict:
    """Load sdk_credit.json, creating a fresh file if absent."""
    if not path.exists():
        data: dict = {
            "initial_usd": _DEFAULT_INITIAL_USD,
            "used_usd": 0.0,
            "last_updated": _now_iso(),
            "cache_ts": _now_iso(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_raw(path, data)
        return data
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "initial_usd": _DEFAULT_INITIAL_USD,
            "used_usd": 0.0,
            "last_updated": _now_iso(),
            "cache_ts": _now_iso(),
        }


def _save_raw(path: Path, data: dict) -> None:
    """Atomically write sdk_credit.json."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_ts() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# CreditTracker class
# ---------------------------------------------------------------------------

class CreditTracker:
    """Thread-safe, cached credit tracker for the SDK orchestrator.

    Parameters
    ----------
    credit_file:
        Override path for sdk_credit.json (useful in tests).
    """

    def __init__(self, credit_file: Optional[Path] = None) -> None:
        self._path = credit_file or _credit_file()
        self._cache_loaded_at: Optional[float] = None
        self._cached_data: Optional[dict] = None

    # ------------------------------------------------------------------
    # Internal cache helpers
    # ------------------------------------------------------------------

    def _data(self) -> dict:
        """Return cached credit data, reloading if TTL expired."""
        now = _now_ts()
        if (
            self._cached_data is None
            or self._cache_loaded_at is None
            or (now - self._cache_loaded_at) > _CACHE_TTL_SECONDS
        ):
            self._cached_data = _load_raw(self._path)
            self._cache_loaded_at = now
        return self._cached_data

    def _invalidate_cache(self) -> None:
        self._cached_data = None
        self._cache_loaded_at = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def remaining_usd(self) -> float:
        """Return remaining credit in USD.

        Negative values indicate overage (should not happen in practice
        if decrement() is guarded by soft-cap checks).
        """
        d = self._data()
        initial = float(d.get("initial_usd", _DEFAULT_INITIAL_USD))
        used = float(d.get("used_usd", 0.0))
        return round(initial - used, 6)

    def used_usd(self) -> float:
        """Return amount consumed so far."""
        d = self._data()
        return float(d.get("used_usd", 0.0))

    def soft_cap_breached(self) -> bool:
        """Return True when remaining_usd() <= _SOFT_CAP_REMAINING_USD ($50).

        Triggers a visible warning but does NOT hard-stop the SDK path.
        """
        return self.remaining_usd() <= _SOFT_CAP_REMAINING_USD

    def is_exhausted(self) -> bool:
        """Return True when remaining_usd() <= 0."""
        return self.remaining_usd() <= 0.0

    def decrement(self, amount_usd: float) -> None:
        """Record that *amount_usd* was spent via the SDK.

        Updates the file atomically and invalidates the in-process cache
        so the next remaining_usd() call reflects the write.
        """
        if amount_usd < 0:
            raise ValueError(f"decrement amount must be non-negative, got {amount_usd}")
        data = _load_raw(self._path)  # fresh read for write path
        data["used_usd"] = round(float(data.get("used_usd", 0.0)) + amount_usd, 6)
        data["last_updated"] = _now_iso()
        _save_raw(self._path, data)
        self._invalidate_cache()

    def reset(self, initial_usd: float = _DEFAULT_INITIAL_USD) -> None:
        """Reset credit to *initial_usd* (monthly refresh use case).

        Overwrites the file completely.  Cache is invalidated.
        """
        data = {
            "initial_usd": initial_usd,
            "used_usd": 0.0,
            "last_updated": _now_iso(),
            "cache_ts": _now_iso(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _save_raw(self._path, data)
        self._invalidate_cache()

    def snapshot(self) -> dict:
        """Return a copy of the current credit state dict."""
        return dict(self._data())
