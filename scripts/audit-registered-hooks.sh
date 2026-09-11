#!/usr/bin/env bash
# scripts/audit-registered-hooks.sh
#
# Read-only report of every hook registered against this repo's tool calls,
# read from BOTH the user-global ~/.claude/settings.json and this repo's own
# .claude/settings.json (D#2344).
#
# Why this exists: a hook registered in the global settings file applies to
# EVERY project on the machine, including this one, and nothing in this repo
# could previously see it. D#2344 found a different repo's fork of this
# project's sandbox hook registered globally — both hooks fired on every
# tool call here, and the only reason it was noticed was someone reading the
# global settings file by hand. This script makes that visible on demand.
#
# For every registered hook entry it prints, on one line:
#   <label> <STATUS> event=<Event> matcher=<Matcher> command=<as-written>
#           resolved=<path with $CLAUDE_PROJECT_DIR expanded> sha256=<hash>
# where <label> is "project" or "global" and <STATUS> is one of:
#   IN-REPO     resolved path is inside this repo
#   FOREIGN     resolved path is outside this repo (report only, never acted on)
#   UNRESOLVED  command has no absolute-path token to resolve
#
# Degenerate settings files (missing, invalid JSON, no "hooks" key, a
# "hooks" key with no entries, or no PreToolUse entries at all) each print a
# single stated line instead of entry lines.
#
# Hard constraints (see D#2344 failure conditions):
#   - Read-only. Never writes to, edits, or offers to edit either settings
#     file — cleanup of a foreign global registration is a documented
#     manual operator step (scripts/install-sandbox-hook.sh's warning),
#     not this script's job.
#   - Non-blocking. ALWAYS exits 0. The output is the deliverable, not a
#     pass/fail gate — see CLAUDE.md "hooks/ is a Guardrail, Not a Security
#     Boundary": over-blocking is the worse failure mode.
#   - The liveness assertion is on the RESOLVED path, not on presence of
#     the "PreToolUse" string — a foreign-only registration must not read
#     like an in-repo one.
#
# Usage:
#   bash scripts/audit-registered-hooks.sh
#     [--project-settings PATH]  (default: <repo-root>/.claude/settings.json)
#     [--global-settings PATH]   (default: $HOME/.claude/settings.json)
#     [--repo-root PATH]         (default: this script's own repo root; used
#                                 to expand $CLAUDE_PROJECT_DIR and to decide
#                                 IN-REPO vs FOREIGN)
#
# The three override flags exist for tests/test_audit_registered_hooks.sh —
# they let fixtures point at throwaway settings files without touching the
# operator's real $HOME or this repo's own committed .claude/settings.json.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_SETTINGS="$REPO_ROOT/.claude/settings.json"
GLOBAL_SETTINGS="$HOME/.claude/settings.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-settings) PROJECT_SETTINGS="${2:-}"; shift 2 ;;
    --global-settings) GLOBAL_SETTINGS="${2:-}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    *) echo "audit-registered-hooks.sh: unknown argument: $1" >&2; shift ;;
  esac
done

python3 - "$REPO_ROOT" "$PROJECT_SETTINGS" "$GLOBAL_SETTINGS" <<'PYEOF'
import json
import hashlib
import os
import sys

repo_root = os.path.realpath(sys.argv[1])
project_path = sys.argv[2]
global_path = sys.argv[3]


def sha256_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def resolve_command_path(cmd, repo_root):
    """Best-effort resolution: the last whitespace token of the command
    (the interpreter/path pattern every registered entry in this repo
    uses, e.g. 'python3 <path>' or 'bash <path>'), with the literal
    $CLAUDE_PROJECT_DIR token expanded against repo_root -- that is how
    Claude Code expands it at hook-invocation time for a tool call made
    inside THIS repo."""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    token = cmd.strip().split()[-1]
    token = token.replace("$CLAUDE_PROJECT_DIR", repo_root)
    token = os.path.expanduser(token)
    if not os.path.isabs(token):
        return None
    return os.path.realpath(token)


def in_repo(resolved, repo_root):
    if resolved is None:
        return None
    return resolved == repo_root or resolved.startswith(repo_root + os.sep)


def iter_entries(hooks_block):
    """Yield (event, matcher, command_as_written) for every registered
    command hook. Tolerates both the current {matcher, hooks:[...]} schema
    and the legacy flat {matcher, command} schema."""
    if not isinstance(hooks_block, dict):
        return
    for event, entries in hooks_block.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "")
            found_any = False
            for h in entry.get("hooks", []) or []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    found_any = True
                    yield event, matcher, h["command"]
            if not found_any and isinstance(entry.get("command"), str):
                yield event, matcher, entry["command"]


def report(label, path, repo_root):
    print(f"== {label}: {path} ==")
    if not path or not os.path.isfile(path):
        print(f"  {label}: not present")
        return
    try:
        with open(path, "r") as f:
            raw = f.read()
    except OSError as e:
        print(f"  {label}: could not read file ({e})")
        return
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  {label}: invalid JSON ({e})")
        return
    if not isinstance(settings, dict) or "hooks" not in settings:
        print(f"  {label}: no hooks key")
        return
    entries = list(iter_entries(settings["hooks"]))
    if not entries:
        print(f"  {label}: hooks key present but no entries registered")
        return
    pretooluse_seen = False
    for event, matcher, cmd in entries:
        if event == "PreToolUse":
            pretooluse_seen = True
        resolved = resolve_command_path(cmd, repo_root)
        located = in_repo(resolved, repo_root)
        digest = sha256_of(resolved) if resolved else None
        if resolved is None:
            status = "UNRESOLVED"
        elif located:
            status = "IN-REPO"
        else:
            status = "FOREIGN"
        print(
            f"  {label} {status} event={event} matcher={matcher!r} "
            f"command={cmd!r} resolved={resolved} sha256={digest}"
        )
    if not pretooluse_seen:
        print(f"  {label}: no PreToolUse entries registered")


report("project", project_path, repo_root)
report("global", global_path, repo_root)
PYEOF

# Always exit 0 -- report only, never a gate (D#2344 failure condition).
exit 0
