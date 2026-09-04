"""
Structured logging for the fulcrumaxe backend.

Usage in each module:
    import logging
    logger = logging.getLogger(__name__)

Setup (call once at program entry point):
    from backend.log import setup_logging
    setup_logging()  # reads AF_LOG_LEVEL env var, defaults to INFO, JSON format

Output:
    JSON (default):  {"ts": "2026-04-10T12:00:00Z", "level": "INFO", "module": "api", "msg": "..."}
    Text (--log-format=text):  [2026-04-10 12:00:00] INFO api: message
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "module": record.module,
                "msg": record.getMessage(),
            }
        )


_TEXT_FORMAT = "[%(asctime)s] %(levelname)s %(module)s: %(message)s"
_TEXT_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level: str | None = None, json_format: bool = True) -> None:
    """
    Configure the root logger.

    Args:
        level: Log level string (DEBUG/INFO/WARNING/ERROR). If None, reads
               AF_LOG_LEVEL env var, falling back to INFO.
        json_format: True → JSON formatter; False → human-readable text.

    Calling this function more than once is safe — subsequent calls reconfigure
    the root logger in place.
    """
    global _configured

    import os

    resolved_level = (level or os.environ.get("AF_LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt=_TEXT_DATE_FMT))

    root = logging.root
    root.handlers = [handler]
    root.setLevel(numeric_level)
    _configured = True
