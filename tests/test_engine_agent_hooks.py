"""EB-2b: agent_started (PreToolUse hook) and agent_stopped (SubagentStop hook) engine events.
Fixtures under tests/fixtures/engine_agent_hooks are SYNTHETIC (hand-written), not captured."""
import json
import os
import shutil
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIX = os.path.join(ROOT, "tests", "fixtures", "engine_agent_hooks")
START = os.path.join(ROOT, "scripts", "hooks", "engine-agent-start.sh")
LINKED = os.path.join(ROOT, "scripts", "hooks", "engine-agent-linked.sh")
STOP = os.path.join(ROOT, "scripts", "subagent-stop-hook.sh")
ENVELOPE = {"v", "seq", "ts", "engine_instance_id", "kind"}
STUB_MSG = "SECRETLAST <!-- AGENT_OUTPUT -->\n```json\n" + json.dumps(
    {"agent": "executor", "discussion": 842, "pr": 7, "verdict": "needs-fix"}) + "\n```\n<!-- /AGENT_OUTPUT -->"


def payload(**tool_input):
    p = json.load(open(os.path.join(FIX, "pretooluse-agent.synthetic.json")))
    p["tool_input"].update(tool_input)
    return p


def base_env(tmp_path, extra):
    e = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    e["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_path / "state")
    e.update(extra)
    return e


def run_start(tmp_path, stdin, **env):
    return subprocess.run(["bash", START], input=stdin, env=base_env(tmp_path, env),
                          capture_output=True, text=True)


def events(tmp_path):
    p = tmp_path / "state" / "engine-events.jsonl"
    return [json.loads(x) for x in open(p).read().splitlines()] if p.exists() else []


def stop_setup(tmp_path, transcript=True):
    d = tmp_path / "sess"
    (d / "tasks").mkdir(parents=True)
    if transcript:
        shutil.copy(os.path.join(FIX, "subagent-transcript.synthetic.jsonl"), d / "tasks" / "agentsyn1.jsonl")
    (d / "parent.jsonl").write_text("")
    return d


def run_stop(tmp_path, d, **env):
    stdin = json.dumps({"hook_event_name": "SubagentStop", "session_id": "synsess", "agent_id": "agentsyn1",
                        "agent_type": "executor", "transcript_path": str(d / "parent.jsonl"),
                        "last_assistant_message": STUB_MSG})
    env.update(SUBAGENT_STOP_DRY_RUN="1", SUBAGENT_STOP_ARGS_FILE=str(tmp_path / "args.json"))
    return subprocess.run(["bash", STOP], input=stdin, env=base_env(tmp_path, env),
                          capture_output=True, text=True)


def test_started_emits_exactly_the_agreed_keys_and_no_text(tmp_path):
    r = run_start(tmp_path, json.dumps(payload()))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    (ev,) = events(tmp_path)
    assert set(ev) - ENVELOPE == {"spawn_id", "role", "background", "isolation", "discussion", "model"}
    assert (ev["kind"], ev["spawn_id"], ev["role"], ev["background"], ev["isolation"], ev["discussion"],
            ev["model"]) == ("agent_started", "toolu_01SyntheticSpawn0001", "executor", True, "worktree",
                             4321, "sonnet")
    raw = open(tmp_path / "state" / "engine-events.jsonl").read()
    assert "SECRET" not in raw and "widget" not in raw and "parent" not in ev


def test_started_defaults_and_unknown_role(tmp_path):
    p = payload(subagent_type="mystery-role", prompt="no number here")
    for k in ("model", "isolation", "run_in_background"):
        p["tool_input"].pop(k)
    assert run_start(tmp_path, json.dumps(p)).returncode == 0
    (ev,) = events(tmp_path)
    assert (ev["role"], ev["background"], ev["isolation"]) == ("other", False, "none")
    assert "discussion" not in ev and "model" not in ev


def test_discussion_word_form_and_bad_model_dropped(tmp_path):
    run_start(tmp_path, json.dumps(payload(prompt="Please see Discussion #77", model="Not A Model!")))
    (ev,) = events(tmp_path)
    assert ev["discussion"] == 77 and "model" not in ev


def test_started_ignores_other_tools_and_garbage(tmp_path):
    p = payload()
    p["tool_name"] = "Bash"
    for stdin in (json.dumps(p), "", "not json", "[1,2]"):
        r = run_start(tmp_path, stdin)
        assert (r.returncode, r.stdout) == (0, "")
    assert events(tmp_path) == []


def test_start_hook_never_blocks_when_the_emitter_fails(tmp_path):
    r = run_start(tmp_path, json.dumps(payload()), ENGINE_EMIT_FORCE_FAIL="1")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert events(tmp_path) == []


def test_stopped_carries_usage_model_and_mode_but_no_message_text(tmp_path):
    assert run_stop(tmp_path, stop_setup(tmp_path)).returncode == 0
    (ev,) = events(tmp_path)
    assert set(ev) - ENVELOPE == {"agent_id", "role", "verdict", "discussion", "pr", "credential_mode",
                                  "model", "usage"}
    assert (ev["kind"], ev["agent_id"], ev["role"], ev["verdict"], ev["discussion"], ev["pr"]) == (
        "agent_stopped", "agentsyn1", "executor", "needs_fix", 842, 7)
    assert ev["credential_mode"] == "subscription" and ev["model"] == "claude-sonnet-5-5"
    assert ev["usage"] == {"input": 150, "output": 30, "cache_read": 700, "cache_write": 7}
    assert "SECRETLAST" not in open(tmp_path / "state" / "engine-events.jsonl").read()


def test_stopped_api_key_mode(tmp_path):
    run_stop(tmp_path, stop_setup(tmp_path), ANTHROPIC_API_KEY="sk-synthetic")
    assert events(tmp_path)[0]["credential_mode"] == "api_key"


def test_stopped_omits_usage_and_model_when_the_transcript_is_unreadable(tmp_path):
    assert run_stop(tmp_path, stop_setup(tmp_path, transcript=False)).returncode == 0
    (ev,) = events(tmp_path)
    assert "usage" not in ev and "model" not in ev


def test_stopped_omits_usage_when_every_total_is_zero(tmp_path):
    d = stop_setup(tmp_path, transcript=False)
    (d / "tasks" / "agentsyn1.jsonl").write_text(json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 0}}}) + "\n")
    run_stop(tmp_path, d)
    assert "usage" not in events(tmp_path)[0]


def test_a_dry_run_without_its_own_state_dir_writes_nothing(tmp_path):
    d = stop_setup(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "AUTONOMOUS_TEAM_STATE_DIR"}
    env.update(HOME=str(home), SUBAGENT_STOP_DRY_RUN="1", SUBAGENT_STOP_ARGS_FILE=str(tmp_path / "a.json"))
    stdin = json.dumps({"session_id": "s", "agent_id": "agentsyn1", "agent_type": "executor",
                        "transcript_path": str(d / "parent.jsonl"), "last_assistant_message": STUB_MSG})
    assert subprocess.run(["bash", STOP], input=stdin, env=env, capture_output=True, text=True).returncode == 0
    assert not (home / ".autonomous-forever-state").exists()


def test_stop_hook_still_finishes_when_the_emitter_fails(tmp_path):
    r = run_stop(tmp_path, stop_setup(tmp_path), ENGINE_EMIT_FORCE_FAIL="1")
    assert r.returncode == 0
    assert events(tmp_path) == []
    assert os.path.exists(tmp_path / "args.json")  # the hook's own work still ran


def post_payload(**resp):
    p = json.load(open(os.path.join(FIX, "posttooluse-agent.synthetic.json")))
    p["tool_response"].update(resp)
    return p


def run_linked(tmp_path, stdin, **env):
    return subprocess.run(["bash", LINKED], input=stdin, env=base_env(tmp_path, env),
                          capture_output=True, text=True)


def test_linked_ties_the_spawn_id_to_the_agent_id_and_nothing_else(tmp_path):
    r = run_linked(tmp_path, json.dumps(post_payload()))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    (ev,) = events(tmp_path)
    assert set(ev) - ENVELOPE == {"spawn_id", "agent_id"}
    assert (ev["kind"], ev["spawn_id"], ev["agent_id"]) == ("agent_linked", "toolu_01SyntheticSpawn0001", "agentsyn1")
    assert "SECRET" not in open(tmp_path / "state" / "engine-events.jsonl").read()


def test_linked_emits_nothing_without_an_agent_id(tmp_path):
    p = post_payload()
    p["tool_response"] = {"status": "completed", "content": "SECRETRESULT"}
    q = post_payload(agentId="not a valid id!")
    r2 = post_payload()
    r2["tool_response"] = "plain string response"
    r3 = post_payload()
    r3["tool_name"] = "Bash"
    for payload_ in (p, q, r2, r3):
        assert run_linked(tmp_path, json.dumps(payload_)).returncode == 0
    for stdin in ("", "not json", "[1]"):
        assert run_linked(tmp_path, stdin).stdout == ""
    assert events(tmp_path) == []


def test_linked_hook_never_blocks_when_the_emitter_fails(tmp_path):
    r = run_linked(tmp_path, json.dumps(post_payload()), ENGINE_EMIT_FORCE_FAIL="1")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert events(tmp_path) == []


def test_post_tool_use_hook_is_registered_for_agent_and_task():
    cfg = json.load(open(os.path.join(ROOT, ".claude", "settings.json")))
    (entry,) = [e for e in cfg["hooks"]["PostToolUse"] if "engine-agent-linked.sh" in json.dumps(e)]
    assert entry["matcher"] == "Agent|Task"
