"""EB-10b: the cloud decide gate for engine spawns (pre-spawn-check.sh and the PreToolUse(Agent) hook) and
for Spec approvals (the project-manager outbox flip to SPEC_READY), against the strict fake decide socket
(testsupport/fake_cloud_decide.py). Every case runs in a scratch engine tree with its own state dir.
The hook payload fixture is SYNTHETIC (hand-written), as in tests/fixtures/engine_agent_hooks."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AUTONOMOUS_TEAM_STATE_DIR", tempfile.mkdtemp(prefix="fxgate-state-"))
from testsupport.fake_cloud_decide import FakeDecide  # noqa: E402
from backend import discussion_outbox as ob  # noqa: E402

PAYLOAD = os.path.join(ROOT, "tests", "fixtures", "engine_agent_hooks", "pretooluse-agent.synthetic.json")
LIBS = ("cloud_decide.py", "cloud_engine_gate.py", "cloud-spawn-gate.sh", "engine_agent_events.py", "engine_emit.py",
        "subagent_payload.py", "state-dir.sh", "hook-event.sh", "worktree-disk-guard.sh")
EVENT_ID = "executor-842-1763300000"
UNREACHABLE = "cloud unreachable; spawn refused (fail-closed)"
# mode -> (exit code, spawn_blocked reason | None)
MATRIX = {"act": (0, None), "announce": (0, None), "act_approved": (0, None),
          "ask": (3, "cloud_denied"), "ask_declined": (3, "cloud_denied"),
          "halted": (4, "halted"), "halted_act": (4, "halted"),
          "http_500": (5, "cloud_unreachable"), "http_403": (5, "cloud_unreachable"),
          "not_json": (5, "cloud_unreachable"), "extra_key": (5, "cloud_unreachable"),
          "truncated": (5, "cloud_unreachable"), "close_early": (5, "cloud_unreachable")}


class World:
    def __init__(self, tmp_path):
        self.root = str(tmp_path / "engine")
        self.state = str(tmp_path / "state")
        self.run_dir = tempfile.mkdtemp(prefix="fxg")
        os.chmod(self.run_dir, 0o700)
        self.sock = os.path.join(self.run_dir, "fx-runner", "engine-bridge.sock")
        self.tlog = str(tmp_path / "teamlog.txt")
        for d in ("scripts/lib", "scripts/hooks", ".autonomous-team", "schemas", "backend"):
            os.makedirs(os.path.join(self.root, d))
        os.makedirs(self.state, mode=0o700)
        for f in LIBS:
            shutil.copy(os.path.join(ROOT, "scripts/lib", f), self.root + "/scripts/lib/")
        for f in ("engine-emit.sh", "pre-spawn-check.sh", "agent-feed-append.sh", "hooks/engine-agent-start.sh"):
            shutil.copy(os.path.join(ROOT, "scripts", f), self.root + "/scripts/" + f)
        shutil.copy(os.path.join(ROOT, "schemas/engine-events.v1.json"), self.root + "/schemas/")
        shutil.copy(os.path.join(ROOT, "backend/agent_feed.py"), self.root + "/backend/")
        with open(self.root + "/scripts/lib/external_intake_gate.py", "w") as f:
            f.write('import json\nprint(json.dumps({"blocked": False, "reason": "stub"}))\n')
        with open(self.root + "/scripts/rotate-team-log.sh", "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%%s\\n" "$2" >> "%s"\n' % self.tlog)
        os.chmod(self.root + "/scripts/rotate-team-log.sh", 0o755)
        self.server = None
        self.write_config({"operate": True, "repo_gh_id": 4242, "socket_path": self.sock})

    def write_config(self, bridge, raw=None):
        with open(self.root + "/.autonomous-team/config.json", "w") as f:
            f.write(raw if raw is not None else json.dumps({"cloud_bridge": bridge}))

    def serve(self, mode):
        self.server = FakeDecide(self.sock, mode)
        return self.server

    def env(self):
        e = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "FX_ENGINE_COMMAND_ID")}
        e["AUTONOMOUS_TEAM_STATE_DIR"] = self.state
        return e

    def run(self, cmd, stdin=None, env=None):
        return subprocess.run(cmd, cwd=self.root, env=env or self.env(), input=stdin, capture_output=True,
                              text=True, timeout=120)

    def prespawn(self, role="executor", disc="842", event_id=EVENT_ID, dry=False, extra=None):
        cmd = ["bash", self.root + "/scripts/pre-spawn-check.sh", "--no-register", "--role", role]
        if disc:
            cmd += ["--discussion", disc]
        if event_id:
            cmd += ["--event-id", event_id]
        return self.run(cmd + (["--dry-run"] if dry else []) + (extra or []))

    def hook(self, payload=None, raw=None):
        p = json.load(open(PAYLOAD))
        p["tool_input"].update(payload or {})
        return self.run(["bash", self.root + "/scripts/hooks/engine-agent-start.sh"], stdin=raw if raw is not None else json.dumps(p))

    def events(self, kind):
        path = self.state + "/engine-events.jsonl"
        evs = [json.loads(x) for x in open(path).read().splitlines()] if os.path.exists(path) else []
        return [e for e in evs if e["kind"] == kind]

    def teamlog(self):
        return open(self.tlog).read() if os.path.exists(self.tlog) else ""

    def close(self):
        if self.server:
            self.server.close()
        shutil.rmtree(self.run_dir, ignore_errors=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))  # in-process outbox state too
    w = World(tmp_path)
    yield w
    w.close()


def body(srv):
    assert srv.violations == []
    assert len(srv.requests) == 1
    return json.loads(srv.requests[0]["body"])


# ── spawns: pre-spawn-check.sh ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_pre_spawn_matrix(world, mode):
    srv = world.serve(mode)
    r = world.prespawn()
    code, reason = MATRIX[mode]
    if code == 0:
        assert r.returncode == 0, r.stderr
        assert world.events("spawn_blocked") == []
        assert ("announce" in world.teamlog()) == (mode == "announce")
    else:
        assert r.returncode == 1 and "blocked_reason=" + reason in r.stderr, r.stderr
        ev = world.events("spawn_blocked")
        assert len(ev) == 1 and ev[0]["reason"] == reason and ev[0]["role"] == "executor" and ev[0]["discussion"] == 842
        assert "cloud decide gate refused spawn" in world.teamlog()
    if mode not in ("truncated", "close_early"):
        assert body(srv)["decision_type"] == "engine_spawn"


def test_pre_spawn_request_is_specific_to_this_spawn(world):
    srv = world.serve("act")
    assert world.prespawn(role="code-reviewer", disc="7", event_id="code-reviewer-7-1763300001").returncode == 0
    assert body(srv) == {"repo_gh_id": 4242, "decision_type": "engine_spawn",
                         "request_key": "spawn:code-reviewer-7-1763300001",
                         "subject": {"role": "code-reviewer", "discussion": 7}}


def test_pre_spawn_unknown_role_goes_as_other_and_odd_event_id_key_binds_role_and_discussion(world):
    srv = world.serve("act")
    assert world.prespawn(role="mission-analyst", disc="9", event_id="evt_1").returncode == 0
    b = body(srv)
    assert b["request_key"] == "spawn:mission-analyst:9:evt_1" and b["subject"]["role"] == "other"


def test_pre_spawn_command_id_travels(world):
    srv = world.serve("act")
    cid = "33333333-3333-4333-8333-333333333333"
    assert world.prespawn(extra=None).returncode == 0
    world.server.requests.clear()
    env = world.env()
    env["FX_ENGINE_COMMAND_ID"] = cid
    r = world.run(["bash", world.root + "/scripts/pre-spawn-check.sh", "--no-register", "--role", "executor",
                   "--discussion", "842", "--event-id", EVENT_ID + "9"], env=env)
    assert r.returncode == 0 and body(srv)["command_id"] == cid


def test_pre_spawn_halt_file_refuses_without_a_socket_call(world):
    srv = world.serve("act")
    with open(world.state + "/engine-halt.json", "w") as f:
        json.dump({"halted": True}, f)
    r = world.prespawn()
    assert r.returncode == 1 and "blocked_reason=halted" in r.stderr and srv.requests == []


def test_pre_spawn_no_socket_is_unreachable(world):
    r = world.prespawn()
    assert r.returncode == 1 and "blocked_reason=cloud_unreachable" in r.stderr
    assert world.events("spawn_blocked")[0]["reason"] == "cloud_unreachable"


def test_pre_spawn_missing_event_id_refuses_when_operate_is_on(world):
    srv = world.serve("act")
    r = world.prespawn(event_id="")
    assert r.returncode == 1 and "blocked_reason=cloud_unreachable" in r.stderr and srv.requests == []


def test_pre_spawn_missing_gate_library_refuses_when_operate_is_on(world):
    os.remove(world.root + "/scripts/lib/cloud-spawn-gate.sh")
    r = world.prespawn()
    assert r.returncode == 1 and "blocked_reason=cloud_unreachable" in r.stderr


def test_pre_spawn_gate_crash_refuses_when_operate_is_on_and_passes_when_off(world):
    open(world.root + "/scripts/lib/cloud_engine_gate.py", "w").write("raise SystemExit(1)\n")
    assert world.prespawn().returncode == 1
    world.write_config({"operate": False})
    assert world.prespawn().returncode == 0


@pytest.mark.parametrize("bridge,raw", [({"operate": False}, None), ({"enabled": True}, None), ({}, None),
                                        (None, '{"x": 1}'), (None, None)])
def test_operate_off_or_absent_changes_nothing(world, bridge, raw):
    if bridge is None and raw is None:
        os.remove(world.root + "/.autonomous-team/config.json")
    else:
        world.write_config(bridge, raw)
    srv = world.serve("http_500")  # would refuse if asked
    r = world.prespawn()
    assert r.returncode == 0, r.stderr
    assert srv.requests == [] and world.events("spawn_blocked") == []
    h = world.hook()
    assert h.returncode == 0 and h.stdout == "" and h.stderr == "" and srv.requests == []
    assert len(world.events("agent_started")) == 1


def test_dry_run_asks_nothing(world):
    srv = world.serve("ask")
    assert world.prespawn(dry=True, event_id="").returncode == 0
    assert srv.requests == []


# ── spawns: the PreToolUse(Agent) hook ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_hook_matrix(world, mode):
    srv = world.serve(mode)
    r = world.hook()
    code, reason = MATRIX[mode]
    if code == 0:
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
        assert len(world.events("agent_started")) == 1
    else:
        assert r.returncode == 2  # Claude Code: exit 2 blocks the tool call
        assert r.stderr.strip() == {3: r.stderr.strip(), 4: "engine halted", 5: UNREACHABLE}[code]
        assert world.events("agent_started") == []
        assert world.events("spawn_blocked")[0]["reason"] == reason
        if code == 3:
            assert r.stderr.startswith(("waiting for a person", "declined by a person"))
    if mode not in ("truncated", "close_early"):
        b = body(srv)
        assert b["decision_type"] == "engine_spawn" and b["subject"] == {"role": "executor", "discussion": 4321}
        assert b["request_key"] == "spawn:executor:4321:toolu_01SyntheticSpawn0001"


def test_hook_and_pre_spawn_share_one_key_through_the_prompt_trailer(world):
    srv = world.serve("act")
    prompt = "do it for D#842\nhook_event_id=" + EVENT_ID
    assert world.hook({"prompt": prompt}).returncode == 0
    assert world.prespawn().returncode == 0
    keys = [json.loads(q["body"])["request_key"] for q in srv.requests]
    assert keys == ["spawn:" + EVENT_ID] * 2
    assert json.loads(srv.requests[0]["body"])["subject"] == {"role": "executor", "discussion": 842}


@pytest.mark.parametrize("prompt,subagent", [
    ("do it for D#842\nhook_event_id=" + EVENT_ID, "fulcrumaxe:code-reviewer"),  # trailer is another role's
    ("do it for D#842\nhook_event_id=" + EVENT_ID, "mystery-role"),
    ("do it for D#7\nhook_event_id=" + EVENT_ID, "fulcrumaxe:executor"),  # trailer's Discussion is not this call's
    ("do it\nhook_event_id=" + EVENT_ID, "fulcrumaxe:executor"),
])
def test_hook_replayed_trailer_is_not_the_key(world, prompt, subagent):
    """An approved spawn:executor-842-<ts> must not be reusable by a call that only quotes its trailer."""
    srv = world.serve("act")
    assert world.hook({"prompt": prompt, "subagent_type": subagent}).returncode == 0
    assert json.loads(srv.requests[0]["body"])["request_key"] == "spawn:" + "executor:4321:toolu_01SyntheticSpawn0001".replace(
        "executor:4321", role_and_disc(prompt, subagent))
    assert "spawn:" + EVENT_ID not in srv.requests[0]["body"]


def role_and_disc(prompt, subagent):
    import re
    role = subagent.rsplit(":", 1)[-1]
    role = role if role in ("executor", "code-reviewer") else "other"
    m = re.search(r"D#(\d+)", prompt)
    return "%s:%s" % (role, m.group(1) if m else "nod")


def test_hook_matching_trailer_for_the_same_role_still_shares_the_key(world):
    srv = world.serve("act")
    prompt = "do it for D#842\nhook_event_id=" + EVENT_ID
    assert world.hook({"prompt": prompt, "subagent_type": "executor"}).returncode == 0
    assert json.loads(srv.requests[0]["body"])["request_key"] == "spawn:" + EVENT_ID


@pytest.mark.parametrize("raw", ["", "not json", "[]", json.dumps({"tool_name": "Bash"})])
def test_hook_unusable_payload_refuses_only_when_operate_is_on(world, raw):
    world.serve("act")
    assert world.hook(raw=raw).returncode == 2
    world.write_config({"operate": False})
    assert world.hook(raw=raw).returncode == 0


def test_hook_no_spawn_id_refuses(world):
    srv = world.serve("act")
    p = json.load(open(PAYLOAD))
    del p["tool_use_id"]
    assert world.hook(raw=json.dumps(p)).returncode == 2 and srv.requests == []


# ── Spec approvals: the outbox flip ──────────────────────────────────────────────────────────────────
def spec(phase="SPEC_READY", extra=""):
    return "<!-- STATUS:%s SINCE:2026-11-16T00:00:00Z -->\n\n## Spec (Acceptance)\n1. thing%s\n" % (phase, extra)


class FakeGH(ob.GitHubClient):
    def __init__(self, title, body):
        self.title, self.body, self.updates = title, body, []

    def get_discussion(self, repo, number):
        return {"id": "D_x", "title": self.title, "body": self.body}

    def update_body(self, repo, number, text):
        self.updates.append(text)
        return {}


def flip(world, title, mode, cur=None, new=None):
    cur = cur if cur is not None else spec("CONSENSUS")
    new = new if new is not None else spec()
    srv = world.serve(mode) if mode else None
    gh = FakeGH(title, cur)
    row = ob._process_body(new, "project-manager", 842, gh, "o/r", "sha", "agent1", repo_root=world.root)
    return row, gh, srv


# mode -> (flipped?, outcome of the row)
SPEC_MATRIX = {"act": (True, "posted"), "announce": (True, "posted"), "act_approved": (True, "posted"),
               "ask": (False, "refused:cloud_ask"), "ask_declined": (False, "refused:cloud_ask"),
               "halted": (False, "refused:cloud_halted"), "halted_act": (False, "refused:cloud_halted"),
               "http_500": (False, "refused:cloud_unreachable"), "not_json": (False, "refused:cloud_unreachable"),
               "truncated": (False, "refused:cloud_unreachable")}


@pytest.mark.parametrize("mode", sorted(SPEC_MATRIX))
def test_spec_ready_matrix(world, mode):
    row, gh, srv = flip(world, "[Small] tidy the thing", mode)
    flipped, outcome = SPEC_MATRIX[mode]
    assert row["outcome"] == outcome
    assert (len(gh.updates) == 1) == flipped
    if not flipped:  # STATUS stays as it was, and the team log names why
        assert gh.updates == [] and "D#842 stays at its current status" in world.teamlog()
    else:
        assert world.teamlog() == ""
    if mode != "truncated":
        assert body(srv)["decision_type"] == "engine_spec_ready"


@pytest.mark.parametrize("title,tag", [("[Feature] x", "feature"), ("[Critical] x", "critical"), ("[Small] x", "small"),
                                       ("[Bug] x", "bug"), ("[Doc] x", "doc"), ("[Process] x", "process"),
                                       ("[Strategy] x", "other"), ("  [bug] lower", "bug"), ("no prefix", None), ("", None),
                                       ("[Small][Feature] two", None), ("[Small] [Feature] two", None)])
def test_spec_ready_sends_the_tag_and_a_key_bound_to_the_spec_text(world, title, tag):
    import hashlib
    row, _gh, srv = flip(world, title, "act")
    b = body(srv)
    expected = {"discussion": 842} if tag is None else {"discussion": 842, "tag": tag}
    assert b["subject"] == expected
    assert b["request_key"] == "spec:842:" + hashlib.sha256(spec().encode()).hexdigest()[:16]
    assert row["outcome"] == "posted"


def test_spec_ready_key_changes_when_the_spec_changes(world):
    _r, _g, srv = flip(world, "[Small] x", "act")
    first = body(srv)["request_key"]
    srv.requests.clear()
    gh = FakeGH("[Small] x", spec("CONSENSUS"))
    ob._process_body(spec(extra=" changed"), "project-manager", 842, gh, "o/r", "sha", "a", repo_root=world.root)
    assert json.loads(srv.requests[0]["body"])["request_key"] != first


def test_spec_ready_only_the_flip_is_gated(world):
    # Already SPEC_READY (a Spec edit) and a non-SPEC_READY phase change never ask.
    srv = world.serve("ask")
    gh = FakeGH("[Feature] x", spec())
    assert ob._process_body(spec(extra=" v2"), "project-manager", 842, gh, "o/r", "s", "a", repo_root=world.root)["outcome"] == "posted"
    gh2 = FakeGH("[Feature] x", spec("DISCUSSING"))
    assert ob._process_body(spec("CONSENSUS"), "project-manager", 842, gh2, "o/r", "s", "a", repo_root=world.root)["outcome"] == "posted"
    assert srv.requests == []


@pytest.mark.parametrize("bridge,raw", [({"operate": False}, None), ({}, None), (None, '{"x": 1}'), (None, None)])
def test_spec_ready_operate_off_or_absent_changes_nothing(world, bridge, raw):
    if bridge is None and raw is None:
        os.remove(world.root + "/.autonomous-team/config.json")
    else:
        world.write_config(bridge, raw)
    row, gh, srv = flip(world, "[Feature] x", "http_500")
    assert row["outcome"] == "posted" and srv.requests == [] and world.teamlog() == ""


def test_spec_ready_gate_that_cannot_load_refuses_when_operate_is_on_and_passes_when_off(world):
    os.remove(world.root + "/scripts/lib/cloud_engine_gate.py")
    row, gh, _ = flip(world, "[Small] x", None)
    assert row["outcome"] == "refused:cloud_unreachable" and gh.updates == []
    world.write_config({"operate": False})
    row, gh, _ = flip(world, "[Small] x", None)
    assert row["outcome"] == "posted"
    world.write_config(None, "{not json")
    row, gh, _ = flip(world, "[Small] x", None)
    assert row["outcome"] == "refused:cloud_unreachable"


def test_spec_ready_flip_without_repo_root_refuses():
    gh = FakeGH("[Feature] x", spec("CONSENSUS"))
    assert ob._process_body(spec(), "project-manager", 842, gh, "o/r", "s", "a")["outcome"] == "refused:cloud_unreachable"
    assert gh.updates == []
    # a body that is not a flip never needs the gate
    gh2 = FakeGH("[Feature] x", spec("DISCUSSING"))
    assert ob._process_body(spec("CONSENSUS"), "project-manager", 842, gh2, "o/r", "s", "a")["outcome"] == "posted"


def test_spec_ready_held_file_logs_once_per_spec_text(world):
    for _ in range(3):
        flip(world, "[Small] x", "ask" if world.server is None else None)
    assert world.teamlog().count("[spec-gate] D#842 stays") == 1
    flip(world, "[Small] x", None, new=spec(extra=" edited"))  # different text: logged again
    assert world.teamlog().count("[spec-gate] D#842 stays") == 2
