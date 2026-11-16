"""engine-team-log-poll.py against a stubbed `gh` and three team-log comment shapes."""
import hashlib
import json
import os
import stat
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
POLL = os.path.join(ROOT, "scripts", "engine-team-log-poll.py")

GH_STUB = """#!/usr/bin/env bash
# Serves comments from $GH_FIXTURE (a JSON list); never touches the network.
case "$*" in
  *"--slurp"*) python3 -c 'import json,sys; print(json.dumps([json.load(open(sys.argv[1]))]))' "$GH_FIXTURE" ;;
  *"issues/comments/"*)
    id="${@: -1}"; id="${id##*/}"
    python3 -c 'import json,sys
for c in json.load(open(sys.argv[1])):
    if str(c["id"]) == sys.argv[2]:
        print(json.dumps(c)); sys.exit(0)
sys.exit(1)' "$GH_FIXTURE" "$id" ;;
  *) exit 1 ;;
esac
"""

COMMENTS = [
    {"id": 101, "node_id": "IC_kwDOaaa101",
     "body": "[10:01] executor-3: started — implementing URL detection for D#12 (PR #40) SECRETWORD"},
    {"id": 102, "node_id": "IC_kwDOaaa102", "body": "[10:30] team-lead: spawning code-reviewer for PR #40"},
    {"id": 103, "node_id": "IC_kwDOaaa103", "body": "[11:00] team-lead: needs-boss — credential for D#12 expired"},
]


def setup(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)
    fixture = tmp_path / "comments.json"
    fixture.write_text(json.dumps(COMMENTS))
    state = tmp_path / "state"
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), GH_FIXTURE=str(fixture),
               AUTONOMOUS_TEAM_STATE_DIR=str(state), ENGINE_TEAM_LOG_REPO="o/r", ENGINE_TEAM_LOG_ISSUE="9")
    return env, fixture, state


def run(env):
    return subprocess.run(["python3", POLL], env=env, capture_output=True, text=True)


def events(state):
    return [json.loads(x) for x in open(state / "engine-events.jsonl").read().splitlines()]


def test_comment_shapes_become_closed_events_with_no_text(tmp_path):
    env, _, state = setup(tmp_path)
    assert run(env).returncode == 0
    raw = open(state / "engine-events.jsonl").read()
    assert "SECRETWORD" not in raw and "URL detection" not in raw and "credential for" not in raw
    ev = events(state)
    assert [e["kind"] for e in ev] == ["team_log_line", "team_log_line", "team_log_line", "ask_raised"]
    a, b, c, ask = ev
    assert (a["actor_role"], a["category"], a["discussion"], a["pr"]) == ("executor", "starting", 12, 40)
    assert (b["actor_role"], b["category"], b["pr"]) == ("team-lead", "spawning", 40)
    assert (c["actor_role"], c["category"]) == ("team-lead", "needs_boss")
    want = hashlib.sha256(b"IC_kwDOaaa103").hexdigest()[:16]
    assert c["line_id"] == ask["ask_id"] == want
    assert (ask["ask_kind"], ask["source"], ask["discussion"]) == ("credential", "team_log", 12)


def test_second_run_with_the_same_cursor_emits_nothing_and_leaves_the_cursor_alone(tmp_path):
    env, _, state = setup(tmp_path)
    run(env)
    cursor = state / "engine-team-log-cursor.json"
    before = (cursor.stat().st_mtime_ns, len(events(state)))
    assert run(env).returncode == 0
    assert (cursor.stat().st_mtime_ns, len(events(state))) == before
    assert stat.S_IMODE(cursor.stat().st_mode) == 0o600


def test_only_new_comments_are_emitted_later(tmp_path):
    env, fixture, state = setup(tmp_path)
    run(env)
    fixture.write_text(json.dumps(COMMENTS + [
        {"id": 104, "node_id": "IC_kwDOaaa104", "body": "[12:00] executor-3: merged PR #40"}]))
    run(env)
    last = events(state)[-1]
    assert (last["kind"], last["category"], last["pr"]) == ("team_log_line", "merged", 40)
    assert len(events(state)) == 5


def test_a_withdrawn_ask_is_cleared(tmp_path):
    env, fixture, state = setup(tmp_path)
    run(env)
    fixture.write_text(json.dumps(COMMENTS[:2]))
    assert run(env).returncode == 0
    last = events(state)[-1]
    assert last["kind"] == "ask_cleared" and last["how"] == "withdrawn"
    assert last["ask_id"] == hashlib.sha256(b"IC_kwDOaaa103").hexdigest()[:16]
    run(env)
    assert len(events(state)) == 5  # cleared once
