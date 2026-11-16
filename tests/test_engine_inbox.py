"""EB-10c: the engine inbox reader, the cloud dial ceiling and the halt file. Every case runs in a scratch engine
tree with its own state dir, a stub `gh` first on PATH and invalid GitHub credentials; nothing reaches GitHub."""
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COPY = ["scripts/engine-inbox.py", "scripts/lib/cloud_decide.py", "scripts/lib/cloud_engine_gate.py", "scripts/lib/engine_emit.py",
        "scripts/lib/engine_agent_events.py", "scripts/lib/subagent_payload.py", "schemas/engine-events.v1.json", "backend/dial_registry.py",
        "backend/cloud_ceiling.py", "backend/state_paths.py"]
REPO_ID = 4242
ASK = "0123456789abcdef"
SYSTEM = [{"kind": "system", "reason": "t"}]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    def __init__(self, tmp_path, operate=True):
        self.root = str(tmp_path / "engine")
        self.state = str(tmp_path / "state")
        self.inbox = self.state + "/engine-inbox"
        self.done = self.inbox + "/done"
        self.bin = str(tmp_path / "bin")
        self.log = str(tmp_path / "tlog.txt")
        for f in COPY:
            os.makedirs(os.path.dirname(os.path.join(self.root, f)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.root, f))
        os.makedirs(self.root + "/.autonomous-team")
        os.makedirs(self.bin)
        for d in (self.state, self.inbox, self.done):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        with open(self.bin + "/gh", "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(self.bin + "/gh", 0o755)
        with open(self.root + "/scripts/rotate-team-log.sh", "w") as f:
            f.write('#!/bin/sh\nprintf "%%s\\n" "$2" >> "%s"\n' % self.log)
        with open(self.root + "/scripts/lib/external_intake_gate.py", "w") as f:
            f.write("import sys\nprint('<<UNTRUSTED>> ' + sys.stdin.read() + ' <<END>>')\n")
        self.config(operate)
        self.write_json(self.state + "/dial-directive-allowlist.json", [{"kind": "cloud_command"}] + SYSTEM)
        self.write_json(self.state + "/engine-team-log-cursor.json", {"issue": 1, "last_id": 5, "open_asks": {ASK: 77}})
        self.seq = 0

    def config(self, operate=True, repo=REPO_ID):
        self.write_json(self.root + "/.autonomous-team/config.json", {"cloud_bridge": {"operate": operate, "repo_gh_id": repo}}, 0o644)

    def write_json(self, path, value, mode=0o600):
        with open(path, "w") as f:
            json.dump(value, f)
        os.chmod(path, mode)

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
        e.update(AUTONOMOUS_TEAM_STATE_DIR=self.state, GH_TOKEN="invalid", GITHUB_TOKEN="invalid", GH_HOST="invalid.localhost")
        e["PATH"] = self.bin + os.pathsep + e["PATH"]
        return e

    def cmd(self, kind, args=None, **over):
        self.seq += 1
        now = datetime.now(timezone.utc)
        c = {"v": 1, "command_id": str(uuid.uuid4()), "command_seq": self.seq, "runner_id": str(uuid.uuid4()), "repo_gh_id": REPO_ID,
             "kind": kind, "args": args or {}, "issued_by": str(uuid.uuid4()), "issued_at": iso(now),
             "expires_at": iso(now + timedelta(seconds=300)), "halt_epoch": 0}
        c.update(over)
        return c

    def put(self, c, mode=0o600):
        name = "%s-%s" % (c["command_seq"], c["command_id"])
        self.write_json("%s/%s.json" % (self.inbox, name), c, mode)
        return name

    def run(self):
        return subprocess.run([sys.executable, self.root + "/scripts/engine-inbox.py", "run"], cwd=self.root, env=self.env(),
                              capture_output=True, text=True, timeout=120)

    def py(self, code):
        full = "import sys; sys.path.insert(0, %r)\nfrom backend import dial_registry as d, cloud_ceiling as c\n%s" % (self.root, code)
        return subprocess.run([sys.executable, "-c", full], cwd=self.root, env=self.env(), capture_output=True, text=True, timeout=120)

    def outcome(self, name):
        p = "%s/%s.outcome.json" % (self.done, name)
        if not os.path.exists(p):
            return None
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        return json.load(open(p))

    def halt(self, halted=True):
        self.write_json(self.state + "/engine-halt.json", {"halted": halted, "halt_epoch": 3, "at": "2026-11-16T10:00:00Z"})

    def dial(self, cls):
        return json.load(open(self.state + "/dial-registry.json"))[cls]["level"]

    def events(self, kind):
        p = self.state + "/engine-events.jsonl"
        if not os.path.exists(p):
            return []
        return [e for e in (json.loads(x) for x in open(p).read().splitlines()) if e["kind"] == kind]

    def ceiling_file(self, ceiling, **over):
        now = datetime.now(timezone.utc)
        doc = {"v": 1, "repo_gh_id": REPO_ID, "fetched_at": iso(now), "expires_at": iso(now + timedelta(minutes=15)), "catalogue_version": 1,
               "halt_epoch": 0, "halted": False, "ceilings": {k: ceiling for k in ("agent.spawn", "merge.standard", "merge.fast-path")}}
        doc.update(over)
        self.write_json(self.state + "/cloud-dial-ceiling.json", doc)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def refused(word):
    return {"outcome": "refused", "refusal": word}


APPLIED = {"outcome": "applied"}


def test_set_dial_applied_with_min_of_level_and_ceiling(w):
    w.ceiling_file(5, ceilings={"agent.spawn": 2, "merge.standard": 5, "merge.fast-path": 5})
    a = w.put(w.cmd("set_dial", {"dial_class": "agent.spawn", "level": 4, "ttl": "none"}))
    b = w.put(w.cmd("set_dial", {"dial_class": "docs.write", "level": 3, "ttl": "for_today"}))
    r = w.run()
    assert r.returncode == 0, r.stderr
    assert w.outcome(a) == APPLIED and w.outcome(b) == APPLIED
    assert w.dial("agent.spawn") == 2 and w.dial("docs.write") == 3
    assert not os.path.exists("%s/%s.json" % (w.inbox, a)) and os.path.exists("%s/%s.json" % (w.done, a))


def test_set_dial_without_a_ceiling_file_uses_ceiling_one_for_the_three_classes(w):
    a = w.put(w.cmd("set_dial", {"dial_class": "merge.standard", "level": 5, "ttl": "none"}))
    w.run()
    assert w.outcome(a) == APPLIED and w.dial("merge.standard") == 1


def test_sandbox_modify_is_refused_even_with_a_forged_allowlist(w):
    a = w.put(w.cmd("set_dial", {"dial_class": "sandbox.modify", "level": 1, "ttl": "none"}))
    w.run()
    assert w.outcome(a) == refused("signature_invalid")
    code = ("for k in ('sandbox.modify','methodology.change','external.system','intent.generate','cost.spend'):\n"
            "    try: d.set_dial(k, 1, source={'kind':'cloud_command'})\n    except ValueError: continue\n    sys.exit(1)\n")
    assert w.py(code).returncode == 0


def test_answer_ask_writes_answer_clears_ask_and_frames_the_note(w):
    a = w.put(w.cmd("answer_ask", {"ask_id": ASK, "choice": "approve", "note": "go ahead"}))
    b = w.put(w.cmd("answer_ask", {"ask_id": "ffffffffffffffff", "choice": "approve"}))
    w.run()
    assert w.outcome(a) == APPLIED and w.outcome(b) == refused("unknown_ask")
    ans = json.load(open(w.state + "/asks/%s.answer.json" % ASK))
    assert ans["choice"] == "approve" and ans["note"].startswith("Owner answer via cloud (user ") and "<<UNTRUSTED>>" in ans["note"]
    assert [e["how"] for e in w.events("ask_cleared")] == ["answered_cloud"]
    assert json.load(open(w.state + "/engine-team-log-cursor.json"))["open_asks"] == {}
    assert ASK in open(w.log).read()


def test_answer_ask_defer_keeps_the_ask_open(w):
    a = w.put(w.cmd("answer_ask", {"ask_id": ASK, "choice": "defer"}))
    w.run()
    assert w.outcome(a) == APPLIED and not w.events("ask_cleared")
    assert ASK in json.load(open(w.state + "/engine-team-log-cursor.json"))["open_asks"]


def test_halt_file_applies_nothing_but_answer_ask(w):
    w.halt()
    names = [w.put(w.cmd("set_dial", {"dial_class": "docs.write", "level": 2, "ttl": "none"})), w.put(w.cmd("start_day")),
             w.put(w.cmd("answer_ask", {"ask_id": ASK, "choice": "decline"}))]
    w.run()
    assert [w.outcome(n) for n in names] == [refused("halted"), refused("halted"), APPLIED]


def test_unreadable_halt_file_counts_as_halted(w):
    open(w.state + "/engine-halt.json", "w").write("{nope")
    n = w.put(w.cmd("start_day"))
    w.run()
    assert w.outcome(n) == refused("halted")


def test_terminal_kinds_print_fixed_sentences_and_are_applied(w):
    c = w.cmd("request_spawn", {"role": "executor", "discussion": 842})
    names = [w.put(c), w.put(w.cmd("stop_agent", {"agent_id": "abc123"})), w.put(w.cmd("pause_loop"))]
    r = w.run()
    assert all(w.outcome(n) == APPLIED for n in names)
    assert "Owner requested executor for D#842 (cloud command %s)." % c["command_id"] in r.stdout
    assert "Owner asked to stop agent abc123" in r.stdout and "pause the loop" in r.stdout


def test_a_running_host_gets_the_command_instead_of_a_sentence(w):
    w.write_json(w.state + "/engine-host.json", {"pid": os.getpid()})
    n = w.put(w.cmd("resume_loop"))
    r = w.run()
    assert w.outcome(n) == APPLIED and r.stdout == ""
    assert os.path.exists("%s/engine-host-inbox/%s.json" % (w.state, n))


@pytest.mark.parametrize("what", ["unknown_kind", "extra_key", "bad_args", "long_note", "control_note", "long_life", "name_mismatch"])
def test_invalid_commands_are_refused(w, what):
    c = w.cmd("start_day")
    if what == "unknown_kind":
        c["kind"] = "format_disk"
    elif what == "extra_key":
        c["extra"] = 1
    elif what == "bad_args":
        c = w.cmd("answer_ask", {"ask_id": ASK, "choice": "approve", "sudo": True})
    elif what == "long_note":
        c = w.cmd("answer_ask", {"ask_id": ASK, "choice": "approve", "note": "x" * 501})
    elif what == "control_note":
        c = w.cmd("answer_ask", {"ask_id": ASK, "choice": "approve", "note": "a‮b"})
    elif what == "long_life":
        c["expires_at"] = iso(datetime.now(timezone.utc) + timedelta(seconds=900))
    if what == "name_mismatch":
        n = "1-%s" % c["command_id"]
        c["command_seq"] = 99
        w.write_json("%s/%s.json" % (w.inbox, n), c)
    else:
        n = w.put(c)
    w.run()
    assert w.outcome(n) == refused("signature_invalid")
    assert not os.path.exists(w.state + "/asks")


def test_wrong_repo_expired_replay_and_operate_off(w):
    wrong = w.put(w.cmd("start_day", repo_gh_id=7))
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    expired = w.put(w.cmd("start_day", issued_at=iso(old), expires_at=iso(old + timedelta(seconds=60))))
    w.run()
    assert w.outcome(wrong) == refused("wrong_repo") and w.outcome(expired) == refused("expired")
    first = w.cmd("pause_loop", command_seq=10)
    first_name = w.put(first)
    w.run()
    assert w.outcome(first_name) == APPLIED
    low = w.put(w.cmd("pause_loop", command_seq=10))
    again = w.put(w.cmd("pause_loop", command_id=first["command_id"], command_seq=60))
    w.run()
    assert w.outcome(low) == refused("seq_not_increasing") and w.outcome(again) == refused("seq_not_increasing")
    w.config(operate=False)
    off = w.put(w.cmd("start_day", command_seq=500))
    w.run()
    assert w.outcome(off) == refused("operate_off")


def test_unsafe_inbox_writes_nothing(w, tmp_path):
    n = w.put(w.cmd("start_day"))
    os.chmod(w.inbox, 0o750)
    r = w.run()
    assert r.returncode == 2 and w.outcome(n) is None and os.path.exists("%s/%s.json" % (w.inbox, n))
    os.chmod(w.inbox, 0o700)
    os.chmod(w.done, 0o755)
    assert w.run().returncode == 2 and w.outcome(n) is None
    os.chmod(w.done, 0o700)
    real = str(tmp_path / "elsewhere")
    os.makedirs(real, mode=0o700)
    shutil.rmtree(w.done)
    os.symlink(real, w.done)
    assert w.run().returncode == 2 and os.listdir(real) == []


def test_bad_files_are_left_alone(w, tmp_path):
    wide = w.put(w.cmd("start_day"), mode=0o644)
    notjson = "%s/90-%s.json" % (w.inbox, uuid.uuid4())
    open(notjson, "w").write("{oops")
    os.chmod(notjson, 0o600)
    target = str(tmp_path / "target.json")
    open(target, "w").write("{}")
    link = "%s/91-%s.json" % (w.inbox, uuid.uuid4())
    os.symlink(target, link)
    r = w.run()
    assert r.returncode == 0 and os.listdir(w.done) == []
    assert os.path.exists("%s/%s.json" % (w.inbox, wide)) and os.path.exists(notjson) and os.path.islink(link)


def test_unusable_config_writes_nothing(w):
    n = w.put(w.cmd("start_day"))
    open(w.root + "/.autonomous-team/config.json", "w").write("{nope")
    r = w.run()
    assert r.returncode == 2 and w.outcome(n) is None


def test_no_inbox_is_a_quiet_pass(tmp_path):
    w = World(tmp_path)
    shutil.rmtree(w.inbox)
    assert w.run().returncode == 0 and not os.path.exists(w.inbox)


# ---- the ceiling in dial_registry.check() ----------------------------------------------------------

def test_ceiling_never_raises_a_level_over_every_combination(w):
    for ceil in range(1, 6):
        w.ceiling_file(ceil)
        code = ("bad = []\nfor local in range(1, 6):\n    d.set_dial('agent.spawn', local, source={'kind': 'system', 'reason': 't'})\n"
                "    for req in range(1, 6):\n        ok = d.check('agent.spawn', req)[0]\n"
                "        if ok != (req <= min(local, %d)): bad.append((local, req))\nprint(bad)\n" % ceil)
        r = w.py(code)
        assert r.stdout.strip() == "[]", (ceil, r.stdout, r.stderr)


@pytest.mark.parametrize("case", ["missing", "expired", "garbage", "symlink", "wide_mode", "other_repo", "bad_ceiling", "long_life", "halted"])
def test_bad_ceiling_file_means_ceiling_one(w, case, tmp_path):
    path = w.state + "/cloud-dial-ceiling.json"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    if case == "expired":
        w.ceiling_file(5, fetched_at=iso(past - timedelta(minutes=15)), expires_at=iso(past))
    elif case == "garbage":
        open(path, "w").write("{x")
        os.chmod(path, 0o600)
    elif case == "symlink":
        w.ceiling_file(5)
        os.rename(path, str(tmp_path / "real.json"))
        os.symlink(str(tmp_path / "real.json"), path)
    elif case == "wide_mode":
        w.ceiling_file(5)
        os.chmod(path, 0o644)
    elif case == "other_repo":
        w.ceiling_file(5, repo_gh_id=1)
    elif case == "bad_ceiling":
        w.ceiling_file(5, ceilings={"agent.spawn": 9, "merge.standard": 5, "merge.fast-path": 5})
    elif case == "long_life":
        w.ceiling_file(5, expires_at=iso(datetime.now(timezone.utc) + timedelta(hours=5)))
    elif case == "halted":
        w.ceiling_file(5, halted=True)
    code = ("d.set_dial('merge.fast-path', 5, source={'kind': 'system', 'reason': 't'})\n"
            "print(d.check('merge.fast-path', 1)[0], d.check('merge.fast-path', 2)[0], c.ceiling_for('agent.spawn'), c.ceiling_for('docs.write'))")
    r = w.py(code)
    assert r.stdout.split() == ["True", "False", "1", "None"], r.stderr


def test_operate_off_leaves_check_unchanged(tmp_path):
    w = World(tmp_path, operate=False)
    code = "d.set_dial('agent.spawn', 4, source={'kind': 'system', 'reason': 't'})\nprint(d.check('agent.spawn', 4)[0], c.ceiling_for('agent.spawn'))"
    assert w.py(code).stdout.split() == ["True", "None"]


# ---- the spawn check compares its role with the role inside --spawn-id -------------------------------

def test_spawn_key_binds_the_role_inside_the_spawn_id(w):
    code = ("import sys; sys.path.insert(0, %r)\nimport cloud_engine_gate as g\n"
            "print(g.spawn_key('executor', 842, 'executor-842-1763300000'), g.spawn_key('code-reviewer', 842, 'executor-842-1763300000'),"
            " g.spawn_key('other', 842, 'executor-842-1763300000'))" % (w.root + "/scripts/lib"))
    r = subprocess.run([sys.executable, "-c", code], env=w.env(), capture_output=True, text=True)
    assert r.stdout.split() == ["spawn:executor-842-1763300000", "None", "spawn:executor-842-1763300000"], r.stderr
