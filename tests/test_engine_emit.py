"""Engine event emitter: schema parity with the agreed field list, the one-writer rule,
and the behaviour of scripts/engine-emit.sh run through the real wrapper."""
import concurrent.futures
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA = os.path.join(ROOT, "schemas", "engine-events.v1.json")
EMIT = os.path.join(ROOT, "scripts", "engine-emit.sh")
# Pinned for this copy of the schema. Replace with the hash of the shared protocol file
# once that file lands, and copy it over this one byte for byte.
SCHEMA_SHA256 = "898c130e9eed04a48af5ed9f5d28302b61c9dcabdcd5a2b71d682a7eb7b8b8a7"

ENVELOPE = {"v", "seq", "ts", "engine_instance_id", "kind"}
ROLES = {"executor", "code-reviewer", "security-reviewer", "project-manager", "acceptance-tester",
         "browser-tester", "technical-architect", "product-owner", "cost-analyst", "performance-expert",
         "security-expert", "researcher", "mission-analyst", "docs-writer", "debater", "explore"}
# kind -> (required fields, optional fields), written out from the agreed catalogue.
FIELDS = {
    "agent_started": ({"spawn_id", "role", "background", "isolation"}, {"discussion", "model"}),
    "agent_linked": ({"spawn_id", "agent_id"}, set()),
    "agent_stopped": ({"agent_id", "role", "verdict", "credential_mode"},
                      {"discussion", "pr", "duration_s", "model", "usage"}),
    "spawn_blocked": ({"role", "reason"}, {"discussion"}),
    "merge_lane": ({"pr", "state"}, {"discussion"}),
    "merged": ({"pr", "closes"}, {"discussion"}),
    "gate_receipt": ({"pr", "gate", "result", "head_sha"}, set()),
    "team_log_line": ({"line_id", "actor_role", "category"}, {"discussion", "pr"}),
    "loop_tick": ({"origin", "duration_s"}, set()),
    "checkin_tick": ({"name", "origin"}, set()),
    "engine_heartbeat": ({"origin"}, {"source_ages_s"}),
    "plan_snapshot": ({"plan_date", "snapshot_id", "chunk_index", "chunk_count", "items", "roadmap"}, set()),
    "ask_raised": ({"ask_id", "ask_kind", "source"}, {"discussion", "pr"}),
    "ask_cleared": ({"ask_id", "how"}, set()),
    "host_started": (set(), {"reason"}),
    "host_stopped": (set(), {"reason"}),
    "host_heartbeat": (set(), {"reason"}),
    "tick_started": ({"tick_kind"}, {"outcome", "duration_s"}),
    "tick_ended": ({"tick_kind", "credential_mode"}, {"outcome", "duration_s", "model", "usage"}),
    "compaction_detected": (set(), set()),
    "halt_applied": ({"halt_epoch", "method", "survivors"}, set()),
    "bridge_heartbeat": (set(), {"count", "reason"}),
    "bridge_dropped": (set(), {"count", "reason"}),
}
FORBIDDEN = {"text", "message", "content", "output", "prompt", "description", "title", "body", "branch",
             "path", "usd", "cost", "cost_usd", "price"}


def load():
    with open(SCHEMA) as f:
        return json.load(f)


def walk_names(node):
    """Every property name used anywhere in the schema, except inside the token-usage object.

    usage.output is a token count that the field list names explicitly, so it is the one
    place the name "output" is allowed; the usage object's own keys are checked separately.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties":
                yield from v.keys()
                for name, sub in v.items():
                    if name != "usage":
                        yield from walk_names(sub)
            else:
                yield from walk_names(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk_names(x)


def emit(state, *args, check=False):
    env = dict(os.environ, AUTONOMOUS_TEAM_STATE_DIR=str(state))
    return subprocess.run(["bash", EMIT, *args], env=env, capture_output=True, text=True, check=check)


def lines(state):
    p = os.path.join(str(state), "engine-events.jsonl")
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def test_schema_matches_agreed_field_list():
    defs = load()["$defs"]
    assert set(FIELDS) == {r["$ref"].rsplit("/", 1)[1] for r in load()["oneOf"]}
    for kind, (req, opt) in FIELDS.items():
        d = defs[kind]
        assert d["additionalProperties"] is False
        assert set(d["properties"]) == ENVELOPE | req | opt, kind
        assert set(d["required"]) == ENVELOPE | req, kind
        assert d["properties"]["kind"] == {"const": kind}


def test_schema_closed_enums_and_ids():
    defs = load()["$defs"]
    assert set(defs["role"]["enum"]) == ROLES | {"other"}
    assert set(defs["actor_role"]["enum"]) == ROLES | {"team-lead", "other"}
    assert defs["credential_mode"]["enum"] == ["subscription", "api_key"]
    assert defs["spawn_id"]["pattern"] == "^toolu_[A-Za-z0-9]{1,64}$"
    assert defs["agent_id"]["pattern"] == "^[A-Za-z0-9_-]{1,64}$"
    assert defs["hex16"]["pattern"] == "^[0-9a-f]{16}$"
    assert defs["sha40"]["pattern"] == "^[0-9a-f]{40}$"
    assert defs["model_id"]["pattern"] == "^[a-z0-9][a-z0-9.-]{0,63}$"
    assert defs["row_key"]["pattern"] == "^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$"
    assert defs["tokens"]["maximum"] == 1_000_000_000_000
    ps = defs["plan_snapshot"]["properties"]
    assert ps["items"]["maxItems"] == 500 and ps["roadmap"]["maxItems"] == 500
    assert set(ps["roadmap"]["items"]["properties"]["status"]["enum"]) == {
        "not_started", "in_progress", "done", "blocked", "deferred", "split", "removed", "superseded"}


def test_no_free_text_or_computed_field_names():
    names = set(walk_names(load()))
    assert not (names & FORBIDDEN)
    usage = load()["$defs"]["agent_stopped"]["properties"]["usage"]["properties"]
    assert set(usage) == {"input", "output", "cache_read", "cache_write"}
    assert not [n for n in names if n.startswith("total_")]
    # the counters v1.1 removed and the tier it replaced
    assert not (names & {"open_prs", "needs_review", "needs_merge", "needs_fix", "queue_depth",
                         "agents_spawned", "prs_merged", "merged_prs", "model_tier"})


def test_schema_hash_is_pinned():
    digest = hashlib.sha256(open(SCHEMA, "rb").read()).hexdigest()
    print("schemas/engine-events.v1.json sha256", digest)
    assert digest == SCHEMA_SHA256


def test_one_line_validates_and_has_a_modes(tmp_path):
    r = emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "12")
    assert r.returncode == 0, r.stderr
    (ev,) = lines(tmp_path)
    assert ev["kind"] == "loop_tick" and ev["seq"] == 0 and ev["v"] == 1 and ev["duration_s"] == 12
    assert re.fullmatch(r"[0-9a-f-]{36}", ev["engine_instance_id"])
    assert stat.S_IMODE(os.stat(tmp_path / "engine-events.jsonl").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "engine-instance.json").st_mode) == 0o600


def test_every_example_event_validates_with_a_real_validator(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(load())
    cases = [
        ("agent_started", "--spawn-id", "toolu_abc", "--role", "executor", "--background", "true",
         "--isolation", "worktree", "--discussion", "5", "--model", "claude-sonnet-5-5"),
        ("agent_stopped", "--agent-id", "a1", "--role", "other", "--verdict", "pass", "--credential-mode",
         "subscription", "--usage", '{"input":1,"output":2,"cache_read":3,"cache_write":4}'),
        ("ask_raised", "--ask-id", "0123456789abcdef", "--ask-kind", "owner_decision", "--source", "team_log"),
        ("gate_receipt", "--pr", "7", "--gate", "gate1", "--result", "pass", "--head-sha", "a" * 40),
        ("checkin", "--name", "ROW-1", "--origin", "cron"),
        ("compaction_detected",),
    ]
    for c in cases:
        assert emit(tmp_path, *c).returncode == 0, c
    got = lines(tmp_path)
    assert len(got) == len(cases)
    for ev in got:
        v.validate(ev)
    assert [e["seq"] for e in got] == list(range(len(cases)))
    bad = dict(got[0], text="x")
    assert not v.is_valid(bad)
    assert not v.is_valid(dict(got[0], role="Executor"))


@pytest.mark.parametrize("args", [
    ("merged", "--pr", "5", "--closes", "true", "--discussion", "D#5 fix it"),
    ("merged", "--pr", "5", "--closes", "true", "--text", "hello"),
    ("merged", "--pr", "5", "--closes", "true", "--cost", "3"),
    ("merged", "--pr", "5", "--closes", "true", "--seq", "9"),
    ("merged", "--pr", "5"),
    ("merged", "--pr", "0", "--closes", "true"),
    ("merged", "--pr", "5\n", "--closes", "true"),
    ("loop_tick", "--origin", "cron\n", "--duration-s", "1"),
    ("loop_tick", "--origin", "elsewhere", "--duration-s", "1"),
    ("team_log_line", "--line-id", "0123456789abcdeg", "--actor-role", "executor", "--category", "other"),
    ("agent_stopped", "--agent-id", "a b", "--role", "executor", "--verdict", "pass", "--credential-mode",
     "api_key"),
    ("no_such_kind",),
])
def test_free_text_and_bad_fields_are_refused_without_a_line(tmp_path, args):
    r = emit(tmp_path, *args)
    assert r.returncode == 2, r.stderr
    assert not os.path.exists(tmp_path / "engine-events.jsonl")
    assert "hello" not in r.stderr and "fix it" not in r.stderr


def test_refused_event_burns_no_sequence_number(tmp_path):
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    assert emit(tmp_path, "loop_tick", "--origin", "bogus", "--duration-s", "1").returncode == 2
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "2")
    assert [e["seq"] for e in lines(tmp_path)] == [0, 1]


def test_failed_append_gives_the_seq_back_so_no_gap_reaches_disk(tmp_path):
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    events = tmp_path / "engine-events.jsonl"
    saved = tmp_path / "saved"
    events.rename(saved)
    os.symlink(tmp_path / "missing" / "nowhere", events)  # dangling: the append fails
    for _ in range(2):
        assert emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "2").returncode == 1
        assert json.load(open(tmp_path / "engine-instance.json"))["next_seq"] == 1
    events.unlink()
    saved.rename(events)
    assert emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "3").returncode == 0
    assert [e["seq"] for e in lines(tmp_path)] == [0, 1]


def test_two_hundred_concurrent_calls_are_unique_and_gapless(tmp_path):
    def one(i):
        return emit(tmp_path, "loop_tick", "--origin", "host", "--duration-s", str(i)).returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        assert set(ex.map(one, range(200))) == {0}
    got = lines(tmp_path)
    assert len(got) == 200
    assert sorted(e["seq"] for e in got) == list(range(200))
    assert len({e["engine_instance_id"] for e in got}) == 1


def test_deleting_the_instance_file_starts_a_new_instance_at_seq_zero(tmp_path):
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    os.unlink(tmp_path / "engine-instance.json")
    emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    a, b, c = lines(tmp_path)
    assert (a["seq"], b["seq"], c["seq"]) == (0, 1, 0)
    assert a["engine_instance_id"] == b["engine_instance_id"] != c["engine_instance_id"]


def test_symlinked_events_file_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    os.symlink(target, tmp_path / "engine-events.jsonl")
    r = emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1")
    assert r.returncode == 1
    assert target.read_text() == ""


def test_symlinked_instance_file_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps({"engine_instance_id": str(uuid.uuid4()), "next_seq": 41}))
    os.symlink(target, tmp_path / "engine-instance.json")
    assert emit(tmp_path, "loop_tick", "--origin", "cron", "--duration-s", "1").returncode == 0
    assert lines(tmp_path)[0]["seq"] == 0


def test_rotation_at_the_size_limit(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("engine_emit", os.path.join(ROOT, "scripts/lib/engine_emit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "ROTATE_BYTES", 200)
    for i in range(6):
        m.emit("loop_tick", {"origin": "cron", "duration_s": i}, str(tmp_path))
    assert os.path.exists(tmp_path / "engine-events.jsonl.1")
    seqs = [json.loads(x)["seq"] for f in ("engine-events.jsonl.1", "engine-events.jsonl")
            for x in open(tmp_path / f).read().splitlines()]
    assert seqs[-1] == 5 and seqs == sorted(seqs)


def test_heartbeat_reports_each_sources_age_and_omits_missing_ones(tmp_path):
    root, state = tmp_path / "root", tmp_path / "state"
    (root / ".autonomous-team").mkdir(parents=True)
    state.mkdir()
    now = time.time()
    fixtures = {root / ".autonomous-team/loop-metrics.jsonl": 90,
                state / "engine-team-log-cursor.json": 4000,
                root / ".autonomous-team/PLAN-2026-11-16.md": 7200}
    for p, age in fixtures.items():
        p.write_text("x")
        os.utime(p, (now - age, now - age))
    env = dict(os.environ, AUTONOMOUS_TEAM_STATE_DIR=str(state), ENGINE_EMIT_REPO_ROOT=str(root))
    env.pop("METRICS_FILE", None)
    r = subprocess.run(["bash", EMIT, "engine_heartbeat", "--origin", "cron"], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    ages = lines(state)[0]["source_ages_s"]
    assert set(ages) == {"loop", "team_log", "plan"}  # no merge-queue records: omitted, not 0
    for k, want in (("loop", 90), ("team_log", 4000), ("plan", 7200)):
        assert want <= ages[k] <= want + 30


def test_only_the_emitter_writes_the_events_file():
    """L1: no file but engine_emit.py names engine-events.jsonl with a write mode or a redirect."""
    write = re.compile(r"(>>?\s*\S*engine-events\.jsonl)|(tee\b.*engine-events\.jsonl)|"
                       r"(open\([^)]*engine-events[^)]*[\"'][wax+]b?[\"'])|(os\.open\([^)]*engine-events)")
    assert write.search('echo x >> "$STATE/engine-events.jsonl"')
    assert write.search("open('engine-events.jsonl', 'a')")
    assert not write.search("open('engine-events.jsonl', 'r')")
    files = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for rel in files:
        if rel.startswith(("tests/", "wiki/")) or rel.endswith(".md") or rel in (
                "scripts/lib/engine_emit.py", "schemas/engine-events.v1.json"):
            continue
        try:
            text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if "engine-events.jsonl" in text and any(write.search(line) for line in text.splitlines()):
            offenders.append(rel)
    assert offenders == []
