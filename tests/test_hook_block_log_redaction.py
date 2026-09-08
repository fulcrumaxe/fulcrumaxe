"""tests/test_hook_block_log_redaction.py — the sandbox hook's block log must
not hand a usable spawn id to the agent that reads it (D#1959).

The mechanism being tested, end to end
--------------------------------------
1. An agent's Agent()/Bash call is blocked. hooks/sandbox.py appends a JSON line
   to .autonomous-team/hook-events/blocks-<date>.jsonl containing the attempted
   command or prompt verbatim, truncated but not otherwise altered.
2. Agent prompts carry "hook_event_id=<role>-<disc>-<unix_ts>", so the log
   accumulates real, contiguous, canonical spawn ids.
3. A later agent reads that log — `cat`, a Read tool call, a run-analyst sweep.
   The file's contents land in a `tool_result` block inside that agent's own
   transcript, which is a user-role turn.
4. scripts/lib/transcript_event_id.py walks user/system turns of that transcript
   looking for a canonical tag, finds the borrowed one, and hands it to
   `complete_run()` — which upserts on agent_id, writing this agent's end_ts and
   token counts onto somebody else's row.

Step 3 is why the assertions below wrap the log line in a transcript instead of
running the extractor on the block log directly. The extractor only reads
user/system messages, so pointing it at a raw blocks-*.jsonl file returns "" no
matter what the file contains — a test written that way would pass identically
before and after the fix, which is to say it would test nothing. The contaminated
transcript is the real path and the only one where the pre-fix run actually
returns the id.

What this does and does not gate: CI runs no pytest today (D#2443), so this file
gates nothing in the build. It is the evidence that the redaction works against
the real consumer rather than against a restatement of the redacted string's
shape.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hooks import sandbox  # noqa: E402
from hooks.sandbox_rules import Decision  # noqa: E402
from hooks.spawn_tag_redaction import TAG, redact_spawn_tags  # noqa: E402


def _load_extractor():
    """scripts/lib/ is not a package; load the real consumer by path.

    Deliberately the shipped module and not a copy of its regex — the whole
    acceptance here is "what does the consumer answer", and a local
    reimplementation would answer for itself.
    """
    spec = importlib.util.spec_from_file_location(
        "transcript_event_id",
        _REPO_ROOT / "scripts" / "lib" / "transcript_event_id.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extractor = _load_extractor()

# Built from the shared prefix so this file never carries a contiguous
# canonical id of its own — scripts/ci/no-planted-spawn-ids-guard.py would fail
# the build on it, correctly.
PLANTED_ID = "executor-1959-1785301265"
SPAWN_TAG_LINE = f"{TAG}{PLANTED_ID}"

# A realistic blocked Agent() prompt: the spawn tag sits near the top, well
# inside the 300-character truncation the writer applies.
ATTEMPTED_PROMPT = (
    "You are the executor for D#1959.\n"
    f"{SPAWN_TAG_LINE}\n"
    "Implement the redaction helper and open a PR.\n"
)


def _transcript_of_agent_reading(log_line: str, path: Path) -> Path:
    """Write the transcript an agent produces when it reads the block log.

    Shape A (real Claude Code): a user turn whose content is a list of blocks,
    with the file's bytes arriving inside a tool_result's `content`. That is
    exactly the shape scripts/lib/transcript_event_id.py was written for — the
    tag never appears in the agent's first message, only in the tool_result of
    it reading something.
    """
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


def _read_only_log_line(log_dir: Path) -> str:
    files = sorted(log_dir.glob("blocks-*.jsonl"))
    assert len(files) == 1, f"expected one daily log file, got {files}"
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1, f"expected one log line, got {lines}"
    return lines[0]


def test_the_unredacted_line_really_does_leak(tmp_path):
    """The negative control, and the reason the rest of this file is not
    theatre.

    This reconstructs the pre-fix behaviour — json.dumps with no scrub, which is
    literally what hooks/sandbox.py did at every one of its eight write sites
    before D#1957 — and shows the extractor recovering the planted id from an
    agent that merely read the log. Without this, "the extractor returns
    nothing" afterwards would be indistinguishable from an extractor that never
    looks at anything (D#1984).
    """
    pre_fix_line = json.dumps(
        {
            "tool": "Agent",
            "kind": "sandbox_block_agent_spawn",
            "decision": "block",
            "cwd": "/srv/example-checkout",
            "worktree_id": "executor-1959-p0",
            "attempted_target": ATTEMPTED_PROMPT[:300],
        }
    )
    transcript = _transcript_of_agent_reading(pre_fix_line, tmp_path / "pre.jsonl")

    assert extractor.extract_event_id(str(transcript)) == PLANTED_ID


def test_agent_spawn_block_event_is_not_extractable(tmp_path, monkeypatch):
    """Item 6: write a real block event through the hook's own logging path, let
    an agent read it, and ask the real extractor what it finds."""
    monkeypatch.setattr(sandbox, "_TELEMETRY_DIR", tmp_path / "hook-events")

    sandbox._write_agent_spawn_block_event(
        cwd="/srv/example-checkout",
        worktree_id="executor-1959-p0",
        reason="agent spawn forbidden below the top level",
        args={"prompt": ATTEMPTED_PROMPT},
    )

    line = _read_only_log_line(tmp_path / "hook-events")
    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")

    assert extractor.extract_event_id(str(transcript)) == ""


def test_redacted_line_keeps_command_cwd_and_agent(tmp_path, monkeypatch):
    """Item 7: the audit value survives.

    A redaction that removed the blocked command, the working directory or the
    agent's identity would satisfy the extractor test above and destroy the
    reason the log exists. Both properties are asserted from the same written
    line, in the same run, so neither can be satisfied at the other's expense.
    """
    monkeypatch.setattr(sandbox, "_TELEMETRY_DIR", tmp_path / "hook-events")

    sandbox._write_telemetry(
        tool="Bash",
        decision=Decision(allow=False, reason="path write outside worktree"),
        cwd="/srv/example-checkout/.claude/worktrees/agent-x",
        command_or_path=f"echo '{SPAWN_TAG_LINE}' >> /srv/example-checkout/notes.md",
        worktree_id="executor-1959-p0",
    )

    line = _read_only_log_line(tmp_path / "hook-events")
    entry = json.loads(line)  # still valid JSON after redaction

    assert entry["cwd"] == "/srv/example-checkout/.claude/worktrees/agent-x"
    assert entry["worktree_id"] == "executor-1959-p0"
    assert entry["reason"] == "path write outside worktree"
    assert entry["tool"] == "Bash"
    # The command is recognisable: everything but the id itself is intact.
    assert entry["command_or_path"].startswith("echo '")
    assert "/srv/example-checkout/notes.md" in entry["command_or_path"]
    assert TAG in entry["command_or_path"]
    # ...and the id is gone.
    assert PLANTED_ID not in line

    transcript = _transcript_of_agent_reading(line, tmp_path / "post.jsonl")
    assert extractor.extract_event_id(str(transcript)) == ""


# The set of hooks/sandbox.py functions expected to call _telemetry_line(entry)
# exactly once. Checked in by name, not by count: a writer added or removed
# should make a failure name *which* function changed, not just report that
# some number moved.
EXPECTED_TELEMETRY_WRITER_FUNCTIONS = {
    "_write_telemetry",
    "_write_claude_spawn_block_event",
    "_write_agent_spawn_block_event",
    "_write_gh_api_mutation_block_event",
    "_write_gh_api_mutation_allow_event",
    "_write_foreign_defer_event",
    "_write_archive_protocol_warning_event",
    "_write_head_flip_warning_event",
    "_write_unclassified_command_event",
}


def _extract_telemetry_writer_functions(src: str) -> set[str]:
    """Every top-level function whose body calls `_telemetry_line(entry)`,
    named by function rather than counted — mirrors the call-site extraction
    pattern used for the merge-gate labels in test_gate_label_drift.py."""
    pieces = re.split(r"^def (\w+)", src, flags=re.MULTILINE)
    names_and_bodies = zip(pieces[1::2], pieces[2::2])
    return {name for name, body in names_and_bodies if "_telemetry_line(entry)" in body}


def test_every_telemetry_writer_goes_through_the_scrubber():
    """The property is structural, not per-field.

    hooks/sandbox.py's telemetry writers carry agent-authored text under six
    different keys. Scrubbing per field would be one chance per writer to
    miss one and another on every writer added later, so all of them
    serialise through _telemetry_line(). This asserts that no direct
    json.dumps(entry) call has crept back in, and that the set of writer
    functions doing so matches a checked-in expected set — so a failure
    names the writer that was added or removed, not just a count that moved.
    """
    src = (_REPO_ROOT / "hooks" / "sandbox.py").read_text()
    assert "json.dumps(entry)" not in src.replace(
        "redact_spawn_tags(json.dumps(entry))", ""
    ), "a telemetry writer serialises an entry without going through _telemetry_line()"
    writers = _extract_telemetry_writer_functions(src)
    assert writers == EXPECTED_TELEMETRY_WRITER_FUNCTIONS, (
        f"telemetry writer functions changed: found {writers}, expected "
        f"{EXPECTED_TELEMETRY_WRITER_FUNCTIONS} — a writer calling "
        f"_telemetry_line(entry) was added or removed in hooks/sandbox.py "
        f"without updating this list"
    )


def test_redaction_leaves_untagged_text_untouched():
    """Over-blocking is the worse failure (CLAUDE.md). Text with no spawn tag
    must come back byte-identical, including text that merely mentions the tag
    without a canonical id after it — that is prose, and prose is not
    extractable."""
    for text in (
        "git rm -r --cached backend/",
        "",
        f"the {TAG} tag is discussed here but no id follows it",
        f"{TAG}not-canonical",
        f"{TAG}executor-1959-1234",  # timestamp too short to be canonical
    ):
        assert redact_spawn_tags(text) == text
