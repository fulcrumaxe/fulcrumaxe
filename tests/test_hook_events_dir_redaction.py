"""tests/test_hook_events_dir_redaction.py — every hook that writes into
hooks/sandbox.py's own .autonomous-team/hook-events/ directory must scrub
spawn tags too (D#2459).

D#1957/D#1959 routed sandbox.py's eight telemetry writers through
_telemetry_line(), which scrubs spawn tags before the JSON line is written
(see tests/test_hook_block_log_redaction.py). Four siblings write into the
same directory with no scrub of their own:
  - hooks/runaway_loop_guard.py
  - hooks/repo_scope_warn.py
  - hooks/cosmetic_retry_breaker.py (whose /tmp/cosmetic-<agent-id>.jsonl
    ring buffer and terminate sentinel carry the identical gap one file over)
  - hooks/subagent_stop_dial_audit.py, writing directly into the SAME
    blocks-*.jsonl file sandbox.py writes to, carrying
    entry["input"].get("prompt", "")[:200] — an attempted Agent() spawn
    prompt, i.e. exactly the field D#1959 fixed sandbox.py's own writer for.
    Found in review of the first three; not part of the original D#2459 body.

A reader of the directory would find redacted lines next to unredacted ones
with no way to tell which files are covered — arguably worse than an
unscrubbed directory, since it invites the assumption the directory is
clean.

Following tests/test_hook_block_log_redaction.py's method: drive each
writer with a payload carrying a canonical spawn tag, read the file back off
disk, and confirm the tag is gone using the real extractor
(scripts/lib/transcript_event_id.py) — not a grep. That file's own negative
control established that pointing the extractor at a raw *.jsonl file
directly returns "" regardless of whether the line is redacted, so the tag
has to be checked inside a transcript shaped the way an agent that reads the
log actually receives it (a tool_result in a user turn).

Enumeration note (also see the PR this test file shipped in): `grep -rl
"hook-events" hooks/*.py` additionally matches hooks/spawn_tag_redaction.py
(the redactor itself, not a writer), hooks/sandbox_rules.py (a comment
naming the directory, not a writer), hooks/payload_shape.py (a writer, but
one whose own docstring explains it deliberately never records command or
prompt text — only payload key names and session_id, CWE-532-aware by
design), and hooks/claude_execve_fence.py (two writers — `_fallback_log`
writes a fixed internal reason string plus a pid, `_audit_block` writes an
execve-syscall target path recovered via ptrace, neither is free-form
agent-authored command/prompt text). None of those four carry the
attempted-command-or-prompt content that makes the other five files (this
one's four plus sandbox.py) an actual spawn-tag leak vector, so none of them
are touched here. A grep-based sweep cannot itself tell "carries
agent-authored command/prompt text" from "writes structured metadata only" —
that judgment call is why this is a per-file review, not a mechanical count,
and why it is worth someone eventually writing the durable version: a test
that asserts every write into hook-events/ of a field plausibly containing
free-form text goes through redact_spawn_tags, the same shape as
test_hook_block_log_redaction.py::test_every_telemetry_writer_goes_through_the_scrubber
already uses for sandbox.py alone (and which is itself currently red there —
see this PR's description).

What this does and does not gate: CI runs no pytest today (D#2443), so this
file gates nothing in the build, same caveat as its sibling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks import (  # noqa: E402
    cosmetic_retry_breaker,
    repo_scope_warn,
    runaway_loop_guard,
    subagent_stop_dial_audit,
)
from hooks.spawn_tag_redaction import TAG  # noqa: E402


def _load_extractor():
    """Load the real consumer by path, same as test_hook_block_log_redaction.py:
    scripts/lib/ is not a package, and the point is what the shipped consumer
    answers, not a local reimplementation of its regex."""
    spec = importlib.util.spec_from_file_location(
        "transcript_event_id",
        _REPO_ROOT / "scripts" / "lib" / "transcript_event_id.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extractor = _load_extractor()

# Built from the imported TAG (not spelled contiguously in this file's own
# source) so this file never carries a canonical id of its own —
# scripts/ci/no-planted-spawn-ids-guard.py would fail the build on it,
# correctly, if it did.
PLANTED_ID = "executor-2459-1788900000"
SPAWN_TAG_LINE = f"{TAG}{PLANTED_ID}"

# A realistic blocked command: the tag sits inside real, legitimate content
# (an echo writing a note) — exactly the kind of thing these hooks are meant
# to keep readable.
COMMAND = f"echo '{SPAWN_TAG_LINE}' >> /srv/example-checkout/notes.md"


def _transcript_of_agent_reading(log_line: str, path: Path) -> Path:
    """The shape an agent actually receives when it reads a hook-events file:
    a user turn whose content is a tool_result block. Reproduced from
    test_hook_block_log_redaction.py — the extractor was written for this
    shape, not for a raw file body."""
    turn = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_read_the_block_log",
                    "content": log_line,
                }
            ],
        },
    }
    path.write_text(json.dumps(turn) + "\n")
    return path


def _only_log_line(log_dir: Path, glob: str) -> str:
    files = sorted(log_dir.glob(glob))
    assert len(files) == 1, f"expected one daily log file for {glob!r}, got {files}"
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1, f"expected one log line, got {lines}"
    return lines[0]


def test_runaway_loop_guard_block_log_scrubs_spawn_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(runaway_loop_guard, "_TELEMETRY_DIR", tmp_path)

    runaway_loop_guard._log_block(COMMAND)

    line = _only_log_line(tmp_path, "runaway-loop-blocks-*.jsonl")
    entry = json.loads(line)  # still valid JSON after redaction

    assert PLANTED_ID not in line
    # Legitimate content survives: the command is still recognisable.
    assert "echo '" in entry["command_excerpt"]
    assert "/srv/example-checkout/notes.md" in entry["command_excerpt"]
    assert TAG in entry["command_excerpt"]

    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")
    assert extractor.extract_event_id(str(transcript)) == ""


def test_repo_scope_warn_log_scrubs_spawn_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_scope_warn, "_TELEMETRY_DIR", tmp_path)

    gh_command = f'gh pr comment 1 --body "{SPAWN_TAG_LINE}"'
    repo_scope_warn._log_warn(gh_command, "fulcrumaxe/fulcrumaxe")

    line = _only_log_line(tmp_path, "repo-scope-warns-*.jsonl")
    entry = json.loads(line)

    assert PLANTED_ID not in line
    assert "gh pr comment 1" in entry["command_excerpt"]
    assert TAG in entry["command_excerpt"]
    # Non-command fields are untouched.
    assert entry["target_repo"] == "fulcrumaxe/fulcrumaxe"

    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")
    assert extractor.extract_event_id(str(transcript)) == ""


def test_cosmetic_retry_breaker_block_log_scrubs_spawn_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(cosmetic_retry_breaker, "_TELEMETRY_DIR", tmp_path)

    cosmetic_retry_breaker._log_block("agent-x", COMMAND, 3, "block")

    line = _only_log_line(tmp_path, "cosmetic-blocks-*.jsonl")
    entry = json.loads(line)

    assert PLANTED_ID not in line
    assert "echo '" in entry["command"]
    assert "/srv/example-checkout/notes.md" in entry["command"]
    assert TAG in entry["command"]
    assert entry["agent_id"] == "agent-x"
    assert entry["action"] == "block"

    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")
    assert extractor.extract_event_id(str(transcript)) == ""


def test_cosmetic_retry_breaker_ring_and_sentinel_scrub_spawn_tag_end_to_end(
    tmp_path, monkeypatch
):
    """Drive hooks/cosmetic_retry_breaker.py through its real main() entry
    point (not a reimplementation of its write logic) so this test actually
    exercises the ring-buffer append (D#2459 names line 198) and the
    terminate sentinel write (line 341), not just the redaction helper in
    isolation.

    Pre-seeds the ring with 4 prior failing calls identical to the new one,
    so the 5th (real, live) call crosses _TERM_THRESHOLD and main() writes
    to all three sites in one pass: the ring buffer, the terminate sentinel,
    and the cosmetic-blocks telemetry file.
    """
    import io

    ring_path = tmp_path / "cosmetic-agent-x.jsonl"
    sentinel_path = tmp_path / "cosmetic-agent-x.terminate"
    monkeypatch.setattr(cosmetic_retry_breaker, "_ring_path", lambda agent_id: ring_path)
    monkeypatch.setattr(cosmetic_retry_breaker, "_sentinel_path", lambda agent_id: sentinel_path)
    monkeypatch.setattr(cosmetic_retry_breaker, "_TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr(cosmetic_retry_breaker, "_gate_enabled", lambda: True)
    monkeypatch.setenv("CLAUDE_AGENT_ID", "agent-x")
    monkeypatch.delenv("AF_AGENT_ID", raising=False)

    # 4 prior failing calls, identical to the live command below, so the
    # live call is a cosmetic variant of all 4 (jaccard == 1.0).
    ring_path.write_text(
        "\n".join(
            json.dumps({"command": COMMAND, "exit_code": 1, "ts": i})
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )

    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": COMMAND}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    try:
        cosmetic_retry_breaker.main()
    except SystemExit as exc:
        assert exc.code == 1  # _TERM_THRESHOLD (5) reached -> exit 1

    ring_line = ring_path.read_text().splitlines()[-1]  # the just-appended live entry
    ring_entry = json.loads(ring_line)
    assert PLANTED_ID not in ring_line
    assert "echo '" in ring_entry["command"]
    assert TAG in ring_entry["command"]

    sentinel_line = sentinel_path.read_text()
    sentinel_entry = json.loads(sentinel_line)
    assert PLANTED_ID not in sentinel_line
    assert TAG in sentinel_entry["command"]

    block_line = _only_log_line(tmp_path, "cosmetic-blocks-*.jsonl")
    block_entry = json.loads(block_line)
    assert PLANTED_ID not in block_line
    assert TAG in block_entry["command"]
    assert block_entry["action"] == "terminate"

    for line in (ring_line, sentinel_line, block_line):
        transcript = _transcript_of_agent_reading(line, tmp_path / f"post-{hash(line)}.jsonl")
        assert extractor.extract_event_id(str(transcript)) == ""


def test_subagent_stop_dial_audit_scrubs_spawn_tag(tmp_path, monkeypatch):
    """hooks/subagent_stop_dial_audit.py writes directly into the same
    blocks-*.jsonl file hooks/sandbox.py writes to, carrying
    entry["input"].get("prompt", "")[:200] under the key "attempted_target"
    — the same field name and the same content class (an attempted Agent()
    spawn prompt) that D#1959 fixed sandbox.py's own writer for. Drives the
    real _scan_transcript()/_append_audit_row() path with a synthetic
    subagent transcript containing an Agent tool_use whose prompt carries a
    canonical spawn tag."""
    monkeypatch.setattr(subagent_stop_dial_audit, "_HOOK_EVENTS_DIR", tmp_path)
    monkeypatch.setattr(
        subagent_stop_dial_audit,
        "_WORKTREE_PREFIXES",
        ["/srv/example-checkout/.claude/worktrees/"],
    )

    attempted_prompt = (
        "You are the executor for D#2459.\n"
        f"{SPAWN_TAG_LINE}\n"
        "Implement the redaction fix for the fourth hook and open a PR.\n"
    )
    transcript_line = json.dumps(
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_agent_spawn_attempt",
                    "name": "Agent",
                    "input": {"prompt": attempted_prompt},
                }
            ]
        }
    )
    transcript_path = tmp_path / "subagent-transcript.jsonl"
    transcript_path.write_text(transcript_line + "\n", encoding="utf-8")

    import io

    payload = json.dumps(
        {
            "cwd": "/srv/example-checkout/.claude/worktrees/agent-x",
            "transcript_path": str(transcript_path),
        }
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.delenv("CLAUDE_HOOK_CWD", raising=False)
    monkeypatch.delenv("CLAUDE_SUBAGENT_TRANSCRIPT_PATH", raising=False)

    try:
        subagent_stop_dial_audit.main()
    except SystemExit as exc:
        assert exc.code == 0  # this hook never blocks

    line = _only_log_line(tmp_path, "blocks-*.jsonl")
    entry = json.loads(line)

    assert PLANTED_ID not in line
    assert TAG in entry["attempted_target"]
    # Legitimate content survives: the rest of the prompt is still readable.
    assert "executor for D#2459" in entry["attempted_target"]
    assert entry["kind"] == "sandbox_block_agent_spawn"
    assert entry["source"] == "subagent_stop_dial_audit"

    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")
    assert extractor.extract_event_id(str(transcript)) == ""
