#!/usr/bin/env python3
"""engine_agent_events.py: agent_started / agent_stopped engine events from hook payloads.

  engine_agent_events.py started                       PreToolUse(Agent|Task) payload on stdin
  engine_agent_events.py stopped --agent-id ID --role R --verdict V [--discussion N] [--pr N]
                                 [--transcript P] [--session S] [--repo-root P]

Raw facts only: ids, enums and token counts. The prompt, the description and the last message
never reach a line. Usage comes from the subagent's own transcript (find_own_usage) and is
left out when that cannot be read, never sent as 0. Prints nothing and always exits 0, so a
failure here can never block a spawn or a stop. Events go through scripts/engine-emit.sh.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_emit  # noqa: E402
import subagent_payload  # noqa: E402

EMIT = os.path.join(HERE, "..", "engine-emit.sh")
ROLES = set(engine_emit.load_schema()["$defs"]["role"]["enum"])
VERDICTS = {"pass", "fail", "done", "blocked", "needs_fix", "unknown"}
MODEL = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
DISC = re.compile(r"(?:\bD#|\bDiscussion #)(\d{1,15})\b")
USAGE = {"input": "input_tokens", "output": "output_tokens",
         "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


def role_of(raw):
    r = str(raw or "").rsplit(":", 1)[-1].strip().lower()  # "plugin:executor" -> "executor"
    return r if r in ROLES else "other"


def model_of(raw):
    m = raw.strip().lower() if isinstance(raw, str) else ""
    return m if MODEL.fullmatch(m) else None


def posint(raw):
    return int(raw) if re.fullmatch(r"[0-9]{1,15}", str(raw or "")) and int(raw) > 0 else None


def started(payload):
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
    m = DISC.search(prompt)
    return {"spawn_id": payload.get("tool_use_id"), "role": role_of(ti.get("subagent_type")),
            "background": ti.get("run_in_background") is True,
            "isolation": "worktree" if ti.get("isolation") == "worktree" else "none",
            "discussion": posint(m.group(1)) if m else None, "model": model_of(ti.get("model"))}


def own_model(path):
    """Model of the last assistant record in the subagent's own transcript, or None."""
    found = None
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as f:
            for line in f:
                try:
                    msg = json.loads(line).get("message")
                except (ValueError, AttributeError):
                    continue
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    found = model_of(msg.get("model")) or found
    except OSError:
        return None
    return found


def stopped(a):
    verdict = str(a.verdict or "").lower().replace("-", "_")
    own = subagent_payload.find_own_transcript(a.transcript, a.agent_id, a.session, a.repo_root)
    totals = subagent_payload.find_own_usage(a.transcript, a.agent_id, a.session, a.repo_root)
    return {"agent_id": a.agent_id, "role": role_of(a.role),
            "verdict": verdict if verdict in VERDICTS else "unknown",
            "discussion": posint(a.discussion), "pr": posint(a.pr),
            "credential_mode": "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription",
            "model": own_model(own) if own else None,
            "usage": {k: totals[v] for k, v in USAGE.items()} if totals else None}


def flag_value(v):
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"))
    return str(v).lower() if isinstance(v, bool) else str(v)


def emit(kind, fields):
    args = []
    for k, v in fields.items():
        if v is not None:
            args += ["--" + k.replace("_", "-"), flag_value(v)]
    subprocess.run(["timeout", "10", "bash", EMIT, kind] + args, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=15)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=("started", "stopped"))
    for n in ("agent-id", "role", "verdict", "discussion", "pr", "transcript", "session", "repo-root"):
        ap.add_argument("--" + n, default="")
    a = ap.parse_args(argv)
    if a.which == "started":
        payload = json.load(sys.stdin)
        fields = started(payload) if isinstance(payload, dict) else None
    else:
        fields = stopped(a) if a.agent_id else None
    if fields:
        emit("agent_" + a.which, fields)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BaseException:  # a hook never blocks a spawn or a stop
        pass
    sys.exit(0)
