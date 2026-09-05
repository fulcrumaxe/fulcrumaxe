"""tests/test_transcript_event_id.py — unit tests for
scripts/lib/transcript_event_id.py

D#1784 Phase 2. Covers the five cases named in acceptance criterion 12:
  1. text block                       (baseline — recovered by the old logic too)
  2. tool_result with str payload     (regression case — NOT recovered by old logic)
  3. tool_result with list-of-blocks  (regression case — NOT recovered by old logic)
  4. no tag present                   (must return "" / print nothing, exit 0)
  5. malformed JSON line              (must not raise; line is skipped)

Cases 2 and 3 are the ones that exercise the tool_result walk.
`_old_text_only_extract` below is a verbatim reproduction of the pre-fix logic
at scripts/subagent-stop-hook.sh:383-387 (only content blocks carrying a "text"
key are searched). Each of those two tests asserts the old logic returns "" on
the same fixture where the new extractor succeeds — proving the fixture is not
tautological and the test would fail if transcript_event_id.py were reverted to
the old logic.

The second group of tests covers shape validation. `_unvalidated_extract`
reproduces the intermediate behaviour (tool_result-aware, but returning the
first `[^\\s\\n]+` match verbatim), which is what turned an orphaned-id bug into
a colliding-id bug: prose mentions of the tag matched ahead of the real tag and
yielded a bare backtick, and `complete_run` upserts on agent_id so every one of
those collided onto a single row. Those tests assert the unvalidated logic
returns garbage on the same fixture where the validated extractor returns the
genuine id.

Note the tag literal is assembled from `_TAG` rather than written out inline.
This test file is itself read by agents, and a source line carrying the tag
prefix immediately followed by a canonical id would plant that id in the
reading agent's transcript — the exact contamination under test here.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from transcript_event_id import extract_event_id  # noqa: E402

_MODULE_PATH = _REPO_ROOT / "scripts" / "lib" / "transcript_event_id.py"

# Split so this file's source never contains the prefix adjacent to a
# canonical id. See module docstring above.
_TAG = "hook_event_" "id="
_PAT = re.compile(_TAG + r"([^\s\n]+)")


def _old_text_only_extract(path):
    """Verbatim reproduction of the pre-fix logic (rev efb4929,
    scripts/subagent-stop-hook.sh:383-387): only content blocks carrying a
    "text" key are searched. A tool_result block's payload lives under
    "content", so this never finds a tag delivered that way."""
    return _reference_extract(path, text_only=True)


def _unvalidated_extract(path):
    """Reproduction of the intermediate logic (rev e86fc4b): tool_result-aware,
    but the first `[^\\s\\n]+` match is returned verbatim with no shape check."""
    return _reference_extract(path, text_only=False)


def _reference_extract(path, text_only):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role", "")
            if not role and isinstance(obj.get("message"), dict):
                role = obj["message"].get("role", "")
            if role not in ("user", "system"):
                continue
            content = obj.get("content", "")
            if not content and isinstance(obj.get("message"), dict):
                content = obj["message"].get("content", "")
            if isinstance(content, list):
                chunks = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("text"):
                        chunks.append(b["text"])
                    if text_only:
                        continue
                    inner = b.get("content")
                    if isinstance(inner, str):
                        chunks.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and sub.get("text"):
                                chunks.append(sub["text"])
                text = "\n".join(chunks)
            else:
                text = str(content)
            m = _PAT.search(text)
            if m:
                return m.group(1)
    return ""


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


# ---------------------------------------------------------------------------
# Case 1: text block — baseline, recovered by both old and new logic
# ---------------------------------------------------------------------------

def test_text_block_recovers_tag(tmp_path):
    t = tmp_path / "t1.jsonl"
    _write_jsonl(t, [
        _user([{"type": "text", "text": f"Implement the fix.\n\n{_TAG}executor-42-1785301265"}]),
    ])
    assert extract_event_id(str(t)) == "executor-42-1785301265"


# ---------------------------------------------------------------------------
# Case 2: tool_result with str payload — the actual D#1784 regression case
# ---------------------------------------------------------------------------

def test_tool_result_str_payload_recovers_tag(tmp_path):
    t = tmp_path / "t2.jsonl"
    _write_jsonl(t, [
        # The spawn prompt itself, delivered as a file reference — no tag here,
        # exactly like a real spawn.
        _user("Read your prompt file and begin."),
        # tool_result of the agent reading its own prompt file: the tag is
        # inside the str payload under "content", not under "text".
        _user([
            {"type": "tool_result", "tool_use_id": "t1",
             "content": f"...assembled prompt...\n{_TAG}code-reviewer-1761-1785300997\n...more..."}
        ]),
    ])
    assert extract_event_id(str(t)) == "code-reviewer-1761-1785300997"
    # Non-tautological: the pre-fix (text-only) logic must NOT recover this.
    assert _old_text_only_extract(str(t)) == "", (
        "fixture is tautological — old text-only logic also recovered the tag"
    )


# ---------------------------------------------------------------------------
# Case 3: tool_result with list-of-blocks payload
# ---------------------------------------------------------------------------

def test_tool_result_list_payload_recovers_tag(tmp_path):
    t = tmp_path / "t3.jsonl"
    _write_jsonl(t, [
        _user([
            {"type": "tool_result", "tool_use_id": "t2", "content": [
                {"type": "text",
                 "text": f"file contents...\n{_TAG}security-reviewer-1778-1785303688\n..."}
            ]}
        ]),
    ])
    assert extract_event_id(str(t)) == "security-reviewer-1778-1785303688"
    assert _old_text_only_extract(str(t)) == "", (
        "fixture is tautological — old text-only logic also recovered the tag"
    )


# ---------------------------------------------------------------------------
# Case 4: no tag present — must return "" (CLI: exit 0, print nothing)
# ---------------------------------------------------------------------------

def test_no_tag_present_returns_empty(tmp_path):
    t = tmp_path / "t4.jsonl"
    _write_jsonl(t, [
        _user("Do the task, no tag here."),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done."}
        ]}},
    ])
    assert extract_event_id(str(t)) == ""


# ---------------------------------------------------------------------------
# Case 5: malformed JSON line — skipped, not raised
# ---------------------------------------------------------------------------

def test_malformed_json_line_is_skipped(tmp_path):
    t = tmp_path / "t5.jsonl"
    with open(t, "w") as f:
        f.write("{not valid json,,,\n")
        f.write(json.dumps(_user(f"{_TAG}executor-7-1785300000")) + "\n")
    assert extract_event_id(str(t)) == "executor-7-1785300000"


def test_malformed_json_only_file_returns_empty_without_raising(tmp_path):
    t = tmp_path / "t5b.jsonl"
    with open(t, "w") as f:
        f.write("not json at all\n")
        f.write('{"broken": \n')
    assert extract_event_id(str(t)) == ""


# ---------------------------------------------------------------------------
# Ordering: first canonical match in transcript order wins, not the last — a
# re-read of the prompt file or a nested quote must not substitute a different
# id once a genuine one has been seen.
# ---------------------------------------------------------------------------

def test_first_match_wins_not_last(tmp_path):
    t = tmp_path / "t6.jsonl"
    _write_jsonl(t, [
        _user(f"{_TAG}first-1-1785300000"),
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"{_TAG}second-2-1785300001"}]),
    ])
    assert extract_event_id(str(t)) == "first-1-1785300000"


def test_missing_file_returns_empty():
    assert extract_event_id("/nonexistent/path/does-not-exist.jsonl") == ""


# ---------------------------------------------------------------------------
# Shape validation: a prose mention of the tag must not win over the real tag.
#
# This is the defect that made PR #1802 worse than the orphaning it replaced:
# briefs and review comments discuss the tag in backticks, that prose lands in
# the agent's own transcript ahead of the genuine tag, and returning the first
# raw match yielded a bare backtick as the id. complete_run() upserts on
# agent_id, so every garbage id collided onto one row.
# ---------------------------------------------------------------------------

def test_prose_mention_first_does_not_beat_real_tag_later(tmp_path):
    """The load-bearing regression test: contaminating prose on line 1, genuine
    tag several messages later, in a tool_result payload."""
    t = tmp_path / "prose.jsonl"
    _write_jsonl(t, [
        # Line 1 — a brief telling the agent about the tag. The backtick
        # immediately after the prefix is what the old code returned as an id.
        _user(f"Your brief: the extractor scans transcripts for `{_TAG}`"
              f" and recovers the spawn id from it."),
        # Line 2 — more prose, this time an example id inside backticks.
        _user([{"type": "text",
                "text": f"For example a review comment might quote `{_TAG}`"
                        f" when explaining the format."}]),
        # Line 3 — the genuine tag, delivered the way a real spawn delivers it.
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"...assembled prompt...\n{_TAG}executor-1784-1785301265\n..."}]),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"
    # Non-tautological in the direction that matters: the unvalidated logic
    # (branch head e86fc4b) returns the backtick from line 1 instead.
    assert _unvalidated_extract(str(t)) == "`", (
        "fixture does not reproduce the defect — unvalidated logic should "
        "have returned a bare backtick"
    )


def test_prose_mention_alone_returns_empty(tmp_path):
    """A transcript with only prose mentions has no id to recover. Returning
    "" routes it to the fallback path; returning a backtick collides it onto
    another agent's row."""
    t = tmp_path / "prose_only.jsonl"
    _write_jsonl(t, [
        _user(f"The hook scans for `{_TAG}` in the transcript."),
    ])
    assert extract_event_id(str(t)) == ""
    assert _unvalidated_extract(str(t)) == "`"


def test_non_canonical_third_segment_rejected(tmp_path):
    """Seen in the live corpus: `exec-1761-round3`. Third segment is not a
    unix timestamp, so it is not an id spawn-agent.sh ever wrote."""
    t = tmp_path / "notts.jsonl"
    _write_jsonl(t, [
        _user(f"{_TAG}exec-1761-round3"),
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"{_TAG}executor-1761-1785300997"}]),
    ])
    assert extract_event_id(str(t)) == "executor-1761-1785300997"
    assert _unvalidated_extract(str(t)) == "exec-1761-round3"


def test_hook_event_init_stdout_id_rejected(tmp_path):
    """hook-event.sh:143 echoes the prefix with a sha16 or uuid4 id. Any agent
    running a script that calls it picks that up; it is not a spawn id."""
    t = tmp_path / "hookinit.jsonl"
    _write_jsonl(t, [
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"{_TAG}35fd883e436ebf6d\n"}]),
        _user([{"type": "tool_result", "tool_use_id": "t2",
                "content": f"{_TAG}c7c2a0de-4a3f-4c1e-9f2b-0e6d5a1b8c33\n"}]),
        _user([{"type": "tool_result", "tool_use_id": "t3",
                "content": f"{_TAG}code-reviewer-1784-1785303688"}]),
    ])
    assert extract_event_id(str(t)) == "code-reviewer-1784-1785303688"
    assert _unvalidated_extract(str(t)) == "35fd883e436ebf6d"


def test_backtick_wrapped_genuine_tag_yields_clean_id(tmp_path):
    """A genuine id wrapped in backticks must not come back with the delimiter
    glued on — the capture group is the canonical shape, not "everything up to
    whitespace"."""
    t = tmp_path / "wrapped.jsonl"
    _write_jsonl(t, [
        _user(f"spawned with `{_TAG}executor-1784-1785301265`"),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"
    assert _unvalidated_extract(str(t)) == "executor-1784-1785301265`"


def test_longer_token_is_not_truncated_into_valid_id(tmp_path):
    """Trailing word characters mean the token is not a canonical id; it must
    be rejected outright rather than silently trimmed to its valid prefix."""
    t = tmp_path / "trailing.jsonl"
    _write_jsonl(t, [
        _user(f"{_TAG}executor-1784-1785301265deadbeef"),
    ])
    assert extract_event_id(str(t)) == ""


def test_overlong_id_is_rejected(tmp_path):
    """Length bound: the role grammar is unbounded, and the text it runs over
    is agent-controlled."""
    t = tmp_path / "long.jsonl"
    long_role = "-".join(["role"] * 20)  # 99 chars before the numeric segments
    _write_jsonl(t, [
        _user(f"{_TAG}{long_role}-1784-1785301265"),
        _user(f"{_TAG}executor-1784-1785301265"),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"


def test_second_match_within_one_message_is_found(tmp_path):
    """Scanning must consider every match inside a single message, not just the
    first — prose and the genuine tag routinely share one tool_result."""
    t = tmp_path / "samemsg.jsonl"
    _write_jsonl(t, [
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"the tag is `{_TAG}` and here it is: "
                           f"{_TAG}executor-1784-1785301265"}]),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"


# ---------------------------------------------------------------------------
# Mutation-pinning tests (D#1807 criteria 11-13).
#
# scripts/lib/transcript_event_id.py is correct today in both cases below;
# these tests exist only because the 22 tests above do NOT discriminate
# between the correct implementation and either mutant — they pass either
# way, so a future accidental regression of either kind would ship silently.
# ---------------------------------------------------------------------------


def test_overlong_match_does_not_block_a_later_valid_match_in_same_message(tmp_path):
    """Mutant 1 pin: _scan_line's `for match in _PATTERN.finditer(text)` loop
    must keep scanning past a match that fails the `_MAX_EVENT_ID_LEN` check
    and return the next valid canonical id later in the SAME message.

    Why `test_second_match_within_one_message_is_found` above does not catch
    a regression here: in that fixture the only text position where the full
    `_PATTERN` can start a match at all is right before the genuine id — the
    backtick-wrapped mention earlier in the string never matches `_PATTERN`
    in the first place (a backtick isn't a valid `_ROLE` character), so
    `.finditer()` and a hypothetical single-match `.search()` return the
    identical first (and only) match. Now that the canonical shape lives
    inside the capture group, `search` skips prose for free, so that test
    passes under both the correct loop and a "first match only" mutant — it
    stopped discriminating BECAUSE the implementation improved, not because
    it was written wrong. This test forces two matches into one message:
    the first is well-formed enough to satisfy `_PATTERN` (and would
    therefore also satisfy a bare `.search()`), but it is rejected by the
    length floor at `_MAX_EVENT_ID_LEN`, so only a loop that keeps scanning
    past a length-rejected match reaches the genuine id after it.

    Verified live against a "finditer -> single .search() match" mutant
    (semantically what "replacing finditer with search" means, since a
    literal `sed -i 's/finditer/search/'` also breaks the `for match in`
    iteration, turning the whole scan into an exception each mutation
    testing run must independently confirm doesn't collapse to a
    different failure mode — see the PR description for both pasted runs):
    all 22 pre-existing tests here stay green under it, and only this test
    fails, going from 'executor-1784-1785301265' to ''.
    """
    t = tmp_path / "overlong_then_valid_same_message.jsonl"
    long_role = "-".join(["role"] * 20)  # matches _PATTERN; candidate exceeds _MAX_EVENT_ID_LEN
    _write_jsonl(t, [
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"{_TAG}{long_role}-1784-1785301265 and then "
                           f"{_TAG}executor-1784-1785301265"}]),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"


def test_short_timestamp_digit_count_rejected(tmp_path):
    """Mutant 2 pin: `_UNIX_TS`'s digit floor (`{9,12}`) must reject a
    timestamp segment shorter than 9 digits, e.g. the `pr-<number>-<single
    digit>` shape. Relaxing the quantifier to `{1,12}` admits this garbage
    class while leaving all 22 pre-existing tests green (every existing
    fixture uses a real ~10-digit unix timestamp, so none of them exercises
    the floor itself) — verified live: both before and after this specific
    one-character quantifier edit, `python3 -m pytest
    tests/test_transcript_event_id.py -q` reports 22 passed until this test
    is added, at which point the mutant run fails here and only here.

    This pin is more load-bearing than "reject garbage" suggests. There is a
    live file in the tree — backend/tests/test_prompt_builder.py, in
    test_hook_event_id_line — that contains the tag prefix immediately
    followed by "executor-42-9999" as an inline literal. That is a contiguous,
    otherwise-canonical adjacency; it is inert only because 9999 is four
    digits and both this module's `_UNIX_TS` floor and the repo sweep in
    tests/test_no_planted_spawn_ids.py require nine. Relax either quantifier
    and that file stops being an example and becomes a real plant, adopted by
    every agent whose transcript reads it. Do not lower the floor to
    accommodate a fixture; split the fixture's literal instead."""
    t = tmp_path / "short_ts.jsonl"
    _write_jsonl(t, [
        _user(f"{_TAG}pr-1784-5"),
    ])
    assert extract_event_id(str(t)) == ""


def test_nod_discussion_segment_accepted(tmp_path):
    """spawn-agent.sh:437 substitutes the literal "nod" when there is no
    discussion number."""
    t = tmp_path / "nod.jsonl"
    _write_jsonl(t, [
        _user(f"{_TAG}quality-sweep-nod-1785301265"),
    ])
    assert extract_event_id(str(t)) == "quality-sweep-nod-1785301265"


# ---------------------------------------------------------------------------
# Malformed block shapes must not abort the scan.
# ---------------------------------------------------------------------------

def test_non_string_text_value_does_not_abort_scan(tmp_path):
    """A block whose "text" is not a str used to raise TypeError out of the
    str.join in _candidate_text, which aborted the whole walk — one bad block
    early in a transcript silently suppressed a genuine tag later."""
    t = tmp_path / "badblock.jsonl"
    _write_jsonl(t, [
        _user([{"type": "text", "text": {"nested": "dict, not a string"}}]),
        _user([{"type": "text", "text": ["list", "not", "a", "string"]}]),
        _user([{"type": "tool_result", "tool_use_id": "t1",
                "content": f"{_TAG}executor-1784-1785301265"}]),
    ])
    assert extract_event_id(str(t)) == "executor-1784-1785301265"


def test_non_dict_json_line_is_skipped(tmp_path):
    t = tmp_path / "notdict.jsonl"
    with open(t, "w") as f:
        f.write("[1, 2, 3]\n")
        f.write('"a bare string line"\n')
        f.write(json.dumps(_user(f"{_TAG}executor-1784-1785301265")) + "\n")
    assert extract_event_id(str(t)) == "executor-1784-1785301265"


# ---------------------------------------------------------------------------
# CLI contract (criterion 5): prints the id and exits 0, or prints nothing
# and exits 0 when no tag is present.
# ---------------------------------------------------------------------------

def test_cli_prints_id_and_exits_zero(tmp_path):
    t = tmp_path / "cli1.jsonl"
    _write_jsonl(t, [_user(f"{_TAG}executor-9-1785300009")])
    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(t)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "executor-9-1785300009"


def test_cli_prints_nothing_and_exits_zero_when_no_tag(tmp_path):
    t = tmp_path / "cli2.jsonl"
    _write_jsonl(t, [_user("no tag here")])
    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(t)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_cli_prints_nothing_for_prose_only_transcript(tmp_path):
    """End-to-end shape of the fix at the boundary the hook actually calls:
    empty stdout means the hook takes its fallback path instead of keying the
    row to a backtick."""
    t = tmp_path / "cli3.jsonl"
    _write_jsonl(t, [_user(f"the hook scans for `{_TAG}` in the transcript")])
    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(t)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
