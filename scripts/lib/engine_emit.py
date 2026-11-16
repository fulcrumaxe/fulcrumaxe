#!/usr/bin/env python3
"""engine_emit.py: the only writer of engine-events.jsonl.

Every engine source goes through emit(), one JSON line per call, checked against
schemas/engine-events.v1.json before anything is written. The line carries raw
facts only: closed enums, ids and counts, never free text and never a computed
figure. Sequence numbers are per engine instance, handed out under a flock, so
concurrent callers get unique, gapless numbers.

State directory: $AUTONOMOUS_TEAM_STATE_DIR, else ~/.autonomous-forever-state.
Files are mode 0600 and opened O_NOFOLLOW; the directory is 0700 when created.

CLI (see scripts/engine-emit.sh):
  engine_emit.py <kind> [--field value ...]     exit 0 written, 2 refused, 1 I/O failure
"""
import datetime
import fcntl
import glob
import json
import os
import re
import sys
import tempfile
import uuid

EVENTS = "engine-events.jsonl"
INSTANCE = "engine-instance.json"
LOCK = "engine-emit.lock"
ROTATE_BYTES = 50 * 1024 * 1024
ENVELOPE = ("v", "seq", "ts", "engine_instance_id", "kind")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "schemas",
                           "engine-events.v1.json")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class EmitError(Exception):
    """The event is refused. The message names a field, never a value."""


def state_dir():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".autonomous-forever-state")


def load_schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def _check(v, s, defs):
    """Minimal JSON-Schema subset used by the schema file. Returns an error or None."""
    if "$ref" in s:
        return _check(v, defs[s["$ref"].rsplit("/", 1)[1]], defs)
    if "const" in s and (v != s["const"] or type(v) is not type(s["const"])):
        return "wrong constant"
    if "enum" in s and v not in s["enum"]:
        return "not an allowed value"
    t = s.get("type")
    if t == "object":
        if not isinstance(v, dict):
            return "not an object"
        props = s.get("properties", {})
        for k in s.get("required", []):
            if k not in v:
                return "missing %s" % k
        for k, x in v.items():
            if k not in props:
                return "unknown field %s" % k
            e = _check(x, props[k], defs)
            if e:
                return "%s: %s" % (k, e)
    elif t == "array":
        if not isinstance(v, list) or len(v) > s.get("maxItems", len(v)):
            return "bad array"
        for x in v:
            e = _check(x, s["items"], defs)
            if e:
                return e
    elif t == "integer":
        if isinstance(v, bool) or not isinstance(v, int):
            return "not an integer"
        if v < s.get("minimum", v) or v > s.get("maximum", v):
            return "out of range"
    elif t == "string":
        if not isinstance(v, str) or (("pattern" in s) and not re.fullmatch(s["pattern"], v)):
            return "does not match the allowed form"
    elif t == "boolean" and not isinstance(v, bool):
        return "not a boolean"
    return None


def validate(event, schema=None):
    schema = schema or load_schema()
    defs = schema["$defs"]
    kind = event.get("kind")
    if kind not in defs or not any(r["$ref"].endswith("/" + str(kind)) for r in schema["oneOf"]):
        raise EmitError("unknown kind")
    err = _check(event, defs[kind], defs)
    if err:
        raise EmitError(err)


def _read_instance(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            d = json.load(f)
        if (isinstance(d.get("engine_instance_id"), str) and _UUID.fullmatch(d["engine_instance_id"])
                and isinstance(d.get("next_seq"), int) and not isinstance(d["next_seq"], bool)
                and d["next_seq"] >= 0):
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return {"engine_instance_id": str(uuid.uuid4()), "next_seq": 0}


def _write_atomic(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".engine-instance-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def emit(kind, fields, sdir=None):
    """Validate and append one event. Returns the event written."""
    schema = load_schema()
    ev = {"v": 1, "seq": 0, "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "engine_instance_id": str(uuid.uuid4()), "kind": kind}
    ev.update({k: v for k, v in fields.items() if v is not None})
    validate(ev, schema)  # before taking a seq, so a refused event burns none
    if os.environ.get("ENGINE_EMIT_DISABLE") == "1":  # test harnesses: validate, write nothing
        return ev
    sdir = sdir or state_dir()
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    lock = os.open(os.path.join(sdir, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ipath = os.path.join(sdir, INSTANCE)
        inst = _read_instance(ipath)
        ev["seq"], ev["engine_instance_id"] = inst["next_seq"], inst["engine_instance_id"]
        _write_atomic(ipath, {"engine_instance_id": inst["engine_instance_id"],
                              "next_seq": inst["next_seq"] + 1})
        epath = os.path.join(sdir, EVENTS)
        try:
            if os.lstat(epath).st_size >= ROTATE_BYTES:
                os.replace(epath, epath + ".1")
        except FileNotFoundError:
            pass
        fd = os.open(epath, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(ev, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    finally:
        os.close(lock)  # releases the flock
    return ev


def _age(path_or_paths, now):
    """Seconds since the newest of the given paths last changed, or None when none exists."""
    paths = [path_or_paths] if isinstance(path_or_paths, str) else path_or_paths
    times = []
    for p in paths:
        try:
            times.append(os.stat(p).st_mtime)
        except OSError:
            pass
    return max(0, int(now - max(times))) if times else None


def source_ages(sdir=None, repo_root=None, now=None):
    """Age in seconds of each source the engine reads. A missing source is omitted, never 0."""
    sdir = sdir or state_dir()
    root = repo_root or os.environ.get("ENGINE_EMIT_REPO_ROOT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..")
    at = os.path.join(root, ".autonomous-team")
    now = now if now is not None else __import__("time").time()
    ages = {
        "loop": _age(os.environ.get("METRICS_FILE") or os.path.join(at, "loop-metrics.jsonl"), now),
        "team_log": _age(os.path.join(sdir, "engine-team-log-cursor.json"), now),
        "plan": _age(glob.glob(os.path.join(at, "PLAN-*.md")) + [os.path.join(at, "roadmap.json")], now),
        "merge_queue": _age(glob.glob(os.path.join(sdir, "merge-queue", "*.json")), now),
    }
    return {k: v for k, v in ages.items() if v is not None}


def _coerce(raw, prop, defs):
    while "$ref" in prop:
        prop = defs[prop["$ref"].rsplit("/", 1)[1]]
    t = prop.get("type")
    if t == "integer":
        if not re.fullmatch(r"[0-9]{1,16}", raw):
            raise EmitError("not an integer")
        return int(raw)
    if t == "boolean":
        if raw not in ("true", "false"):
            raise EmitError("not a boolean")
        return raw == "true"
    if t in ("object", "array"):
        try:
            return json.loads(raw)
        except ValueError:
            raise EmitError("not JSON")
    return raw


def main(argv):
    if not argv:
        print("usage: engine_emit.py <kind> [--field value ...]", file=sys.stderr)
        return 2
    if os.environ.get("ENGINE_EMIT_FORCE_FAIL") == "1":  # test seam: an emitter that cannot write
        return 1
    kind = {"checkin": "checkin_tick"}.get(argv[0], argv[0])
    try:
        schema = load_schema()
        if kind not in schema["$defs"]:
            raise EmitError("unknown kind")
        props = schema["$defs"][kind]["properties"]
        fields, rest = {}, argv[1:]
        if len(rest) % 2:
            raise EmitError("a field has no value")
        for flag, raw in zip(rest[::2], rest[1::2]):
            name = flag[2:].replace("-", "_") if flag.startswith("--") else ""
            if name not in props or name in ENVELOPE:
                raise EmitError("unknown field %s" % re.sub(r"[^A-Za-z0-9_-]", "?", name)[:40])
            fields[name] = _coerce(raw, props[name], schema["$defs"])
        if kind == "engine_heartbeat" and "source_ages_s" not in fields:
            fields["source_ages_s"] = source_ages()
        emit(kind, fields)
    except EmitError as e:
        print("engine-emit: refused: %s" % e, file=sys.stderr)
        return 2
    except OSError as e:
        print("engine-emit: could not write (%s)" % e.__class__.__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
