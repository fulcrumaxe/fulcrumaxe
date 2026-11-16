#!/usr/bin/env python3
"""engine-inbox.py: apply the commands the runner left in <state dir>/engine-inbox/ (EB-10c).

  engine-inbox.py [run]      one pass, oldest seq first; prints fixed sentences for the terminal Team Lead

The runner has already verified each command's signature. This reader still judges every one: closed kinds
and strict args, repo, operate, expiry, replay, and the local halt file. A command ends in
engine-inbox/done/<seq>-<id>.outcome.json holding exactly {"outcome":"applied"} or
{"outcome":"refused","refusal":<word>} (mode 0600, temp file then rename), and moves to done/.
Unsafe state (a directory that is a link, not ours or open to others, a file with such a mode, a file that is
not JSON, a replay record of the wrong shape) is left alone: nothing is written for it. Valid JSON that is not a
valid command (any shape, any depth) is refused as signature_invalid, the closed list having no better word;
so is a command whose processing raised. One command never stops the ones behind it.

Terminal Team Lead path: start_day, pause_loop, resume_loop, request_spawn and stop_agent each become one
notice file engine-notices/<seq>-<id>.json (0600). The outcome "applied" is written only after that file is
durable, and means "delivered to the Team Lead channel". Every tick prints every notice still there; the Team
Lead removes a notice once it has acted on it.

Host hand-off (EB-12b): engine-host.json {"pid": int, "heartbeat_at": ISO-8601 UTC} with a heartbeat no older than
60 s (and a live pid) means the host is running. Then the command moves, still unjudged-by-host, into
engine-host-inbox/<seq>-<id>.json and the reader writes NO outcome: the host writes
engine-inbox/done/<seq>-<id>.outcome.json itself (applied, or refused with host_not_running / unknown_agent) and
removes its file. When the heartbeat goes stale with files pending, the next tick takes them back through the
terminal path (halt, expiry and repo are re-checked; replay already counted at hand-off).
exit: 0 pass done   2 inbox or config unsafe (nothing written)
"""
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [os.path.join(ROOT, "scripts", "lib"), ROOT]
import cloud_decide as cd  # noqa: E402
import engine_emit  # noqa: E402

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NAME = re.compile(r"([1-9][0-9]{0,15})-(%s)\.json" % UUID, re.I)
UUID_RE = re.compile(UUID, re.I)
HEX16 = re.compile(r"[0-9a-f]{16}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
FILE_MAX = 64 * 1024
STATE = "engine-inbox-state.json"
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")
FIELDS = {"v", "command_id", "command_seq", "runner_id", "repo_gh_id", "kind", "args", "issued_by", "issued_at", "expires_at", "halt_epoch"}
ARGS = {"start_day": set(), "pause_loop": set(), "resume_loop": set(), "answer_ask": {"ask_id", "choice", "note"},
        "set_dial": {"dial_class", "level", "ttl"}, "request_spawn": {"role", "discussion"}, "stop_agent": {"agent_id"}}
HANDED = ("start_day", "pause_loop", "resume_loop", "request_spawn", "stop_agent")
HANDOFF = "<handed to the host>"  # internal: not an outcome word
HOST_FRESH_S = 60
TMP = re.compile(r"\.[^/]+\.[0-9a-f]{16}\.tmp")
TMP_STALE_S = 600


class Unsafe(Exception):
    pass


def _int(v, lo=0, hi=None):
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def _time(v):
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t.timestamp() if t.tzinfo is not None else None


def valid_args(kind, a, settable):
    if not isinstance(a, dict) or not set(a) <= ARGS[kind]:
        return False
    if kind == "answer_ask":
        note = a.get("note")
        return (isinstance(a.get("ask_id"), str) and HEX16.fullmatch(a["ask_id"]) is not None and isinstance(a.get("choice"), str) and a["choice"] in ("approve", "decline", "defer")
                and (note is None or (isinstance(note, str) and len(note) <= 500 and not CONTROL.search(note))))
    if kind == "set_dial":
        dc, ttl = a.get("dial_class"), a.get("ttl")
        return (isinstance(dc, str) and dc in settable and _int(a.get("level"), 1, 5) and isinstance(ttl, str) and ttl in ("for_today", "none"))
    if kind == "request_spawn":
        role = a.get("role")
        return isinstance(role, str) and role in cd.ROLES and role != "other" and _int(a.get("discussion"), 1)
    if kind == "stop_agent":
        return isinstance(a.get("agent_id"), str) and AGENT_ID.fullmatch(a["agent_id"]) is not None
    return not a


def valid_command(c, name, settable):
    try:
        return _valid_command(c, name, settable)
    except Exception:  # a shape no check above foresaw is a refusal, never a crash
        return False


def _valid_command(c, name, settable):
    if not isinstance(c, dict) or set(c) != FIELDS or not _int(c["v"], 1, 1) or not isinstance(c["kind"], str) or c["kind"] not in ARGS:
        return False
    if not all(isinstance(c[k], str) and UUID_RE.fullmatch(c[k]) for k in ("command_id", "runner_id", "issued_by")):
        return False
    if not (_int(c["command_seq"], 1) and _int(c["repo_gh_id"], 1) and _int(c["halt_epoch"])):
        return False
    issued, expires = _time(c["issued_at"]), _time(c["expires_at"])
    if issued is None or expires is None or not 0 < expires - issued <= 600:
        return False
    if name != "%d-%s" % (c["command_seq"], c["command_id"].lower()):
        return False
    return valid_args(c["kind"], c["args"], settable)


def check_dir(path, missing_ok=False):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise Unsafe
    except OSError:
        raise Unsafe
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Unsafe
    return True


def read_command(path):
    """(parsed JSON, ok). A link, a file not ours, one open to others, a big or non-JSON file is not ok."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > FILE_MAX:
            return None, False
        return json.loads(os.read(fd, FILE_MAX + 1).decode("utf-8")), True
    except RecursionError:
        return None, True  # valid-sized text nested too deep to parse: a command that is refused, not one that is skipped
    except (OSError, ValueError):
        return None, False
    finally:
        os.close(fd)


def write_private(path, value, replace=True):
    """Random-named temp file (0600, no follow, fsynced), then rename. Without replace the target is linked into
    place, which never overwrites: an existing target is left alone (False)."""
    d, base = os.path.split(path)
    tmp = os.path.join(d, ".%s.%s.tmp" % (base, secrets.token_hex(8)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(value, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.rename(tmp, path)
            return True
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clean_stale_tmp(d):
    """Remove our own leftover temp files (a run that died between create and rename) older than ten minutes."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    now = datetime.now(timezone.utc).timestamp()
    for n in names:
        if not TMP.fullmatch(n):
            continue
        try:
            st = os.lstat(os.path.join(d, n))
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime > TMP_STALE_S:
                os.unlink(os.path.join(d, n))
        except OSError:
            pass


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    check_dir(path)


def sanitize(note):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lib", "external_intake_gate.py"), "sanitize"], input=note,
                       capture_output=True, text=True, timeout=30, env=dict(os.environ))
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def team_log(line):
    try:
        subprocess.run(["bash", os.path.join(ROOT, "scripts", "rotate-team-log.sh"), "comment", line], capture_output=True,
                       timeout=60, env=dict(os.environ), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass  # the local answer file is the record; the team-log reply is a courtesy


class Reader:
    def __init__(self):
        from backend import cloud_ceiling, dial_registry  # noqa: PLC0415
        self.ceiling, self.dials = cloud_ceiling, dial_registry
        self.sdir = cd.state_dir()
        self.inbox = os.path.join(self.sdir, "engine-inbox")
        self.done = os.path.join(self.inbox, "done")
        self.notices = []

    def halted(self):
        try:
            return cd.halted_locally()
        except cd.Refused:
            return True  # a halt file we cannot read is a halt

    def state(self):
        try:
            s = cd._read_json_nofollow(os.path.join(self.sdir, STATE), 256 * 1024, "state")
        except cd.Refused:
            raise Unsafe  # without the replay record nothing can be judged
        if s is None:
            return {"v": 1, "last_seq": {}, "ids": []}  # first run: no record yet
        if not isinstance(s, dict) or not isinstance(s.get("last_seq"), dict) or not isinstance(s.get("ids"), list):
            raise Unsafe  # a record of the wrong shape must not silently reopen replay
        return s

    def judge(self, c, bridge, now, state, replay=True):
        """The refusal word for a command that must not be applied, else None."""
        if c["repo_gh_id"] != bridge.get("repo_gh_id") or isinstance(bridge.get("repo_gh_id"), bool):
            return "wrong_repo"
        if not cd.operate_enabled(bridge):
            return "operate_off"
        if _time(c["expires_at"]) <= now:
            return "expired"
        if replay and (c["command_seq"] <= state["last_seq"].get(str(c["repo_gh_id"]), 0) or c["command_id"].lower() in state["ids"]):
            return "seq_not_increasing"
        if c["kind"] != "answer_ask" and self.halted():
            return "halted"
        return None

    def apply(self, c, entry=None):
        """None when applied, HANDOFF when the host now owns the command, else a refusal word."""
        kind, a = c["kind"], c["args"]
        if kind == "set_dial":
            cap = self.ceiling.ceiling_for(a["dial_class"])
            level = min(a["level"], 5 if cap is None else cap)
            try:
                self.dials.set_dial(a["dial_class"], level, ttl="for-today" if a["ttl"] == "for_today" else None,
                                    source={"kind": "cloud_command", "command_id": c["command_id"].lower(), "issued_by": c["issued_by"].lower()})
            except self.dials.DialCeilingExceeded:
                return "ceiling"
            except ValueError as e:
                # not a ceiling hit: a missing allowlist entry means this engine is not set up to take the command
                return "operate_off" if "allowlist" in str(e) else "signature_invalid"
            return None
        if kind == "answer_ask":
            return self.answer(c)
        who = c["command_id"].lower()
        text = {"start_day": "Owner asked to start the day (cloud command %s)." % who,
                "pause_loop": "Owner asked to pause the loop (cloud command %s)." % who,
                "resume_loop": "Owner asked to resume the loop (cloud command %s)." % who,
                "request_spawn": "Owner requested %s for D#%d (cloud command %s). Run the normal spawn checks; the spawn decide must carry command_id %s." % (a.get("role"), a.get("discussion", 0), who, who),
                "stop_agent": "Owner asked to stop agent %s (cloud command %s). Stop it with TaskStop." % (a.get("agent_id"), who)}[kind]
        if entry is not None and self.hand_to_host(c, entry):
            return HANDOFF
        return self.deliver(c, text)

    def deliver(self, c, text):
        """The terminal path: one durable notice file per command. None once it is on disk, else a refusal word."""
        try:
            d = os.path.join(self.sdir, "engine-notices")
            private_dir(d)
            write_private(os.path.join(d, "%d-%s.json" % (c["command_seq"], c["command_id"].lower())),
                          {"v": 1, "command_id": c["command_id"].lower(), "text": text}, replace=False)  # already there is durable too
        except (OSError, Unsafe):
            return "host_not_running"  # nothing could carry it to the Team Lead, so it is not "applied"
        return None

    def host_live(self):
        """engine-host.json {pid, heartbeat_at}: a heartbeat at most 60 s old and a pid that exists."""
        try:
            host = cd._read_json_nofollow(os.path.join(self.sdir, "engine-host.json"), 4096, "host")
        except cd.Refused:
            return False
        if not isinstance(host, dict) or not _int(host.get("pid"), 2):
            return False
        beat = _time(host.get("heartbeat_at"))
        age = datetime.now(timezone.utc).timestamp() - beat if beat is not None else None
        if age is None or not -5 <= age <= HOST_FRESH_S:
            return False
        try:
            os.kill(host["pid"], 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def hand_to_host(self, c, entry):
        """Move the command file into engine-host-inbox/ when the host is live. The host owns the outcome from here."""
        if not self.host_live():
            return False
        src = os.path.join(self.inbox, entry)
        try:
            d = os.path.join(self.sdir, "engine-host-inbox")
            private_dir(d)
            dst = os.path.join(d, entry.lower())
            os.link(src, dst)  # never overwrites
        except (OSError, Unsafe):
            return False
        try:
            os.unlink(src)
        except OSError:
            try:
                os.unlink(dst)
            except OSError:
                pass
            return False
        return True

    def take_back(self, bridge, settable):
        """Commands left in engine-host-inbox/ by a host whose heartbeat has gone stale go through the terminal path."""
        d = os.path.join(self.sdir, "engine-host-inbox")
        try:
            if not check_dir(d, missing_ok=True):
                return
        except Unsafe:
            print("engine-inbox: engine-host-inbox is not safe; its commands were left alone", file=sys.stderr)
            return
        clean_stale_tmp(d)
        if self.host_live():
            return
        entries = sorted((int(m.group(1)), e) for e in os.listdir(d) for m in [NAME.fullmatch(e)] if m)
        for _seq, entry in entries[:200]:
            name = entry[:-5].lower()
            path = os.path.join(d, entry)
            outcome_path = os.path.join(self.done, name + ".outcome.json")
            if os.path.lexists(outcome_path):
                try:
                    os.unlink(path)  # the host finished it and left its copy behind
                except OSError:
                    pass
                continue
            c, ok = read_command(path)
            if not ok:
                continue
            try:
                if not valid_command(c, name, settable):
                    refusal = "signature_invalid"
                else:
                    refusal = self.judge(c, bridge, datetime.now(timezone.utc).timestamp(), None, replay=False) or self.apply(c)
            except Exception:
                refusal = "signature_invalid"
            body = {"outcome": "applied"} if refusal is None else {"outcome": "refused", "refusal": refusal}
            try:
                write_private(outcome_path, body, replace=False)
                os.rename(path, os.path.join(self.done, entry.lower()))
            except OSError:
                pass

    def print_pending(self):
        """Every notice file still there, oldest first, one fixed sentence each."""
        d = os.path.join(self.sdir, "engine-notices")
        check_dir(self.sdir)
        if not check_dir(d, missing_ok=True):
            return
        for _seq, entry in sorted((int(m.group(1)), e) for e in os.listdir(d) for m in [NAME.fullmatch(e)] if m)[:50]:
            n, ok = read_command(os.path.join(d, entry))
            text = n.get("text") if ok and isinstance(n, dict) else None
            if isinstance(text, str) and len(text) <= 1000 and not CONTROL.search(text):
                self.notices.append(text)

    def answer(self, c):
        a = c["args"]
        cpath = os.path.join(self.sdir, "engine-team-log-cursor.json")
        try:
            cur = cd._read_json_nofollow(cpath, 1024 * 1024, "cursor")
        except cd.Refused:
            cur = None
        asks = cur.get("open_asks") if isinstance(cur, dict) else None
        if not isinstance(asks, dict) or a["ask_id"] not in asks:
            return "unknown_ask"
        user = c["issued_by"].lower()
        rec = {"v": 1, "ask_id": a["ask_id"], "choice": a["choice"], "answered_by": user, "command_id": c["command_id"].lower(),
               "answered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if a.get("note"):
            clean = sanitize(a["note"])
            if clean is not None:  # a note that cannot be sanitized is dropped, the choice still counts
                rec["note"] = "Owner answer via cloud (user %s): %s" % (user, clean)
        try:
            private_dir(os.path.join(self.sdir, "asks"))
            write_private(os.path.join(self.sdir, "asks", a["ask_id"] + ".answer.json"), rec)
        except (OSError, Unsafe):
            return "unknown_ask"
        team_log("[%s] team-lead: cloud answer for ask %s: %s (user %s)" % (datetime.now().strftime("%H:%M"), a["ask_id"], a["choice"], user))
        if a["choice"] != "defer":
            try:
                engine_emit.emit("ask_cleared", {"ask_id": a["ask_id"], "how": "answered_cloud"}, self.sdir)
            except BaseException:
                pass  # a refused event must not undo the answer
            del asks[a["ask_id"]]
            engine_emit._write_atomic(cpath, cur, ".cursor-")
        return None

    def run(self):
        check_dir(self.sdir)
        if not check_dir(self.inbox, missing_ok=True):
            return
        check_dir(self.done)
        try:
            bridge = cd.load_bridge(ROOT)
            cd.operate_enabled(bridge)
        except cd.Refused:
            raise Unsafe
        settable = self.dials.SETTABLE_DIAL_CLASSES
        entries = sorted((int(m.group(1)), e) for e in os.listdir(self.inbox) for m in [NAME.fullmatch(e)] if m)
        state = self.state()
        for d in (self.sdir, self.done, os.path.join(self.sdir, "engine-notices")):
            clean_stale_tmp(d)
        self.take_back(bridge, settable)
        for _seq, entry in entries[:200]:
            try:
                self.one(entry, bridge, settable, state)
            except Exception:
                continue  # whatever it was, the commands behind this one still get their turn

    def one(self, entry, bridge, settable, state):
        name = entry[:-5].lower()
        c, ok = read_command(os.path.join(self.inbox, entry))
        if not ok:
            return
        outcome_path = os.path.join(self.done, name + ".outcome.json")
        valid = False
        try:
            if os.path.lexists(outcome_path) or os.path.lexists(os.path.join(self.done, name + ".acked.json")):
                refusal = "seq_not_increasing"  # already dealt with: never apply twice, never overwrite the outcome
            elif not valid_command(c, name, settable):
                refusal = "signature_invalid"
            else:
                valid = True
                refusal = self.judge(c, bridge, datetime.now(timezone.utc).timestamp(), state) or self.apply(c, entry)
        except Exception:
            refusal = "signature_invalid"
        if valid and refusal in (None, "ceiling", HANDOFF):
            state["last_seq"][str(c["repo_gh_id"])] = max(c["command_seq"], state["last_seq"].get(str(c["repo_gh_id"]), 0))
            state["ids"] = (state["ids"] + [c["command_id"].lower()])[-500:]
            if refusal != HANDOFF:
                write_private(outcome_path, {"outcome": "applied"} if refusal is None else {"outcome": "refused", "refusal": refusal}, replace=False)
            write_private(os.path.join(self.sdir, STATE), state)
            if refusal == HANDOFF:
                return  # the host writes the outcome
        else:
            write_private(outcome_path, {"outcome": "applied"} if refusal is None else {"outcome": "refused", "refusal": refusal}, replace=False)
        try:
            os.rename(os.path.join(self.inbox, entry), os.path.join(self.done, entry.lower()))
        except OSError:
            pass


def main():
    r = Reader()
    try:
        r.run()
    except Unsafe:
        print("engine-inbox: the inbox, its state or the config is not safe; nothing was written", file=sys.stderr)
        return 2
    except Exception as e:  # a bug here must not look like a pass
        print("engine-inbox: failed (%s)" % type(e).__name__, file=sys.stderr)
        return 1
    try:
        r.print_pending()  # also when nothing new came in: an unhandled notice is shown on every tick
    except (Unsafe, OSError):
        pass
    for line in r.notices:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
