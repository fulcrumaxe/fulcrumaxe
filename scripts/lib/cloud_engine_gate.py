#!/usr/bin/env python3
"""cloud_engine_gate.py: the cloud decide gate for engine spawns and Spec approvals (EB-10b).

Callers: scripts/lib/cloud-spawn-gate.sh (pre-spawn-check.sh and the PreToolUse(Agent) hook) and
backend/discussion_outbox.py (the one place a project-manager Spec is flipped to SPEC_READY).
Everything goes through cloud_decide.decide(), so cloud_bridge.operate absent or false changes nothing.

  cloud_engine_gate.py check --role R --spawn-id ID [--discussion N] [--command-id U] [--repo-root DIR]
  cloud_engine_gate.py hook  [--repo-root DIR]        (PreToolUse payload on stdin)
exit:   0 go ahead (act, announce, or operate off)   3 ask   4 halted   5 unusable (fail closed)
stdout: "announce receipt=<uuid>" on an announce, nothing else. stderr: one fixed reason line.
On a refusal it emits spawn_blocked (reason cloud_denied | halted | cloud_unreachable).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cloud_decide as cd  # noqa: E402

EVENT_ID = re.compile(r"[a-z]+(?:-[a-z]+)*-(?:[0-9]+|nod)-[0-9]{9,12}")  # spawn-agent.sh's hook_event_id
TRAILER = re.compile(r"^hook_event_id=([a-z]+(?:-[a-z]+)*-(?:[0-9]+|nod)-[0-9]{9,12})$", re.MULTILINE)
TRAILER_PARTS = re.compile(r"([a-z]+(?:-[a-z]+)*)-([0-9]+|nod)-[0-9]{9,12}")
SPAWN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
TITLE_TAG = re.compile(r"\s*\[([A-Za-z]+)\]")
REASON = {cd.EXIT_ASK: "cloud_denied", cd.EXIT_HALTED: "halted", cd.EXIT_UNREACHABLE: "cloud_unreachable"}


def spawn_key(role, discussion, spawn_id):
    """spawn:<hook_event_id> when the id has the shape spawn-agent.sh builds (role, discussion and a
    timestamp are inside it, and the Agent() hook reads the same id from the prompt, so one spawn is one
    decision). Otherwise spawn:<role>:<discussion|nod>:<id>. None when no usable spawn id was given."""
    sid = spawn_id if isinstance(spawn_id, str) else ""
    if not SPAWN_ID.fullmatch(sid):
        return None
    if EVENT_ID.fullmatch(sid):
        return "spawn:" + sid
    return "spawn:%s:%s:%s" % (role, discussion or "nod", sid)


def tag_of_title(title):
    """The Discussion's tag from its title prefix; None when there is no prefix at all."""
    m = TITLE_TAG.match(title) if isinstance(title, str) else None
    if not m:
        return None
    if TITLE_TAG.match(title, m.end()):
        return None  # a second bracket prefix ([Small][Feature]): which one counts is unclear, so no tag
    t = m.group(1).lower()
    return t if t in cd.TAGS and t != "other" else "other"


def _operate_on(repo_root):
    """True when the cloud gate applies; a config that cannot be read counts as on (fail closed)."""
    try:
        return cd.operate_enabled(cd.load_bridge(repo_root))
    except cd.Refused:
        return True


def _emit_blocked(repo_root, role, discussion, reason):
    cmd = ["timeout", "10", "bash", os.path.join(repo_root, "scripts", "engine-emit.sh"), "spawn_blocked",
           "--role", role, "--reason", reason]
    if discussion:
        cmd += ["--discussion", str(discussion)]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def _message(code, reply):
    if code == cd.EXIT_HALTED:
        return "engine halted"
    if code == cd.EXIT_ASK:
        ask = (reply or {}).get("ask_id") or "-"
        if (reply or {}).get("answered") == "decline":
            return "declined by a person in the cloud (ask %s)" % ask
        return "waiting for a person in the cloud (ask %s)" % ask
    return "cloud unreachable; spawn refused (fail-closed)"


def gate_spawn(repo_root, role_raw, discussion, spawn_id, command_id=None, engine_role=None):
    """Returns (exit code, fixed message, reply). Never raises."""
    role = role_raw if role_raw in cd.ROLES else "other"
    erole = engine_role or role
    disc = int(discussion) if re.fullmatch(r"[1-9][0-9]{0,15}", str(discussion or "")) else None
    try:
        key = spawn_key(role_raw, disc, spawn_id)
        if key is None:
            # No usable spawn id: with operate off nothing is asked or refused; with it on this is a refusal.
            code, reply = (cd.EXIT_UNREACHABLE if _operate_on(repo_root) else cd.EXIT_ACT), None
        else:
            code, _o, reply = cd.decide(repo_root, "engine_spawn", key, {"role": role, "discussion": disc}, command_id)
    except BaseException:
        code, reply = cd.EXIT_UNREACHABLE, None
    if code == cd.EXIT_ACT:
        return code, "", reply
    _emit_blocked(repo_root, erole, disc, REASON.get(code, "cloud_unreachable"))
    return code, _message(code, reply), reply


def gate_spec_ready(repo_root, discussion, title, body_text):
    """Decide engine_spec_ready for flipping a Spec. Returns (exit code, fixed message, reply). Never raises.
    The key binds the Spec text, so an old act cannot be replayed for a Spec that has since changed."""
    try:
        digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()[:16]
        subject = {"discussion": discussion}
        tag = tag_of_title(title)
        if tag:
            subject["tag"] = tag  # a missing tag is sent as no tag, and the cloud asks
        code, _outcome, reply = cd.decide(repo_root, "engine_spec_ready", "spec:%s:%s" % (discussion, digest), subject)
    except BaseException:
        return cd.EXIT_UNREACHABLE, "cloud unreachable; Spec not flipped (fail-closed)", None
    if code == cd.EXIT_ACT:
        return code, "", reply
    return code, _message(code, reply).replace("spawn refused", "Spec not flipped"), reply


def _same_role(trailer_role, subagent_type):
    """The trailer's role segment and the call's subagent_type name the same role (an unknown role must match by name)."""
    from engine_agent_events import role_of  # noqa: PLC0415
    a, b = role_of(trailer_role), role_of(subagent_type)
    return a == b and (a != "other" or trailer_role == str(subagent_type or "").rsplit(":", 1)[-1].strip().lower())


def _hook(repo_root):
    from engine_agent_events import role_of, posint  # noqa: PLC0415
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = None
    ti = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(ti, dict) or payload.get("tool_name") not in ("Agent", "Task"):
        # Not a spawn we can describe. Only a configured gate may refuse it.
        if _operate_on(repo_root):
            return cd.EXIT_UNREACHABLE, "cloud unreachable; spawn refused (fail-closed)", None
        return cd.EXIT_ACT, "", None
    prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
    role = role_of(ti.get("subagent_type"))
    mentioned = [posint(x) for x in re.findall(r"(?:\bD#|\bDiscussion #)(\d{1,15})\b", prompt)]
    disc = mentioned[0] if mentioned else None
    sid = payload.get("tool_use_id")
    # The prompt is caller-controlled text. Its last hook_event_id trailer is the spawn's id only when the
    # trailer names this call's role and a Discussion the prompt itself is about; otherwise a quoted trailer
    # from another spawn would replay that spawn's approval. Anything else keys on tool_use_id.
    trailers = TRAILER.findall(prompt)
    if trailers:
        tm = TRAILER_PARTS.fullmatch(trailers[-1])
        if tm and _same_role(tm.group(1), ti.get("subagent_type")) and \
                (tm.group(2) == "nod" or posint(tm.group(2)) in mentioned):
            sid = trailers[-1]
            disc = posint(tm.group(2)) if tm.group(2) != "nod" else None
    return gate_spawn(repo_root, role, disc, sid, os.environ.get("FX_ENGINE_COMMAND_ID") or None)


def main(argv):
    ap = argparse.ArgumentParser(prog="cloud_engine_gate.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--role", required=True)
    c.add_argument("--spawn-id", default="")
    c.add_argument("--discussion", default="")
    c.add_argument("--command-id")
    for p in (c, sub.add_parser("hook")):
        p.add_argument("--repo-root", default=os.path.abspath(os.path.join(HERE, "..", "..")))
    a = ap.parse_args(argv)
    if a.cmd == "check":
        from engine_agent_events import role_of  # noqa: PLC0415
        code, msg, reply = gate_spawn(a.repo_root, a.role, a.discussion, a.spawn_id, a.command_id, role_of(a.role))
    else:
        code, msg, reply = _hook(a.repo_root)
    if msg:
        print(msg, file=sys.stderr)
    if code == cd.EXIT_ACT and reply and reply.get("disposition") == "announce":
        print("announce receipt=%s" % (reply.get("receipt_id") or "-"))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        print("cloud unreachable; spawn refused (fail-closed)", file=sys.stderr)
        sys.exit(cd.EXIT_UNREACHABLE)
