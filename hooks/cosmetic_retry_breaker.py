#!/usr/bin/env python3
"""hooks/cosmetic_retry_breaker.py

PreToolUse hook — runtime cosmetic-retry circuit breaker.

Reads a JSON tool-call object from stdin:
  {"tool_name": "Bash", "tool_input": {"command": "..."}, "cwd": "..."}

Behaviour:
  - Maintains a per-session JSONL ring buffer at /tmp/cosmetic-<agent-id>.jsonl
    (last 5 entries). No shared DB — avoids write contention on the hot path.
  - Detects "cosmetic variant": same command stem (first 2 tokens) + ≥80% token-set
    Jaccard similarity + previous call exit ≠ 0.
  - After 3 cosmetic variants of a failing command → exit 2 (block) with a sanitized
    injected message.
  - After 5 cosmetic variants → exit 1 (block) + write sentinel file
    /tmp/cosmetic-<agent-id>.terminate (executor wrapper checks this).
  - Appends a JSON line to .autonomous-team/hook-events/cosmetic-blocks-YYYY-MM-DD.jsonl
    on every block event.
  - 50ms hard wall-clock timeout — on timeout or any IO error → fail-open (exit 0).
  - Gated by control-plane: gates.cosmetic_retry_breaker (default false).

Security:
  - Sanitize <command> placeholder in BLOCKED message: escape control bytes,
    strip </system>-style markers and tokenizer delimiters (<|...|>), cap at 200 chars.
  - Scrub secrets from JSONL log: GH_TOKEN, ANTHROPIC_API_KEY, Authorization headers,
    .env-style KEY=VALUE pairs.
  - Validate agent_id from env: must match [a-zA-Z0-9_-]{1,128}. If invalid, fail-open
    (do not track — safer than blocking on garbage input).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: make hooks/ importable when invoked directly as a script
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks._retry_common import (  # noqa: E402
    command_stem,
    jaccard,
    normalize,
    tokenize,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RING_SIZE = 5          # Keep last 5 calls in the ring buffer
_BLOCK_THRESHOLD = 3    # Block after this many cosmetic variants
_TERM_THRESHOLD = 5     # Exit-1 + sentinel after this many cosmetic variants
_JACCARD_THRESHOLD = 0.8
_HOOK_TIMEOUT_S = 0.050  # 50ms
_TELEMETRY_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events"
_CMD_MAX_CHARS = 200

# Validate agent_id — alphanumeric, hyphens, underscores, 1–128 chars.
# Rejects path-traversal payloads like "../../etc/passwd".
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# ---------------------------------------------------------------------------
# Secret scrubber
# ---------------------------------------------------------------------------
# TODO: replace with backend/secret_scrubber.py once that module exists.

_SECRET_PATTERNS = [
    re.compile(r"GH_TOKEN=\S+"),
    re.compile(r"ANTHROPIC_API_KEY=\S+"),
    re.compile(r"Authorization:\s*Bearer\s+\S+"),
    re.compile(r"(?i)\b(?:API_KEY|SECRET|PASSWORD|TOKEN|PASSWD)\s*=\s*\S+"),
]


def _scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split("=")[0] + "=***REDACTED***"
                       if "=" in m.group(0)
                       else re.sub(r"Bearer\s+\S+", "Bearer ***REDACTED***", m.group(0)),
                       text)
    return text


# ---------------------------------------------------------------------------
# Command sanitizer (for BLOCKED message — prompt injection mitigation)
# ---------------------------------------------------------------------------

# Strip </system>, </user>, </assistant> and variants.
_INJECTION_MARKER = re.compile(r"</?\s*(?:system|user|assistant)[^>]*>", re.IGNORECASE)
# Strip tokenizer delimiter sequences: <|im_start|>, <|im_end|>, <|endoftext|>, etc.
_TOKENIZER_MARKER = re.compile(r"<\|[a-zA-Z0-9_]+\|>")
# Strip control bytes except \t and \n.
_CONTROL_BYTES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_cmd(cmd: str) -> str:
    """Escape command for safe injection into BLOCKED message."""
    cmd = _INJECTION_MARKER.sub("", cmd)
    cmd = _TOKENIZER_MARKER.sub("", cmd)
    cmd = _CONTROL_BYTES.sub("", cmd)
    if len(cmd) > _CMD_MAX_CHARS:
        cmd = cmd[:_CMD_MAX_CHARS] + "..."
    return cmd


# ---------------------------------------------------------------------------
# Control plane gate
# ---------------------------------------------------------------------------


def _gate_enabled() -> bool:
    """Return True if gates.cosmetic_retry_breaker is enabled. Default: False."""
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "control_plane.py"),
             "get", "gates.cosmetic_retry_breaker"],
            capture_output=True, text=True, timeout=0.04
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-session ring buffer
# ---------------------------------------------------------------------------


def _session_key() -> "str | None":
    """Return a validated agent UUID from the environment, or None.

    Validates against _AGENT_ID_RE to prevent path traversal attacks — a
    malicious value like '../../etc/cron.d/evil' would otherwise let _ring_path()
    and _sentinel_path() write outside /tmp/. On invalid input we fail-open: the
    breaker simply won't track this session.
    """
    for var in ("CLAUDE_AGENT_ID", "AF_AGENT_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            if _AGENT_ID_RE.match(val):
                return val
            # Invalid agent_id — fail-open, don't track
            return None
    return None


def _ring_path(agent_id: str) -> Path:
    return Path(f"/tmp/cosmetic-{agent_id}.jsonl")


def _sentinel_path(agent_id: str) -> Path:
    """Return the terminate sentinel path for this agent. Extracted for testability."""
    return Path(f"/tmp/cosmetic-{agent_id}.terminate")


def _read_ring(agent_id: str) -> list[dict]:
    """Read the ring buffer for this agent (at most _RING_SIZE entries)."""
    path = _ring_path(agent_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-_RING_SIZE:]


def _append_ring(agent_id: str, entry: dict) -> None:
    """Append one entry to the ring buffer, keeping only the last _RING_SIZE.

    The command field is scrubbed before writing to prevent secrets from
    landing in /tmp/ in plaintext (CWE-312).
    """
    existing = _read_ring(agent_id)
    existing.append(entry)
    trimmed = existing[-_RING_SIZE:]
    path = _ring_path(agent_id)
    try:
        path.write_text(
            "\n".join(json.dumps(e) for e in trimmed) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # fail-open


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _log_block(agent_id: str, command: str, variant_count: int, action: str) -> None:
    """Append one JSON line to the daily cosmetic-blocks telemetry file."""
    try:
        _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _TELEMETRY_DIR / f"cosmetic-blocks-{date.today().isoformat()}.jsonl"
        import datetime as _dt
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "agent_id": agent_id,
            "command": _scrub(command)[:500],
            "variant_count": variant_count,
            "action": action,  # "block" or "terminate"
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never wedge on telemetry failure


# ---------------------------------------------------------------------------
# Similarity check
# ---------------------------------------------------------------------------


def _is_cosmetic_variant(new_cmd: str, ring: list[dict]) -> "tuple[bool, int]":
    """Return (is_variant, count_of_failing_cosmetic_variants_in_ring).

    A ring entry is a cosmetic variant of new_cmd if:
      1. Its exit_code ≠ 0 (it was a failure)
      2. Its command stem (first 2 tokens) matches new_cmd's stem
      3. Token-set Jaccard ≥ _JACCARD_THRESHOLD
    """
    new_norm = normalize(new_cmd)
    new_stem = command_stem(new_norm)
    new_tokens = tokenize(new_norm)

    count = 0
    for entry in ring:
        if entry.get("exit_code") in (0, None):
            continue  # skip successes and in-flight (unresolved) entries
        old_cmd = entry.get("command", "")
        old_norm = normalize(old_cmd)
        old_stem = command_stem(old_norm)
        old_tokens = tokenize(old_norm)

        if new_stem != old_stem:
            continue
        if jaccard(new_tokens, old_tokens) >= _JACCARD_THRESHOLD:
            count += 1

    return count > 0, count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    start = time.monotonic()

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool_name: str = payload.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    tool_input: dict = payload.get("tool_input", {})
    new_command: str = tool_input.get("command", "")

    if not new_command:
        sys.exit(0)

    # Gate check (fail-open if gate is off or any error)
    if time.monotonic() - start > _HOOK_TIMEOUT_S:
        sys.exit(0)

    try:
        if not _gate_enabled():
            sys.exit(0)
    except Exception:
        sys.exit(0)

    if time.monotonic() - start > _HOOK_TIMEOUT_S:
        sys.exit(0)

    # Session key
    agent_id = _session_key()
    if not agent_id:
        sys.exit(0)

    # Read ring
    try:
        ring = _read_ring(agent_id)
    except Exception:
        sys.exit(0)

    if time.monotonic() - start > _HOOK_TIMEOUT_S:
        sys.exit(0)

    # Check for cosmetic variant pattern
    is_variant, variant_count = _is_cosmetic_variant(new_command, ring)

    # Append current call to ring — scrub secrets before writing to /tmp/.
    # (PostToolUse would update exit_code; for now we record None and treat
    # absence as 0 when reading. The variant check reads prior entries, so
    # adding now is safe.)
    try:
        scrubbed_command = _scrub(new_command)
        _append_ring(agent_id, {"command": scrubbed_command, "exit_code": None, "ts": time.time()})
    except Exception:
        pass  # fail-open

    if not is_variant:
        sys.exit(0)

    # variant_count is the count of failing similar entries already in the ring
    # plus 1 for this new attempt = total attempts after this one would be variant_count+1
    total_variants = variant_count + 1  # counting this attempt as the next one

    sanitized = _sanitize_cmd(new_command)

    if total_variants >= _TERM_THRESHOLD:
        # Write sentinel file
        try:
            sentinel = _sentinel_path(agent_id)
            sentinel.write_text(
                json.dumps({"ts": time.time(), "command": _scrub(new_command)[:200]}),
                encoding="utf-8",
            )
        except Exception:
            pass
        _log_block(agent_id, new_command, total_variants, "terminate")
        sys.stderr.write(
            f"BLOCKED: cosmetic retry loop detected on '{sanitized}'. "
            f"({total_variants} variants of a failing command) "
            "Emitting verdict:fail block_reason:cosmetic_retry_loop — change approach.\n"
        )
        sys.exit(1)

    elif total_variants >= _BLOCK_THRESHOLD:
        _log_block(agent_id, new_command, total_variants, "block")
        sys.stderr.write(
            f"BLOCKED: cosmetic retry loop detected on '{sanitized}'. "
            f"({total_variants} cosmetic variants of a failing command) "
            "Change approach or emit verdict:fail.\n"
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    deadline = time.monotonic() + _HOOK_TIMEOUT_S
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Unexpected error — fail open
        sys.stderr.write(f"[cosmetic-retry-breaker] INTERNAL ERROR (fail-open)\n")
        sys.exit(0)
