#!/usr/bin/env python3
"""loop-subsystem-snapshot.py — emit one JSON blob covering all subsystem state.

Reads blackboard / circuit_breaker / audit_trail / workflow_runner / agent_cards /
spawn queue / GitHub Discussions and writes one JSON object to stdout.

Never crashes. On any failure, sets the affected key to null and adds a warning.
Uses only subprocess to call existing CLIs — no backend imports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0.0"

# Matches STATUS in both HTML-comment form (<!-- STATUS:SPEC_READY SINCE:... -->)
# and bare-line form (STATUS: DISCUSSING). Captures only the uppercase token.
_STATUS_RE = re.compile(r"STATUS:\s*([A-Z_]+)")

# ────────────────────────────────────────────────────────────────
# Repo-relative helpers
# ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
AT = REPO_ROOT / ".autonomous-team"
CURSOR_FILE = AT / "loop-snapshot-cursor.json"
EVENT_FEED = AT / "agent-feed.jsonl"
SPAWN_QUEUE = AT / "spawn-queue.json"


def _run(cmd: list[str], **kw) -> tuple[int, str]:
    """Run a subprocess and return (returncode, combined output)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
            **kw,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _json_or_none(text: str) -> object | None:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _truncate_keep_tail(out: str, budget: int = 200) -> str:
    """Truncate to ``budget`` chars, but keep the traceback's last line too.

    A bare ``out[:200]`` keeps only the head of a traceback — the exception
    type and message (e.g. ``ModuleNotFoundError: No module named 'yaml'``)
    is almost always the last line, and it was getting cut off entirely.
    """
    if len(out) <= budget:
        return out
    lines = out.splitlines()
    last_line = next((l for l in reversed(lines) if l.strip()), "")
    head = out[:budget]
    if last_line and last_line not in head:
        return f"{head}... [last line] {last_line}"
    return head


# ────────────────────────────────────────────────────────────────
# Cursor state — makes the snapshot incremental
# ────────────────────────────────────────────────────────────────

def _load_cursor() -> dict:
    try:
        return json.loads(CURSOR_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {"event_offset": 0, "seen_discussion_numbers": []}


def _save_cursor(cursor: dict) -> None:
    try:
        CURSOR_FILE.write_text(json.dumps(cursor, indent=2))
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────────────────────────────────────────────
# Blackboard
# ────────────────────────────────────────────────────────────────

def _blackboard_section(warnings: list[str]) -> dict:
    result: dict = {
        "memory_recent": None,
        "queue_pending": None,
        "queue_active": None,
        "budget": None,
    }

    # memory — list keys under memory/, read up to 5 most recent
    rc, out = _run([sys.executable, "backend/blackboard.py", "list", "memory/"])
    if rc == 0:
        keys = [k.strip() for k in out.splitlines() if k.strip()]
        recent_keys = keys[-5:]
        lessons = []
        for key in recent_keys:
            rc2, val = _run([sys.executable, "backend/blackboard.py", "read", key])
            if rc2 == 0:
                v = _json_or_none(val.strip())
                if v is not None:
                    lessons.append(v)
        result["memory_recent"] = lessons
    else:
        warnings.append(f"blackboard list memory/ failed: {_truncate_keep_tail(out)}")

    # spawn queue from blackboard (team-lead/spawn-queue key, may not exist)
    rc, out = _run([sys.executable, "backend/blackboard.py", "read", "team-lead/spawn-queue"])
    if rc == 0:
        v = _json_or_none(out.strip())
        if isinstance(v, dict):
            result["queue_pending"] = v.get("pending", [])
            result["queue_active"] = v.get("active", [])
    # fallback: read from the JSONL file on disk
    if result["queue_pending"] is None:
        try:
            raw = json.loads(SPAWN_QUEUE.read_text())
            result["queue_pending"] = raw.get("pending", [])
            result["queue_active"] = raw.get("active", [])
        except Exception:  # noqa: BLE001
            # also try spawn_queue.json (underscore variant)
            alt = AT / "spawn_queue.json"
            try:
                raw = json.loads(alt.read_text())
                result["queue_pending"] = raw.get("pending", [])
                result["queue_active"] = raw.get("active", [])
            except Exception:  # noqa: BLE001
                warnings.append("spawn queue not readable from blackboard or disk")

    # budget
    rc, out = _run([sys.executable, "backend/budget.py", "status"])
    if rc == 0:
        v = _json_or_none(out.strip())
        if v is not None:
            result["budget"] = v
        else:
            result["budget"] = {"raw": out.strip()[:200]}
    else:
        warnings.append(f"budget.py status failed: {_truncate_keep_tail(out)}")

    return result


# ────────────────────────────────────────────────────────────────
# Event bus — read agent-feed.jsonl (FileAppender output)
# ────────────────────────────────────────────────────────────────

def _drain_events(cursor: dict, warnings: list[str], advance: bool = True) -> list[dict]:
    """Return events newer than the cursor offset.

    With ``advance=True`` (the default, used by the loop iteration) the cursor's
    ``event_offset`` moves to the end of the feed — these events are consumed.

    With ``advance=False`` (``--no-drain``) the same events are *peeked*: read
    from the committed offset, offset left where it was. The scheduled refresh
    uses this so a snapshot taken every 5 minutes never eats feed entries the
    next loop iteration still has to see.
    """
    offset = cursor.get("event_offset", 0)
    events: list[dict] = []

    if not EVENT_FEED.exists():
        return events

    try:
        lines = EVENT_FEED.read_text(encoding="utf-8", errors="replace").splitlines()
        new_lines = lines[offset:]
        for line in new_lines[-50:]:  # cap at 50 most recent new events
            v = _json_or_none(line.strip())
            if v is not None:
                events.append(v)
        if advance:
            cursor["event_offset"] = len(lines)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"event_bus drain failed: {exc}")

    return events


# ────────────────────────────────────────────────────────────────
# Circuit breaker — list tripped roles
# ────────────────────────────────────────────────────────────────

def _circuit_breaker_section(warnings: list[str]) -> dict:
    """Read circuit breaker state via `summary --json` and return {tripped_roles: [...]}`.

    The output shape is unchanged for back-compat: tripped_roles is a deduplicated
    list of agent role strings extracted from all blocked Discussions.
    """
    rc, out = _run([sys.executable, "backend/circuit_breaker.py", "summary", "--json"])
    if rc != 0:
        warnings.append(f"circuit_breaker summary failed: {_truncate_keep_tail(out)}")
        return {"tripped_roles": None}

    data = _json_or_none(out.strip())
    if not isinstance(data, dict):
        warnings.append(f"circuit_breaker summary: unexpected output: {_truncate_keep_tail(out)}")
        return {"tripped_roles": []}

    tripped = data.get("tripped") or []
    # Deduplicate agent roles across all blocked discussions (back-compat key)
    roles_seen: set[str] = set()
    tripped_roles: list[str] = []
    for entry in tripped:
        role = entry.get("agent") if isinstance(entry, dict) else None
        if role and role not in roles_seen:
            roles_seen.add(role)
            tripped_roles.append(role)

    return {"tripped_roles": tripped_roles}


# ────────────────────────────────────────────────────────────────
# Audit trail — last 5 failed entries
# ────────────────────────────────────────────────────────────────

def _audit_failures(warnings: list[str]) -> list[dict] | None:
    rc, out = _run([
        sys.executable, "backend/audit_trail.py", "query",
        "--action", "fail",
        "--limit", "5",
    ])
    if rc != 0:
        # Fallback: tail 20 and filter locally
        rc2, out2 = _run([sys.executable, "backend/audit_trail.py", "tail", "--n", "20"])
        if rc2 != 0:
            warnings.append(f"audit_trail query/tail failed: {_truncate_keep_tail(out)}")
            return None
        entries = []
        for line in out2.splitlines():
            v = _json_or_none(line.strip())
            if isinstance(v, dict) and v.get("verdict") in ("fail", "needs-fix"):
                entries.append(v)
        return entries[-5:]

    entries = []
    for line in out.splitlines():
        v = _json_or_none(line.strip())
        if v is not None:
            entries.append(v)
    return entries[-5:]


# ────────────────────────────────────────────────────────────────
# GitHub Discussions — incremental scan
# ────────────────────────────────────────────────────────────────

def _resolve_repo_owner_name(warnings: list[str]) -> tuple[str, str] | tuple[None, None]:
    """Resolve (owner, name) for the GitHub Discussions GraphQL query below.

    D#1905: this used to be a hard-coded (owner, name) literal naming this
    repo itself. That's correct for THIS repo's own runtime, but this file ships
    to adopters (see MANIFEST.md) — on their exported copy it would silently
    query OUR Discussions instead of theirs. Resolved the same
    config-then-env precedence as scripts/backfill-accuracy.py's
    `_load_repo()` and ts-backend's `resolveRepo()`, without importing
    backend (see module docstring: this file is subprocess-only by design).

    This module's contract is "never crashes" (see module docstring), so an
    unresolved repo is a warning + no discussions, not a raise.
    """
    repo = None
    try:
        data = json.loads((AT / "config.json").read_text())
        repo = data.get("repo") or None
    except Exception:  # noqa: BLE001
        repo = None
    if not repo:
        repo = os.environ.get("AUTONOMOUS_TEAM_REPO") or None
    if not repo or "/" not in repo:
        warnings.append(
            "GitHub Discussions: could not resolve a repo slug (set "
            'AUTONOMOUS_TEAM_REPO or .autonomous-team/config.json "repo") — skipping'
        )
        return None, None
    owner, name = repo.split("/", 1)
    return owner, name


def _github_discussions(cursor: dict, warnings: list[str]) -> list[dict]:
    seen = set(cursor.get("seen_discussion_numbers", []))

    owner, name = _resolve_repo_owner_name(warnings)
    if not owner:
        return []

    # labels(first:20) — must not be a tighter cutoff than this: intake-approved
    # (or provenance:*) can be pushed past a smaller page by other labels on a
    # busy Discussion, which would silently hide it from the external-intake
    # gate (D#1588 panel Risk 4).
    query = (
        'query { repository(owner:\\"' + owner + '\\", name:\\"' + name + '\\") { '
        'discussions(first:20, orderBy:{field:UPDATED_AT, direction:DESC}) { '
        'nodes { number title body author{login} labels(first:20){nodes{name}} } } } }'
    )

    rc, out = _run([
        "gh", "api", "graphql",
        "-f", f"query={query.replace(chr(92), '')}",
    ])

    if rc != 0:
        # Try with raw query string
        raw_query = (
            'query { repository(owner:"' + owner + '", name:"' + name + '") { '
            'discussions(first:20, orderBy:{field:UPDATED_AT, direction:DESC}) { '
            'nodes { number title body author{login} labels(first:20){nodes{name}} } } } }'
        )
        rc, out = _run([
            "gh", "api", "graphql", "--field",
            f"query={raw_query}",
        ])

    if rc != 0:
        warnings.append(f"GitHub Discussions GraphQL failed: {out[:300]}")
        return []

    data = _json_or_none(out.strip())
    if not isinstance(data, dict):
        warnings.append(f"GitHub Discussions: unexpected output: {_truncate_keep_tail(out)}")
        return []

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("discussions", {})
        .get("nodes", [])
    )

    new_discussions: list[dict] = []
    for node in nodes:
        num = node.get("number")
        if num is None:
            continue
        body = node.get("body", "") or ""
        label_names = [l["name"] for l in (node.get("labels") or {}).get("nodes", [])]

        # Filter: only include discussions with STATUS: or workflow-relevant labels
        is_relevant = (
            "STATUS:" in body
            or any(
                lbl in label_names
                for lbl in ("spec-ready", "discussing", "needs-impl", "in-progress")
            )
        )

        # Determine STATUS from body.
        # Bodies use the HTML-comment form: <!-- STATUS:SPEC_READY SINCE:... -->
        # or occasionally bare-line form: STATUS: DISCUSSING
        # The old startswith("STATUS:") scan never matched the HTML-comment form.
        status = "UNKNOWN"
        m = _STATUS_RE.search(body)
        if m:
            status = m.group(1)

        author_login = ((node.get("author") or {}).get("login")) if isinstance(node.get("author"), dict) else None

        entry = {
            "number": num,
            "title": node.get("title", ""),
            "status": status,
            "labels": label_names,
            "author": author_login,
            "is_new_since_last_run": num not in seen,
            "relevant": is_relevant,
        }
        new_discussions.append(entry)

    # Update cursor with all seen numbers
    all_seen = list(seen | {n["number"] for n in new_discussions})
    cursor["seen_discussion_numbers"] = all_seen

    return new_discussions


# ────────────────────────────────────────────────────────────────
# Workflows and agent cards
# ────────────────────────────────────────────────────────────────

def _workflows_available(warnings: list[str]) -> list[str] | None:
    rc, out = _run([sys.executable, "backend/workflow_runner.py", "list"])
    if rc != 0:
        warnings.append(f"workflow_runner list failed: {_truncate_keep_tail(out)}")
        return None
    names = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith(" ")]
    return names


def _agent_cards(warnings: list[str]) -> list[str] | None:
    rc, out = _run([sys.executable, "backend/agent_cards.py", "list"])
    if rc != 0:
        warnings.append(f"agent_cards list failed: {_truncate_keep_tail(out)}")
        return None
    return [l.strip() for l in out.splitlines() if l.strip()]


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit a JSON blob covering all subsystem state.",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=None,
        metavar="N",
        help=(
            "After writing, exit non-zero if the written file's generated_at "
            "is already older than N seconds (clock-skew sentinel). "
            "Default: no check."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write JSON to this file path instead of stdout.",
    )
    parser.add_argument(
        "--no-drain",
        action="store_true",
        help=(
            "Read-only mode: peek events from the committed cursor offset without "
            "advancing it, and do not write .autonomous-team/loop-snapshot-cursor.json "
            "at all. Use this for any scheduled/periodic run. Without it a refresh "
            "every few minutes would consume agent-feed.jsonl entries out from under "
            "the next loop iteration, which would then treat them as already seen."
        ),
    )
    args = parser.parse_args()

    warnings: list[str] = []
    cursor = _load_cursor()

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

    snapshot = {
        "generated_at": generated_at,
        "schema_version": SCHEMA_VERSION,
        "blackboard": _blackboard_section(warnings),
        "events_drained": _drain_events(cursor, warnings, advance=not args.no_drain),
        "circuit_breaker": _circuit_breaker_section(warnings),
        "discussions": _github_discussions(cursor, warnings),
        "workflows_available": _workflows_available(warnings),
        "agent_cards": _agent_cards(warnings),
        "audit_recent_failures": _audit_failures(warnings),
        # Keep legacy key for backward compatibility
        "snapshot_at": generated_at,
    }

    if warnings:
        snapshot["warnings"] = warnings

    # _github_discussions() also mutates cursor["seen_discussion_numbers"];
    # skipping the save is what keeps --no-drain read-only for both cursor fields.
    if not args.no_drain:
        _save_cursor(cursor)

    output_text = json.dumps(snapshot, indent=2)

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(output_text)

    # --max-age sentinel: verify the file we wrote isn't already stale
    if args.max_age is not None:
        try:
            generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - generated).total_seconds()
            if age > args.max_age:
                print(
                    f"ERROR: snapshot generated_at is {age:.0f}s old, "
                    f"exceeds --max-age {args.max_age}s (clock skew?)",
                    file=sys.stderr,
                )
                sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not verify --max-age: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
