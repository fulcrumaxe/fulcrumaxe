"""scripts/tests/test_event_id_containment.py

Proves two independent guards added for D#1803:

  1. A component guard in scripts/subagent-stop-hook.sh (before the fallback
     EVENT_ID composition at :402) that substitutes `unknown`/`0` for a
     malformed agent-authored ROLE/DISC_PART *in the id-forming copy only* —
     it never rejects, because the fallback branch's uniqueness comes from
     SESSION_ID + a nanosecond timestamp, and `--role "$ROLE"` still carries
     the true value downstream (rejecting would drop the telemetry row, the
     D#1784 failure mode).

  2. A grammar-agnostic path-containment guard in scripts/lib/hook-event.sh
     (after :146, before the `mkdir -p` at :150) that asserts the resolved
     real path of HOOK_EVENT_MARKER/HOOK_EVENT_LOCK stays inside HOOK_EVENT_DIR,
     and rejects loudly (exit non-zero) if not. This is the belt to the
     component guard's braces: it holds even if a caller skips guard #1
     entirely, which is exactly what the tests below prove by calling
     hook_event_init directly.

Guard #1 is only observable through scripts/subagent-stop-hook.sh's own
SUBAGENT_STOP_DRY_RUN test harness: the real (non-dry-run) path swallows
post-agent-hook.sh's exit status with `|| true`, so nothing about a rejected
or substituted id is visible any other way.

Criterion 3 (this is the one that matters most): a guard that merely exists
but is never exercised proves nothing. The fallback branch at :402 is the
*only* code path that composes a four-hyphen-segment id whose third segment
is UUID-shaped (role-disc-SESSION_ID-nanos, where SESSION_ID is the Claude
session UUID) — the canonical branch (:360) emits three segments and never a
UUID. test_fallback_branch_produces_four_segment_uuid_discriminator asserts
exactly that shape, which is only reachable if :402 actually ran.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_EVENT_LIB = REPO_ROOT / "scripts" / "lib" / "hook-event.sh"
SUBAGENT_STOP_HOOK = REPO_ROOT / "scripts" / "subagent-stop-hook.sh"

# Only scripts/subagent-stop-hook.sh:402 can produce this shape: 4 hyphen-
# delimited segments, with the 3rd being a UUID (the Claude session_id).
EXPECTED_SEGMENTS = 4
DISCRIMINATOR_RE = re.compile(
    r"^(?P<role>[a-z][a-z-]{0,24})-(?P<disc>[0-9]+)-"
    r"(?P<session>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-(?P<nanos>[0-9]+)$"
)

# Same list the security report and the Spec cite: a directory traversal, an
# absolute-looking path, a newline, the empty string, a pathologically long
# value, and the bare backtick already sitting in agent_run.
HOSTILE_ROLE_VALUES = [
    "../ESCAPED/pwned",
    "/tmp/pwned",
    "line1\nline2",
    "",
    "x" * 500,
    "`",
]
HOSTILE_ROLE_IDS = ["dotdot-path", "abs-path", "newline", "empty", "long-500", "backtick"]

# Values that actually resolve outside HOOK_EVENT_DIR when composed into a
# path — used against the containment guard directly (criterion 5).
HOSTILE_EVENT_IDS = ["../ESCAPED/pwned-pah", "../../ESCAPED/deeper-pwned"]
HOSTILE_EVENT_ID_IDS = ["single-dotdot", "double-dotdot"]


def _write_transcript(path: Path, agent_value, discussion: int = 1803) -> None:
    """A flat-shape (Shape B) transcript with one assistant turn carrying an
    AGENT_OUTPUT envelope and NO hook_event_id= tag anywhere — the condition
    that sends subagent-stop-hook.sh down the fallback branch at :402."""
    envelope = {"agent": agent_value, "discussion": discussion, "verdict": "done"}
    line = {
        "role": "assistant",
        "content": (
            "Work complete.\n\n<!-- AGENT_OUTPUT -->\n```json\n"
            + json.dumps(envelope)
            + "\n```\n<!-- /AGENT_OUTPUT -->"
        ),
    }
    path.write_text(json.dumps(line) + "\n")


def _run_dry_run(tmp_path: Path, agent_value, session_id: str | None = None, discussion: int = 1803):
    """Drive scripts/subagent-stop-hook.sh end to end and capture the composed
    EVENT_ID via SUBAGENT_STOP_DRY_RUN — the only place it's observable."""
    session_id = session_id or str(uuid.uuid4())
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, agent_value, discussion=discussion)
    args_file = tmp_path / "args.json"
    stdin = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
        }
    )
    env = dict(os.environ)
    env["SUBAGENT_STOP_DRY_RUN"] = "1"
    env["SUBAGENT_STOP_ARGS_FILE"] = str(args_file)
    proc = subprocess.run(
        ["bash", str(SUBAGENT_STOP_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    args = json.loads(args_file.read_text()) if args_file.exists() else {}
    return session_id, proc, args


def _run_hook_event_init_direct(tmp_path: Path, event_id: str):
    """Call hook_event_init directly, bypassing subagent-stop-hook.sh's :402
    entirely, so a passing test here cannot be explained by guard #1 having
    already neutralized the value upstream (criterion 5)."""
    hook_event_dir = tmp_path / "hook-events"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f'source "{HOOK_EVENT_LIB}"\n'
        'hook_event_init "test-hook" "step1" --event-id "$1"\n'
        "echo INIT_OK\n"
    )
    env = dict(os.environ)
    env["HOOK_EVENT_DIR"] = str(hook_event_dir)
    proc = subprocess.run(
        ["bash", str(harness), event_id],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return hook_event_dir, proc


# ── Criterion 3: proof the fallback branch was actually entered ────────────


def test_fallback_branch_produces_four_segment_uuid_discriminator(tmp_path):
    session_id, proc, args = _run_dry_run(tmp_path, agent_value="executor", discussion=1803)
    assert proc.returncode == 0, proc.stderr
    event_id = args.get("event_id", "")
    m = DISCRIMINATOR_RE.match(event_id)
    assert m is not None, f"expected role-disc-uuid-nanos, got {event_id!r}"
    assert len(m.groupdict()) == EXPECTED_SEGMENTS
    assert m.group("session") == session_id
    assert m.group("role") == "executor"
    assert m.group("disc") == "1803"


# ── Criterion 4: hostile ROLE values are substituted, not rejected ─────────


@pytest.mark.parametrize("hostile_value", HOSTILE_ROLE_VALUES, ids=HOSTILE_ROLE_IDS)
def test_hostile_role_substituted_not_rejected(tmp_path, hostile_value):
    session_id, proc, args = _run_dry_run(tmp_path, agent_value=hostile_value, discussion=1803)
    assert proc.returncode == 0, proc.stderr
    event_id = args.get("event_id", "")
    assert event_id.startswith(f"unknown-1803-{session_id}-"), f"got {event_id!r}"
    # the original value must be visible to an operator, not silently dropped
    assert f"'{hostile_value}'" in proc.stderr, proc.stderr
    # nothing written outside tmp_path (dry-run never invokes hook_event_init)
    for p in tmp_path.rglob("*"):
        assert str(p.resolve()).startswith(str(tmp_path.resolve()))


def test_hostile_discussion_substituted_not_rejected(tmp_path):
    # discussion is a string in the envelope on purpose — a non-digit value
    # must trip the DISC_PART guard the same way a hostile ROLE trips its own.
    envelope_transcript = tmp_path / "transcript.jsonl"
    envelope_transcript.write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": (
                    "Work complete.\n\n<!-- AGENT_OUTPUT -->\n```json\n"
                    + json.dumps({"agent": "executor", "discussion": "../nope", "verdict": "done"})
                    + "\n```\n<!-- /AGENT_OUTPUT -->"
                ),
            }
        )
        + "\n"
    )
    session_id = str(uuid.uuid4())
    args_file = tmp_path / "args.json"
    stdin = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "transcript_path": str(envelope_transcript),
            "cwd": str(tmp_path),
        }
    )
    env = dict(os.environ)
    env["SUBAGENT_STOP_DRY_RUN"] = "1"
    env["SUBAGENT_STOP_ARGS_FILE"] = str(args_file)
    proc = subprocess.run(
        ["bash", str(SUBAGENT_STOP_HOOK)], input=stdin, capture_output=True, text=True, env=env, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    args = json.loads(args_file.read_text())
    event_id = args.get("event_id", "")
    assert event_id.startswith(f"executor-0-{session_id}-"), f"got {event_id!r}"
    assert "non-canonical DISC_PART" in proc.stderr


# ── Criterion 5: containment guard holds even if the component guard is bypassed ──


@pytest.mark.parametrize("hostile_id", HOSTILE_EVENT_IDS, ids=HOSTILE_EVENT_ID_IDS)
def test_containment_guard_rejects_when_component_guard_bypassed(tmp_path, hostile_id):
    hook_event_dir, proc = _run_hook_event_init_direct(tmp_path, hostile_id)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert hostile_id in proc.stderr, proc.stderr
    # the escape target must never have been created anywhere under tmp_path
    assert not (tmp_path / "ESCAPED").exists()
    assert "INIT_OK" not in proc.stdout


# ── Criterion 8: canonical / generated / -pah ids are unaffected ───────────


def test_canonical_three_segment_id_unaffected(tmp_path):
    hook_event_dir, proc = _run_hook_event_init_direct(tmp_path, "executor-1803-1787283900")
    assert proc.returncode == 0, proc.stderr
    assert (hook_event_dir / "executor-1803-1787283900.json").exists()


def test_generated_hex_id_unaffected(tmp_path):
    hook_event_dir, proc = _run_hook_event_init_direct(tmp_path, "0123456789abcdef")
    assert proc.returncode == 0, proc.stderr
    assert (hook_event_dir / "0123456789abcdef.json").exists()


def test_pah_suffixed_id_unaffected(tmp_path):
    hook_event_dir, proc = _run_hook_event_init_direct(tmp_path, "executor-1803-1787283900-pah")
    assert proc.returncode == 0, proc.stderr
    assert (hook_event_dir / "executor-1803-1787283900-pah.json").exists()
