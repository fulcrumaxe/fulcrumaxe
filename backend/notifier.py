"""
Webhook notification dispatcher — delivers alerts to Slack, Discord, email, and
generic HTTP endpoints when interesting events fire on the internal event bus.

Configuration lives in .autonomous-team/config.json under the ``notifications``
key:

    {
      "notifications": {
        "enabled": true,
        "channels": [
          {
            "id": "slack-ops",
            "type": "slack",
            "webhook_url": "$SLACK_WEBHOOK_URL",
            "events": ["loop_stale", "pr_merged"],
            "min_interval_seconds": 60
          },
          {
            "id": "discord-all",
            "type": "discord",
            "webhook_url": "$DISCORD_WEBHOOK_URL",
            "events": "*",
            "min_interval_seconds": 30
          },
          {
            "id": "email-alerts",
            "type": "email",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "bot@example.com",
            "smtp_password": "$SMTP_PASSWORD",
            "from_addr": "bot@example.com",
            "to_addrs": ["ops@example.com"],
            "events": ["budget_exceeded"],
            "min_interval_seconds": 300
          }
        ]
      }
    }

Env-var references in string config values (``$VAR_NAME``) are resolved at
runtime.  Missing vars log a warning and disable that channel.

Usage:
    notifier = Notifier()
    notifier.start()   # subscribe to event bus
    # ...
    notifier.stop()    # unsubscribe

CLI:
    python backend/notifier.py test     # send a test notification to all channels
    python backend/notifier.py history  # print recent notifications
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# Allow `python backend/notifier.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.event_bus import (  # noqa: E402
    AgentOutputEvent,
    BudgetSpendEvent,
    Event,
    GateChangeEvent,
    LoopIterationEvent,
    get_bus,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _REPO_ROOT / ".autonomous-team" / "config.json"
_NOTIF_LOG = _REPO_ROOT / ".autonomous-team" / "notification-log.jsonl"

_SEVERITY_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
}

_DISCORD_COLORS = {
    "info": 0x3498DB,      # blue
    "warning": 0xF39C12,   # yellow
    "critical": 0xE74C3C,  # red
}

_EVENT_TYPE_MAP: dict[type, str] = {
    AgentOutputEvent: "agent_output",
    BudgetSpendEvent: "budget_spend",
    GateChangeEvent: "gate_change",
    LoopIterationEvent: "loop_iteration",
}

# Reverse map: string name → event type for subscription
_ALL_EVENT_TYPES = list(_EVENT_TYPE_MAP.keys())


# ---------------------------------------------------------------------------
# Notification log record
# ---------------------------------------------------------------------------


@dataclass
class NotifRecord:
    timestamp: str
    event_type: str
    channel_id: str
    channel_type: str
    success: bool
    error: str | None = None
    severity: str = "info"
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_env(value: str) -> str | None:
    """Replace ``$VAR`` references in *value* with env values.

    Returns the resolved string, or None if any referenced variable is missing.
    """
    pattern = re.compile(r'\$([A-Z_][A-Z0-9_]*)')
    missing: list[str] = []

    def replacer(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            missing.append(var)
            return ""
        return val

    resolved = pattern.sub(replacer, value)
    if missing:
        logger.warning("notifier: env vars not set: %s", ", ".join(missing))
        return None
    return resolved


def _http_post(url: str, payload: dict, timeout: int = 10, retries: int = 2) -> None:
    """POST *payload* as JSON to *url*, retrying on 5xx or connection errors."""
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
            if status < 500:
                return  # success (2xx/4xx — don't retry client errors)
            last_exc = RuntimeError(f"HTTP {status}")
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last_exc = exc
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
        if attempt < retries:
            time.sleep(5)
    raise RuntimeError(f"delivery failed after {retries + 1} attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class Notifier:
    """Subscribe to the event bus and dispatch notifications per channel config."""

    def __init__(self, config_file: Path = _CONFIG_FILE, log_file: Path = _NOTIF_LOG) -> None:
        self._config_file = config_file
        self._log_file = log_file
        self._lock = threading.Lock()

        # channel_id → last_sent_timestamp (monotonic)
        self._rate_state: dict[str, float] = {}

        # event bus subscription IDs
        self._sub_ids: list[str] = []

        # resolved channel configs
        self._channels: list[dict] = []

        self._enabled = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load config, resolve env vars, and subscribe to the event bus."""
        self._load_config()
        if not self._enabled:
            logger.info("notifier: disabled or no channels configured — skipping subscription")
            return
        bus = get_bus()
        for event_type in _ALL_EVENT_TYPES:
            sub_id = bus.subscribe(event_type, self._on_event)
            self._sub_ids.append(sub_id)
        logger.info("notifier: started with %d channel(s)", len(self._channels))

    def stop(self) -> None:
        """Unsubscribe from the event bus."""
        bus = get_bus()
        for sub_id in self._sub_ids:
            bus.unsubscribe(sub_id)
        self._sub_ids.clear()
        logger.info("notifier: stopped")

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Read and validate notification config from config.json."""
        self._channels = []
        self._enabled = False

        if not self._config_file.exists():
            return

        try:
            raw = json.loads(self._config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("notifier: cannot read config: %s", exc)
            return

        notif_cfg = raw.get("notifications", {})
        if not notif_cfg.get("enabled", False):
            return

        raw_channels = notif_cfg.get("channels", [])
        if not raw_channels:
            return

        self._enabled = True
        for ch in raw_channels:
            resolved = self._resolve_channel(ch)
            if resolved is not None:
                self._channels.append(resolved)

    def _resolve_channel(self, ch: dict) -> dict | None:
        """Resolve env vars in a channel config. Returns None if unresolvable."""
        resolved = dict(ch)
        ch_type = ch.get("type", "")
        ch_id = ch.get("id", ch_type)

        # Fields that may contain env-var references
        str_fields = ["webhook_url", "smtp_host", "smtp_user", "smtp_password",
                      "from_addr", "api_key"]
        for field in str_fields:
            val = ch.get(field)
            if val and isinstance(val, str) and "$" in val:
                result = _resolve_env(val)
                if result is None:
                    logger.warning(
                        "notifier: skipping channel %r — unresolved env var in %r", ch_id, field
                    )
                    return None
                resolved[field] = result

        # to_addrs list
        to_addrs = ch.get("to_addrs", [])
        resolved_addrs = []
        for addr in to_addrs:
            if isinstance(addr, str) and "$" in addr:
                result = _resolve_env(addr)
                if result is None:
                    logger.warning(
                        "notifier: skipping channel %r — unresolved env var in to_addrs", ch_id
                    )
                    return None
                resolved_addrs.append(result)
            else:
                resolved_addrs.append(addr)
        resolved["to_addrs"] = resolved_addrs

        resolved.setdefault("id", ch_id)
        resolved.setdefault("min_interval_seconds", 60)
        return resolved

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        """Callback invoked by the event bus for every subscribed event."""
        event_type_str = _EVENT_TYPE_MAP.get(type(event), type(event).__name__)
        severity, message = self._classify_event(event, event_type_str)

        for ch in self._channels:
            ch_events = ch.get("events", "*")
            # Filter check
            if ch_events != "*" and event_type_str not in ch_events:
                continue
            # Rate limit check
            ch_id = ch["id"]
            min_interval = ch.get("min_interval_seconds", 60)
            now_mono = time.monotonic()
            with self._lock:
                last_sent = self._rate_state.get(ch_id, 0.0)
                if now_mono - last_sent < min_interval:
                    continue  # silently drop
                self._rate_state[ch_id] = now_mono

            # Dispatch
            success = True
            error_msg: str | None = None
            try:
                self._dispatch(ch, event_type_str, severity, message, event)
            except Exception as exc:  # noqa: BLE001
                success = False
                error_msg = str(exc)
                logger.warning("notifier: delivery failed for channel %r: %s", ch_id, exc)

            self._log_notification(NotifRecord(
                timestamp=_now_iso(),
                event_type=event_type_str,
                channel_id=ch_id,
                channel_type=ch.get("type", ""),
                success=success,
                error=error_msg,
                severity=severity,
                message=message,
            ))

    def _classify_event(self, event: Event, event_type_str: str) -> tuple[str, str]:
        """Return (severity, human_message) for an event."""
        if isinstance(event, LoopIterationEvent):
            if event.idle:
                return "info", f"Loop iteration complete — idle (no new work)"
            return "info", (
                f"Loop iteration complete — {event.agents_spawned} agent(s) spawned, "
                f"{event.duration_seconds:.1f}s"
            )
        if isinstance(event, BudgetSpendEvent):
            return "info", (
                f"Budget spend: {event.role} agent used {event.input_tokens} input / "
                f"{event.output_tokens} output tokens"
            )
        if isinstance(event, GateChangeEvent):
            sev = "warning" if not event.new_value else "info"
            return sev, (
                f"Gate '{event.gate_name}' changed: {event.old_value} → {event.new_value}"
            )
        if isinstance(event, AgentOutputEvent):
            if event.event_subtype == "error":
                return "critical", f"Agent error from {event.agent_role}: {event.content[:200]}"
            return "info", f"Agent output from {event.agent_role}: {event.content[:200]}"
        return "info", f"Event: {event_type_str}"

    def _dispatch(
        self,
        ch: dict,
        event_type: str,
        severity: str,
        message: str,
        event: Event,
    ) -> None:
        ch_type = ch.get("type", "webhook")
        if ch_type == "slack":
            self._send_slack(ch, event_type, severity, message)
        elif ch_type == "discord":
            self._send_discord(ch, event_type, severity, message)
        elif ch_type == "email":
            self._send_email(ch, event_type, severity, message)
        else:
            self._send_webhook(ch, event_type, severity, message, event)

    # ------------------------------------------------------------------
    # Channel senders
    # ------------------------------------------------------------------

    def _send_slack(self, ch: dict, event_type: str, severity: str, message: str) -> None:
        url = ch.get("webhook_url", "")
        if not url:
            raise ValueError("slack channel missing webhook_url")
        emoji = _SEVERITY_EMOJI.get(severity, "ℹ️")
        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *{severity.upper()}* — `{event_type}`\n"
                            f"{message}\n"
                            f"_autonomous-forever · {_now_iso()}_"
                        ),
                    },
                }
            ]
        }
        _http_post(url, payload)

    def _send_discord(self, ch: dict, event_type: str, severity: str, message: str) -> None:
        url = ch.get("webhook_url", "")
        if not url:
            raise ValueError("discord channel missing webhook_url")
        color = _DISCORD_COLORS.get(severity, _DISCORD_COLORS["info"])
        payload = {
            "embeds": [
                {
                    "title": f"{event_type} ({severity})",
                    "description": message,
                    "color": color,
                    "footer": {"text": f"autonomous-forever · {_now_iso()}"},
                }
            ]
        }
        _http_post(url, payload)

    def _send_email(self, ch: dict, event_type: str, severity: str, message: str) -> None:
        smtp_host = ch.get("smtp_host", "")
        if not smtp_host:
            raise ValueError("email channel missing smtp_host")
        smtp_port = int(ch.get("smtp_port", 587))
        smtp_user = ch.get("smtp_user", "")
        smtp_password = ch.get("smtp_password", "")
        from_addr = ch.get("from_addr", smtp_user)
        to_addrs = ch.get("to_addrs", [])
        if not to_addrs:
            raise ValueError("email channel missing to_addrs")

        subject = f"[autonomous-forever] {severity.upper()}: {event_type}"
        body = f"{subject}\n\n{message}\n\nTimestamp: {_now_iso()}\n"

        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.sendmail(from_addr, to_addrs, msg.as_string())

    def _send_webhook(
        self,
        ch: dict,
        event_type: str,
        severity: str,
        message: str,
        event: Event,
    ) -> None:
        url = ch.get("webhook_url", "")
        if not url:
            raise ValueError("webhook channel missing webhook_url")
        payload = {
            "event": event_type,
            "severity": severity,
            "message": message,
            "timestamp": _now_iso(),
            "data": event.to_dict(),
        }
        _http_post(url, payload)

    # ------------------------------------------------------------------
    # Notification log
    # ------------------------------------------------------------------

    def _log_notification(self, record: NotifRecord) -> None:
        """Append *record* to the JSONL log and prune to 100 entries."""
        with self._lock:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            # Read existing lines
            existing: list[str] = []
            if self._log_file.exists():
                try:
                    existing = [
                        ln for ln in self._log_file.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                except OSError:
                    pass
            # Append new entry, keep last 100
            existing.append(json.dumps(record.to_dict(), default=str))
            trimmed = existing[-100:]
            try:
                self._log_file.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
            except OSError as exc:
                logger.warning("notifier: cannot write notification log: %s", exc)

    # ------------------------------------------------------------------
    # Public helpers for API / CLI
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 50) -> list[dict]:
        """Return the last *limit* notification records from the log."""
        if not self._log_file.exists():
            return []
        try:
            lines = [
                ln for ln in self._log_file.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except OSError:
            return []
        records: list[dict] = []
        for ln in lines:
            try:
                records.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        return records[-limit:]

    def send_test(self) -> list[dict[str, Any]]:
        """Send a test notification to all configured channels.

        Returns a list of {channel_id, success, error} dicts.
        """
        self._load_config()
        results: list[dict[str, Any]] = []
        for ch in self._channels:
            ch_id = ch["id"]
            try:
                self._dispatch(ch, "test", "info", "Test notification from autonomous-forever", Event())
                results.append({"channel_id": ch_id, "success": True, "error": None})
            except Exception as exc:  # noqa: BLE001
                results.append({"channel_id": ch_id, "success": False, "error": str(exc)})
        return results


# ---------------------------------------------------------------------------
# Module-level singleton for API integration
# ---------------------------------------------------------------------------

_notifier: Notifier | None = None
_notifier_lock = threading.Lock()


def get_notifier() -> Notifier:
    """Return the process-global Notifier, creating it on first call."""
    global _notifier  # noqa: PLW0603
    if _notifier is None:
        with _notifier_lock:
            if _notifier is None:
                _notifier = Notifier()
    return _notifier


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cmd_test(_args: argparse.Namespace) -> None:
    n = Notifier()
    results = n.send_test()
    if not results:
        print("No channels configured.")
        return
    for r in results:
        status = "OK" if r["success"] else f"FAILED: {r['error']}"
        print(f"  {r['channel_id']}: {status}")


def _cmd_history(_args: argparse.Namespace) -> None:
    n = Notifier()
    records = n.get_history(50)
    if not records:
        print("No notifications recorded.")
        return
    for rec in records:
        ts = rec.get("timestamp", "?")
        ch = rec.get("channel_id", "?")
        et = rec.get("event_type", "?")
        ok = "OK" if rec.get("success") else "FAIL"
        err = f" ({rec['error']})" if rec.get("error") else ""
        print(f"[{ts}] {ok} {ch} {et}{err}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Notification dispatcher CLI")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("test", help="Send a test notification to all configured channels")
    sub.add_parser("history", help="Print recent notifications from the log")
    args = parser.parse_args(argv)

    if args.command == "test":
        _cmd_test(args)
    elif args.command == "history":
        _cmd_history(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
