"""cloud_decide.py and scripts/hooks/pre-merge.d/cloud-decide.sh against a fake decide socket that enforces
the real request contract (testsupport/fake_cloud_decide.py). The hook is run as a real subprocess in a
scratch engine tree with its own state directory; nothing here touches a real state dir or a real repo."""
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from testsupport.fake_cloud_decide import FakeDecide, UUID_A, UUID_B, MODES  # noqa: E402

spec = importlib.util.spec_from_file_location("cloud_decide", os.path.join(ROOT, "scripts", "lib", "cloud_decide.py"))
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)

SHA = "a" * 40
LINE_UNREACHABLE = "cloud unreachable; merge refused (fail-closed)"


class World:
    """A scratch engine tree: scripts/{hooks/pre-merge.d,lib}, a config, a state dir, a short socket path."""

    def __init__(self, tmp_path):
        self.root = str(tmp_path / "engine")
        self.state = str(tmp_path / "state")
        self.run_dir = tempfile.mkdtemp(prefix="fxd")  # short: sun_path is limited to ~108 bytes
        os.chmod(self.run_dir, 0o700)
        self.sock = os.path.join(self.run_dir, "fx-runner", "engine-bridge.sock")
        self.log = str(tmp_path / "teamlog.txt")
        for d in ("scripts/hooks/pre-merge.d", "scripts/lib", ".autonomous-team"):
            os.makedirs(os.path.join(self.root, d))
        os.makedirs(self.state, mode=0o700)
        shutil.copy(os.path.join(ROOT, "scripts/hooks/pre-merge.d/cloud-decide.sh"), self.root + "/scripts/hooks/pre-merge.d/")
        for f in ("cloud_decide.py", "state-dir.sh"):
            shutil.copy(os.path.join(ROOT, "scripts/lib", f), self.root + "/scripts/lib/")
        stub = self.root + "/scripts/rotate-team-log.sh"
        with open(stub, "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%%s\\n" "$2" >> "%s"\n' % self.log)
        os.chmod(stub, 0o755)
        self.server = None
        self.write_config({"operate": True, "repo_gh_id": 4242, "socket_path": self.sock})

    def write_config(self, bridge, raw=None):
        with open(self.root + "/.autonomous-team/config.json", "w") as f:
            f.write(raw if raw is not None else json.dumps({"cloud_bridge": bridge}))

    def serve(self, mode):
        self.server = FakeDecide(self.sock, mode)
        return self.server

    def run_hook(self, pr="77", sha=SHA, disc=None, xdg=None, timeout=60):
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "AUTONOMOUS_TEAM_STATE_DIR")}
        env["AUTONOMOUS_TEAM_STATE_DIR"] = self.state
        if xdg:
            env["XDG_RUNTIME_DIR"] = xdg
        cmd = ["bash", self.root + "/scripts/hooks/pre-merge.d/cloud-decide.sh", "--pr", pr, "--head-sha", sha]
        if disc:
            cmd += ["--discussion", disc]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)

    def teamlog(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def close(self):
        if self.server:
            self.server.close()
        shutil.rmtree(self.run_dir, ignore_errors=True)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


# ── 1. operate off or absent: no cloud call, merge unchanged ─────────────────────────────────────────
@pytest.mark.parametrize("bridge,raw", [
    ({"operate": False}, None), ({"enabled": True}, None), ({}, None), (None, '{"other": 1}'), (None, None)])
def test_operate_off_or_absent_exits_0_and_never_calls(world, bridge, raw):
    if raw is None and bridge is None:
        os.remove(world.root + "/.autonomous-team/config.json")
    else:
        world.write_config(bridge, raw)
    srv = world.serve("http_500")  # would refuse if it were asked
    r = world.run_hook(sha="notasha")  # not even validated while operate is off
    assert r.returncode == 0, r.stderr
    assert srv.requests == [] and r.stderr == ""


# ── 2. the answers ───────────────────────────────────────────────────────────────────────────────────
def _body(srv):
    assert srv.violations == []
    assert len(srv.requests) == 1
    return json.loads(srv.requests[0]["body"])


def test_act_proceeds_and_sends_the_exact_request(world):
    srv = world.serve("act")
    r = world.run_hook(pr="77", disc="12")
    assert r.returncode == 0 and r.stderr == ""
    assert _body(srv) == {"repo_gh_id": 4242, "decision_type": "engine_merge", "request_key": "merge:77:" + SHA,
                          "subject": {"pr": 77, "discussion": 12, "head_sha": SHA}}
    assert world.teamlog() == ""


def test_announce_proceeds_and_logs_the_receipt(world):
    srv = world.serve("announce")
    r = world.run_hook()
    assert r.returncode == 0
    _body(srv)
    assert UUID_A in world.teamlog() and "PR #77" in world.teamlog()


def test_announce_still_proceeds_when_the_team_log_fails(world):
    os.remove(world.root + "/scripts/rotate-team-log.sh")
    world.serve("announce")
    assert world.run_hook().returncode == 0


def test_ask_blocks_with_exit_3_and_the_ask_id(world):
    srv = world.serve("ask")
    r = world.run_hook()
    assert r.returncode == 3
    assert "waiting for a person in the cloud (ask %s)" % UUID_B in r.stderr
    _body(srv)


def test_a_declined_ask_still_blocks(world):
    world.serve("ask_declined")
    r = world.run_hook()
    assert r.returncode == 3 and "declined by a person in the cloud (ask %s)" % UUID_B in r.stderr


def test_an_approved_ask_proceeds(world):
    world.serve("act_approved")
    assert world.run_hook().returncode == 0


@pytest.mark.parametrize("mode", ["halted", "halted_act"])
def test_halted_reply_exits_4(world, mode):
    world.serve(mode)
    r = world.run_hook()
    assert r.returncode == 4 and "engine halted" in r.stderr


@pytest.mark.parametrize("mode", ["chunked_act", "no_length_act"])
def test_other_valid_http_framings_work(world, mode):
    world.serve(mode)
    assert world.run_hook().returncode == 0


# ── 3. fail closed: every way the answer can be missing or wrong ─────────────────────────────────────
BAD_MODES = sorted(m for m in MODES if m not in ("act", "announce", "ask", "ask_declined", "act_approved", "halted", "halted_act")) \
    + ["garbage", "truncated", "close_early"]


@pytest.mark.parametrize("mode", BAD_MODES)
def test_bad_answers_refuse_the_merge(world, mode):
    srv = world.serve(mode)
    r = world.run_hook()
    assert r.returncode == 5
    assert r.stderr.strip() == LINE_UNREACHABLE
    assert srv.violations == []


def test_a_peer_that_never_answers_times_out_and_refuses(world):
    world.serve("hang")
    r = world.run_hook()  # the client's own 10 s limit
    assert r.returncode == 5 and r.stderr.strip() == LINE_UNREACHABLE


def test_library_timeout_is_a_refusal(world):
    world.serve("hang")
    code, outcome, reply = cd.decide(world.root, "engine_merge", "merge:1:" + SHA, {"pr": 1, "head_sha": SHA},
                                     timeout=1.0)
    assert (code, outcome, reply) == (5, "unreachable:timeout", None)


def test_no_socket_at_all(world):
    r = world.run_hook()
    assert r.returncode == 5 and r.stderr.strip() == LINE_UNREACHABLE


def test_stale_socket_file_nobody_listens_on(world):
    os.makedirs(os.path.dirname(world.sock), mode=0o700)
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(world.sock)
    os.chmod(world.sock, 0o600)
    dead.close()  # the file stays, nothing listens
    r = world.run_hook()
    assert r.returncode == 5


def test_socket_path_that_is_a_symlink(world):
    world.serve("act")
    link = os.path.join(world.run_dir, "fx-runner", "link.sock")
    os.symlink(world.sock, link)
    world.write_config({"operate": True, "repo_gh_id": 4242, "socket_path": link})
    r = world.run_hook()
    assert r.returncode == 5 and world.server.requests == []


def test_socket_with_wide_mode_is_not_trusted(world):
    world.serve("act")
    os.chmod(world.sock, 0o666)
    assert world.run_hook().returncode == 5 and world.server.requests == []


def test_socket_directory_with_wide_mode_is_not_trusted(world):
    world.serve("act")
    os.chmod(os.path.dirname(world.sock), 0o755)
    assert world.run_hook().returncode == 5 and world.server.requests == []


def test_a_regular_file_is_not_a_socket(world):
    os.makedirs(os.path.dirname(world.sock))
    open(world.sock, "w").close()
    os.chmod(world.sock, 0o600)
    assert world.run_hook().returncode == 5


def test_peer_uid_mismatch_refuses(world, monkeypatch):
    srv = world.serve("act")
    real = os.getuid()
    monkeypatch.setattr(cd, "check_socket_file", lambda p: None)  # isolate the SO_PEERCRED check
    monkeypatch.setattr(cd.os, "getuid", lambda: real + 1)
    code, outcome, _ = cd.decide(world.root, "engine_merge", "merge:1:" + SHA, {"pr": 1, "head_sha": SHA})
    assert (code, outcome) == (5, "unreachable:peer_uid_mismatch")
    assert srv.requests == []  # refused before anything was sent


def test_fake_server_rejects_a_foreign_peer_too(world):
    """The fake enforces the same uid rule, so a test of the client cannot pass by accident."""
    srv = world.serve("act")
    s = socket.socket(socket.AF_UNIX)
    s.connect(world.sock)
    s.sendall(b"GET / HTTP/1.1\r\n\r\n")
    assert b"400" in s.recv(100)
    s.close()
    assert srv.violations == ["wrong method or path"]


@pytest.mark.parametrize("xdg,bridge", [
    (None, {"operate": True, "repo_gh_id": 4242}),                                   # no socket path anywhere
    (None, {"operate": True, "repo_gh_id": 4242, "socket_path": "relative.sock"}),
    (None, {"operate": True, "repo_gh_id": 4242, "socket_path": 5}),
    (None, {"operate": True, "socket_path": "/nonexistent/x.sock"}),                 # no repo id
    (None, {"operate": True, "repo_gh_id": 0, "socket_path": "/nonexistent/x.sock"}),
    (None, {"operate": True, "repo_gh_id": True, "socket_path": "/nonexistent/x.sock"}),
    (None, {"operate": "yes", "repo_gh_id": 1}),                                      # not a boolean
    (None, {"operate": None, "repo_gh_id": 1}),
    (None, "not-an-object"),
])
def test_bad_configuration_refuses(world, xdg, bridge):
    world.write_config(bridge)
    r = world.run_hook(xdg=xdg)
    assert r.returncode == 5 and r.stderr.strip() == LINE_UNREACHABLE


def test_unparsable_config_refuses(world):
    world.write_config(None, raw="{ not json")
    assert world.run_hook().returncode == 5


def test_symlinked_config_refuses(world):
    p = world.root + "/.autonomous-team/config.json"
    os.rename(p, p + ".real")
    os.symlink(p + ".real", p)
    assert world.run_hook().returncode == 5


def test_default_socket_path_comes_from_xdg_runtime_dir(world):
    world.write_config({"operate": True, "repo_gh_id": 4242})
    xdg = world.run_dir
    world.serve("act")
    assert world.run_hook(xdg=xdg).returncode == 0


@pytest.mark.parametrize("pr,sha", [("0", SHA), ("x", SHA), ("77", "deadbeef"), ("77", SHA.upper()), ("77", SHA + "0"), ("", SHA)])
def test_bad_arguments_refuse_when_operate_is_on(world, pr, sha):
    srv = world.serve("act")
    r = world.run_hook(pr=pr, sha=sha)
    assert r.returncode == 5 and srv.requests == []


# ── 4. the local halt file ───────────────────────────────────────────────────────────────────────────
def test_halt_file_refuses_before_any_socket_call(world):
    srv = world.serve("act")
    with open(world.state + "/engine-halt.json", "w") as f:
        json.dump({"halted": True}, f)
    r = world.run_hook()
    assert r.returncode == 4 and srv.requests == []


def test_halt_file_false_lets_the_cloud_decide(world):
    srv = world.serve("act")
    with open(world.state + "/engine-halt.json", "w") as f:
        json.dump({"halted": False}, f)
    assert world.run_hook().returncode == 0 and len(srv.requests) == 1


@pytest.mark.parametrize("content", ["{ nope", "[]", '{"halted": "true"}', '{"halted": 1}', "{}"])
def test_unreadable_halt_file_refuses(world, content):
    srv = world.serve("act")
    with open(world.state + "/engine-halt.json", "w") as f:
        f.write(content)
    assert world.run_hook().returncode == 5 and srv.requests == []


def test_symlinked_halt_file_refuses(world):
    srv = world.serve("act")
    with open(world.state + "/real.json", "w") as f:
        json.dump({"halted": False}, f)
    os.symlink(world.state + "/real.json", world.state + "/engine-halt.json")
    assert world.run_hook().returncode == 5 and srv.requests == []


def test_halt_file_is_ignored_when_operate_is_off(world):
    world.write_config({"operate": False})
    with open(world.state + "/engine-halt.json", "w") as f:
        json.dump({"halted": True}, f)
    assert world.run_hook().returncode == 0


# ── 5. the CLI prints nothing the peer wrote ─────────────────────────────────────────────────────────
def test_stdout_line_carries_only_validated_fields(world):
    world.serve("ask")
    p = subprocess.run([sys.executable, world.root + "/scripts/lib/cloud_decide.py", "decide", "--type", "engine_merge",
                        "--key", "merge:1:" + SHA, "--pr", "1", "--head-sha", SHA, "--repo-root", world.root],
                       env={**os.environ, "AUTONOMOUS_TEAM_STATE_DIR": world.state}, capture_output=True, text=True)
    assert p.returncode == 3
    assert p.stdout.strip() == "ask receipt=%s ask=%s answered=-" % (UUID_A, UUID_B)


def test_socket_modes_are_what_the_checker_demands(world):
    world.serve("act")
    assert stat.S_IMODE(os.lstat(world.sock).st_mode) == 0o600
