"""tests/test_backfill_agent_runs_event_id.py — parity tests proving
scripts/cron/backfill-agent-runs.sh takes its spawn id from the shared
extractor at scripts/lib/transcript_event_id.py (D#1953, Phase 3 of D#1784).

Before this change the script carried its own copy of the tag regex. That copy
differed from the shared module in three ways, each of which mis-attributes
telemetry:

  1. It read only blocks carrying a "text" key. The tag reaches an agent as a
     file reference and materialises inside the `tool_result` of the agent
     reading its own prompt file, whose payload lives under "content" — so the
     copy could not see the delivery path that actually carries the tag.
  2. It did no shape validation, so a prose mention of the tag in backticks
     yielded a lone backtick as the "id".
  3. `if m: hook_event_id = m.group(1)` sat inside the per-line loop with no
     `break`, making it *last*-match-wins across the whole file. Any late
     mention of the tag overwrote the correct id.

`complete_run()` upserts on agent_id, so each of those lands one agent's end_ts
and token usage on another agent's row.

Non-tautology: `_old_backfill_extract` below reproduces the deleted logic
verbatim (rev 1105889b, scripts/cron/backfill-agent-runs.sh:155-175). Every
fixture asserts what the OLD code returned as well as what the new path does,
so a fixture that exercises nothing fails visibly instead of passing quietly.

The end-to-end tests run the real shell script once against a throwaway
sessions dir and a throwaway duckdb (via STATS_DB_PATH), never real stats.

Tag literal: assembled from two fragments so this file's own source never
carries the tag prefix immediately followed by a canonical-shaped id — see
tests/test_no_planted_spawn_ids.py for why that matters.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "cron" / "backfill-agent-runs.sh"
_SHARED_MODULE = _REPO_ROOT / "scripts" / "lib" / "transcript_event_id.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(_REPO_ROOT))

from transcript_event_id import _MAX_EVENT_ID_LEN  # noqa: E402

duckdb = pytest.importorskip("duckdb")

# Split so this file's source never plants a canonical id. See module docstring.
_TAG = "hook_event_" "id="
_OLD_PATTERN = re.compile(_TAG + r"([^\s\n]+)")

# ---------------------------------------------------------------------------
# Fixture ids. Roles are deliberately fictional ("fixture*") so a stray write
# can never collide with a real agent_run row.
# ---------------------------------------------------------------------------
ID_TEXT = "fixturetext-1953-1787000001"
ID_TOOLRESULT = "fixturetool-1953-1787000002"
ID_LATE_MENTION = "fixturelate-1953-1787000005"

# Rejected by the shared module, accepted verbatim by the old regex.
BAD_SHORT_TS = "fixtureshort-1953-12345678"  # 8-digit ts; the floor is 9
BAD_ROLE = "fixture_role9-1953-1787000003"  # role is not [a-z]+(-[a-z]+)*
BAD_TOO_LONG = "b" * (_MAX_EVENT_ID_LEN + 5) + "-1953-1787000004"
BAD_PROSE = "`"  # bare backtick from a prose mention
BAD_LATE_TOKEN = "<redacted>"  # the trailing mention in the late_mention fixture

_USAGE = {
    "input_tokens": 1234,
    "output_tokens": 56,
    "cache_read_input_tokens": 78,
    "cache_creation_input_tokens": 9,
}


def _text_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _tool_result_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": text}
                ],
            },
        }
    )


def _assistant_usage_line() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [], "usage": _USAGE},
        }
    )


def _transcripts() -> dict[str, list[str]]:
    """name -> transcript lines. Every tag is built as _TAG + <id> at runtime."""
    return {
        "accept_text": [
            _text_line("spawn prompt preamble\n" + _TAG + ID_TEXT),
            _assistant_usage_line(),
        ],
        "accept_tool_result": [
            _text_line("Read the prompt file at /tmp/prompt.txt"),
            _tool_result_line("brief body\n" + _TAG + ID_TOOLRESULT + "\n"),
            _assistant_usage_line(),
        ],
        "late_mention": [
            _text_line("spawn prompt preamble\n" + _TAG + ID_LATE_MENTION),
            _text_line("later note about " + _TAG + BAD_LATE_TOKEN + " in prose"),
            _assistant_usage_line(),
        ],
        "reject_short_ts": [
            _text_line(_TAG + BAD_SHORT_TS),
            _assistant_usage_line(),
        ],
        "reject_bad_role": [
            _text_line(_TAG + BAD_ROLE),
            _assistant_usage_line(),
        ],
        "reject_too_long": [
            _text_line(_TAG + BAD_TOO_LONG),
            _assistant_usage_line(),
        ],
        "reject_prose_only": [
            _text_line("the tag is written `" + _TAG + "` in the docs"),
            _assistant_usage_line(),
        ],
    }


def _old_backfill_extract(path: Path) -> str:
    """Verbatim reproduction of the deleted backfill logic (rev 1105889b,
    scripts/cron/backfill-agent-runs.sh:155-175): text-key blocks only, no
    shape validation, and no `break` — so the LAST match in the file wins."""
    hook_event_id = None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role", "")
            content = obj.get("content", "")
            if not role and isinstance(obj.get("message"), dict):
                msg = obj["message"]
                role = msg.get("role", "")
                content = msg.get("content", "")
            if role in ("user", "system"):
                text = (
                    content
                    if isinstance(content, str)
                    else "\n".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                )
                m = _OLD_PATTERN.search(text)
                if m:
                    hook_event_id = m.group(1)
    return hook_event_id or ""


# ---------------------------------------------------------------------------
# End-to-end harness: seed a throwaway duckdb, run the real script once.
# ---------------------------------------------------------------------------

# Every id the run could plausibly attribute to, each seeded with a NULL end_ts
# so "was this row touched?" is a clean yes/no.
_SEED_IDS = [
    ID_TEXT,
    ID_TOOLRESULT,
    ID_LATE_MENTION,
    BAD_SHORT_TS,
    BAD_ROLE,
    BAD_TOO_LONG,
    BAD_PROSE,
    BAD_LATE_TOKEN,
]


@pytest.fixture(scope="module")
def backfill_run(tmp_path_factory):
    """Write the fixture corpus, seed the DB, run the script once, return rows."""
    root = tmp_path_factory.mktemp("afx1953")
    sessions = root / "sessions"
    sessions.mkdir()
    paths = {}
    for name, lines in _transcripts().items():
        tpath = sessions / f"{name}.jsonl"
        tpath.write_text("\n".join(lines) + "\n")
        paths[name] = tpath

    db_path = root / "stats.duckdb"
    state_dir = root / "state"
    state_dir.mkdir()

    from backend.agent_run_tracker import _ensure_schema

    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        for agent_id in _SEED_IDS:
            conn.execute(
                "INSERT INTO agent_run (agent_id, role, discussion, start_ts, end_ts) "
                "VALUES (?, 'fixture', 1953, TIMESTAMPTZ '2026-08-18 00:00:00+00', NULL)",
                [agent_id],
            )
    finally:
        conn.close()

    env = dict(os.environ)
    # _db_path() honours STATS_DB_PATH first, so this never touches real stats.
    env["STATS_DB_PATH"] = str(db_path)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--sessions-dir", str(sessions)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"script failed:\n{proc.stdout}\n{proc.stderr}"

    conn = duckdb.connect(str(db_path))
    try:
        rows = {
            r[0]: {"end_ts": r[1], "input_tok": r[2], "output_tok": r[3]}
            for r in conn.execute(
                "SELECT agent_id, end_ts, input_tok, output_tok FROM agent_run"
            ).fetchall()
        }
    finally:
        conn.close()
    return {"rows": rows, "paths": paths, "proc": proc}


# ---------------------------------------------------------------------------
# The duplicate implementation is gone and the docstring is honest again.
# ---------------------------------------------------------------------------


def test_no_hand_rolled_tag_regex_remains():
    src = _SCRIPT.read_text()
    assert _TAG not in src, (
        "backfill still contains a literal spawn-tag pattern; extraction must "
        "route through scripts/lib/transcript_event_id.py"
    )
    assert "HOOK_ID_PATTERN" not in src
    assert "re.compile" not in src
    assert "re.search" not in src


def test_extraction_routes_through_shared_module():
    src = _SCRIPT.read_text()
    assert "from transcript_event_id import extract_event_id" in src
    assert "extract_event_id(str(tpath))" in src


def test_shared_module_docstring_no_longer_defers_phase_3():
    phase3 = [ln for ln in _SHARED_MODULE.read_text().splitlines() if "Phase 3" in ln]
    assert phase3, "the Phase 3 call-site line vanished from the docstring"
    assert all("not in this PR" not in ln for ln in phase3)
    assert any("1953" in ln for ln in phase3)


# ---------------------------------------------------------------------------
# Accepted-id parity, including the tool_result delivery path.
# ---------------------------------------------------------------------------


def test_text_delivered_id_is_attributed(backfill_run):
    row = backfill_run["rows"][ID_TEXT]
    assert row["end_ts"] is not None
    assert row["input_tok"] == _USAGE["input_tokens"]
    assert row["output_tok"] == _USAGE["output_tokens"]


def test_tool_result_delivered_id_is_attributed(backfill_run):
    """The delivery path the old implementation could not read at all."""
    row = backfill_run["rows"][ID_TOOLRESULT]
    assert row["end_ts"] is not None
    assert row["input_tok"] == _USAGE["input_tokens"]
    assert row["output_tok"] == _USAGE["output_tokens"]


def test_tool_result_fixture_is_not_tautological(backfill_run):
    """Old logic recovers nothing from a tool_result payload."""
    assert _old_backfill_extract(backfill_run["paths"]["accept_tool_result"]) == ""


# ---------------------------------------------------------------------------
# The second mis-attribution mechanism: last-match-wins across the file.
# ---------------------------------------------------------------------------


def test_late_prose_mention_does_not_override_the_real_id(backfill_run):
    rows = backfill_run["rows"]
    assert rows[ID_LATE_MENTION]["end_ts"] is not None
    assert rows[ID_LATE_MENTION]["input_tok"] == _USAGE["input_tokens"]
    assert rows[BAD_LATE_TOKEN]["end_ts"] is None


def test_late_mention_fixture_is_not_tautological(backfill_run):
    """The old loop had no `break`, so the trailing prose mention won."""
    assert _old_backfill_extract(backfill_run["paths"]["late_mention"]) == BAD_LATE_TOKEN


# ---------------------------------------------------------------------------
# Rejected-id parity: the shared module's "no" is backfill's "no".
# ---------------------------------------------------------------------------

_REJECTIONS = [
    ("reject_short_ts", BAD_SHORT_TS),
    ("reject_bad_role", BAD_ROLE),
    ("reject_too_long", BAD_TOO_LONG),
    ("reject_prose_only", BAD_PROSE),
]


@pytest.mark.parametrize("fixture_name,bad_id", _REJECTIONS)
def test_rejected_ids_are_not_attributed(backfill_run, fixture_name, bad_id):
    assert backfill_run["rows"][bad_id]["end_ts"] is None, (
        f"{fixture_name}: backfill attributed telemetry to an id the shared "
        f"module rejects ({bad_id!r})"
    )


@pytest.mark.parametrize("fixture_name,bad_id", _REJECTIONS)
def test_rejection_fixtures_are_not_tautological(backfill_run, fixture_name, bad_id):
    """The old regex accepted every one of these verbatim — which is what made
    them mis-attribution vectors rather than harmless no-ops."""
    assert _old_backfill_extract(backfill_run["paths"][fixture_name]) == bad_id


# ---------------------------------------------------------------------------
# The regression guard: a stricter extractor that returns nothing would pass
# every negative test above, so pin the positive count explicitly.
# ---------------------------------------------------------------------------


def test_attribution_is_preserved_not_just_tightened(backfill_run):
    attributed = {i for i, r in backfill_run["rows"].items() if r["end_ts"] is not None}
    assert attributed == {ID_TEXT, ID_TOOLRESULT, ID_LATE_MENTION}, (
        f"expected exactly the three canonical ids to be attributed, got {attributed}"
    )
