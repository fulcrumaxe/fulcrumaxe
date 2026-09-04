"""
backend/researcher.py — TTL cache for researcher agent WebFetch results.

Keyed by URL (MD5 hash for filename safety), stored in the runtime state dir.
TTL: 15 minutes. CLI: python3 backend/researcher.py get <url> / set <url> <body>

Cache file: ~/.fulcrumaxe-state/researcher-cache.json
(or $AUTONOMOUS_TEAM_STATE_DIR/researcher-cache.json)
"""

import json
import hashlib
import sys
import time
from pathlib import Path

# Allow running as `python3 backend/researcher.py` without PYTHONPATH set.
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend import state_paths as _state_paths


TTL_SECONDS = 900  # 15 minutes


def _cache_file() -> Path:
    # Resolved at call time, not import time — see D#1810.
    return _state_paths.STATE_DIR / "researcher-cache.json"


def _load() -> dict:
    cache_file = _cache_file()
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(cache: dict) -> None:
    cache_file = _cache_file()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=2))


def _key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def get(url: str) -> str | None:
    """Return cached body for url if within TTL, else None."""
    cache = _load()
    entry = cache.get(_key(url))
    if entry is None:
        return None
    age = time.time() - entry.get("stored_at", 0)
    if age > TTL_SECONDS:
        return None
    return entry.get("body")


def set_cache(url: str, body: str) -> None:
    """Store body for url with current timestamp."""
    cache = _load()
    cache[_key(url)] = {
        "url": url,
        "body": body,
        "stored_at": time.time(),
    }
    _save(cache)


def purge_expired() -> int:
    """Remove expired entries. Returns count removed."""
    cache = _load()
    now = time.time()
    expired = [k for k, v in cache.items() if now - v.get("stored_at", 0) > TTL_SECONDS]
    for k in expired:
        del cache[k]
    if expired:
        _save(cache)
    return len(expired)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 backend/researcher.py get <url>")
        print("       python3 backend/researcher.py set <url> <body>")
        print("       python3 backend/researcher.py purge")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "get":
        url = sys.argv[2]
        result = get(url)
        if result is None:
            sys.exit(1)  # cache miss — non-zero signals caller to WebFetch
        print(result)

    elif cmd == "set":
        if len(sys.argv) < 4:
            print("Usage: python3 backend/researcher.py set <url> <body>", file=sys.stderr)
            sys.exit(1)
        url = sys.argv[2]
        body = sys.argv[3]
        set_cache(url, body)
        print(f"Cached {len(body)} bytes for {url}")

    elif cmd == "purge":
        removed = purge_expired()
        print(f"Purged {removed} expired entries")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
