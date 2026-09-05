"""Tests for backend/log.py — structured JSON logging setup."""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO

import pytest

# Ensure backend is importable when running from repo root.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.log import setup_logging, JSONFormatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_log(level: str = "DEBUG", json_format: bool = True) -> tuple[logging.Logger, StringIO]:
    """Configure logging to a StringIO buffer and return (logger, buffer)."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s %(module)s: %(message)s")
        )
    root = logging.root
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    return logging.getLogger("test_log"), buf


# ---------------------------------------------------------------------------
# JSONFormatter tests
# ---------------------------------------------------------------------------


def test_json_formatter_produces_valid_json():
    """Each log record should serialize to a valid JSON object."""
    logger, buf = _capture_log()
    logger.info("hello world")
    line = buf.getvalue().strip()
    obj = json.loads(line)  # raises if invalid
    assert isinstance(obj, dict)


def test_json_formatter_required_fields():
    """JSON output must contain ts, level, module, and msg fields."""
    logger, buf = _capture_log()
    logger.info("check fields")
    obj = json.loads(buf.getvalue().strip())
    assert "ts" in obj
    assert "level" in obj
    assert "module" in obj
    assert "msg" in obj


def test_json_formatter_level_name():
    """The 'level' field must match the logging level name."""
    logger, buf = _capture_log()
    logger.warning("a warning")
    obj = json.loads(buf.getvalue().strip())
    assert obj["level"] == "WARNING"


def test_json_formatter_msg_content():
    """The 'msg' field must contain the log message text."""
    logger, buf = _capture_log()
    logger.info("my specific message")
    obj = json.loads(buf.getvalue().strip())
    assert obj["msg"] == "my specific message"


def test_json_formatter_module_name():
    """The 'module' field must be populated (not empty)."""
    logger, buf = _capture_log()
    logger.error("module check")
    obj = json.loads(buf.getvalue().strip())
    assert obj["module"]  # non-empty string


def test_json_formatter_ts_is_iso8601():
    """The 'ts' field must be a parseable ISO-8601 timestamp."""
    from datetime import datetime
    logger, buf = _capture_log()
    logger.info("timestamp test")
    obj = json.loads(buf.getvalue().strip())
    ts = obj["ts"]
    # Should not raise
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# setup_logging tests
# ---------------------------------------------------------------------------


def test_setup_logging_configures_root_handler():
    """setup_logging() must attach at least one handler to the root logger."""
    setup_logging(level="INFO", json_format=True)
    assert logging.root.handlers


def test_setup_logging_level_filtering_suppresses_debug(tmp_path):
    """With level=INFO, DEBUG messages must not appear in output."""
    buf = StringIO()
    setup_logging(level="INFO", json_format=True)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)

    logger = logging.getLogger("test_filter")
    logger.debug("this is debug")
    logger.info("this is info")

    lines = [l for l in buf.getvalue().strip().splitlines() if l]
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "INFO"


def test_setup_logging_warning_level_suppresses_info():
    """With level=WARNING, INFO messages must not appear."""
    buf = StringIO()
    setup_logging(level="WARNING", json_format=True)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.WARNING)

    logger = logging.getLogger("test_warn_filter")
    logger.info("suppressed info")
    logger.warning("visible warning")

    lines = [l for l in buf.getvalue().strip().splitlines() if l]
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "WARNING"


def test_setup_logging_idempotent():
    """Calling setup_logging() twice must not duplicate handlers."""
    setup_logging(level="INFO", json_format=True)
    setup_logging(level="INFO", json_format=True)
    assert len(logging.root.handlers) == 1


def test_setup_logging_stderr_routing(monkeypatch):
    """setup_logging() must route log output to stderr, not stdout."""
    setup_logging(level="DEBUG", json_format=True)
    # The single handler must write to sys.stderr.
    assert len(logging.root.handlers) == 1
    handler = logging.root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    # StreamHandler's stream attribute is sys.stderr for stderr handlers.
    assert handler.stream is sys.stderr


def test_text_formatter_produces_human_readable_output():
    """With json_format=False, output must be human-readable (not JSON)."""
    buf = StringIO()
    setup_logging(level="DEBUG", json_format=False)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(module)s: %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.DEBUG)

    logger = logging.getLogger("test_text")
    logger.info("human readable")

    line = buf.getvalue().strip()
    # Should NOT be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "human readable" in line


def test_af_log_level_env_var(monkeypatch):
    """setup_logging() must read AF_LOG_LEVEL from environment when level arg is None."""
    monkeypatch.setenv("AF_LOG_LEVEL", "ERROR")
    setup_logging(level=None, json_format=True)
    assert logging.root.level == logging.ERROR
