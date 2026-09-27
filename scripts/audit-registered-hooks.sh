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
#           token=<the token that decided STATUS> resolved=<that token's
#           path, $CLAUDE_PROJECT_DIR expanded> sha256=<hash of that path>
# where <label> is "project" or "global" and <STATUS> is one of:
#   IN-REPO     every absolute-path token in the command resolves inside
#               this repo
#   FOREIGN     at least one absolute-path token resolves outside this
#               repo (report only, never acted on) — this wins over
#               IN-REPO even when another token in the same command IS
#               in-repo, because a wrapper puts the real hook's path
#               alongside its own and the wrapper is what actually runs
#   UNRESOLVED  command has no absolute-path token to resolve at all
#
# Every whitespace-separated token in the command is checked, not just the
# last one (D#2533 finding 1): "python3 <path>" and "bash <path>" are
# today's shape for all registered entries, but a wrapper --
#   python3 /foreign/wrapper.py $CLAUDE_PROJECT_DIR/hooks/sandbox.py
# -- puts a second absolute path after the first, and only looking at the
# last token let the wrapper's genuine in-repo target report IN-REPO with a
# hash that matched a known-good value while /foreign/wrapper.py is what
# Claude Code actually executes. The reported token and hash always belong
# to the same file the STATUS is about, so the two can no longer disagree.
#
# Also prints a `[WARN]` marker line (distinct from the per-entry dump
# lines) for either settings file that has a FOREIGN or UNRESOLVED entry,
# and an `[OK]` marker when it doesn't (D#2533 finding 3) — an operator
# scanning the morning output for markers previously had nothing to scan
# for.
#
# Every branch through report() ends in exactly one marker line, including
# the degenerate ones (fix round 1: the "no hooks key" branch used to print
# neither, which is the single most common shape for a global settings file
# and exactly the blind spot finding 3 exists to close). The degenerate
# branches split on whether the file's absence of danger is verified or
# merely assumed:
#   [OK]   file not present, or present with no "hooks" key, or a "hooks"
#          key with no entries -- each of these is a settings file this
#          script can fully read, and reading it confirms nothing is
#          registered.
#   [WARN] file present but unreadable, or present but invalid JSON -- in
#          both cases this script cannot see what the file actually
#          contains, so it cannot claim [OK]; an operator has to look.
#
# Degenerate settings files (missing, unreadable, invalid JSON, no "hooks"
# key, a "hooks" key with no entries, or no PreToolUse entries at all) each
# print a single stated line plus that one marker, instead of entry lines.
#
# Hard constraints (see D#2344 failure conditions):
#   - Read-only. Never writes to, edits, or offers to edit either settings
#     file — cleanup of a foreign global registration is a documented
#     manual operator step (scripts/install-sandbox-hook.sh's warning),
#     not this script's job.
#   - Non-blocking on the reporting path. ALWAYS exits 0 once it starts
#     reading settings — see CLAUDE.md "hooks/ is a Guardrail, Not a
#     Security Boundary": over-blocking is the worse failure mode. This
#     script deliberately does not run under `set -e`: an unrelated failing
#     command must never be able to kill the report. A malformed CLI
#     argument (a flag with no value) is a separate, earlier failure mode —
#     see D#2533 finding 2 below — and is the one place this script exits
#     non-zero.
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
# Each requires a value; a trailing flag with none exits non-zero immediately
# and names the flag, instead of hanging (D#2533 finding 2 — under
# `set -uo pipefail` with no `-e`, a bare `shift 2` on the last argument
# fails silently and never advances $1, spinning the arg-parse loop
# forever).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_SETTINGS="$REPO_ROOT/.claude/settings.json"
GLOBAL_SETTINGS="$HOME/.claude/settings.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-settings)
      if [[ $# -lt 2 ]]; then
        echo "audit-registered-hooks.sh: $1 requires a value" >&2
        exit 1
      fi
      PROJECT_SETTINGS="$2"
      shift 2
      ;;
    --global-settings)
      if [[ $# -lt 2 ]]; then
        echo "audit-registered-hooks.sh: $1 requires a value" >&2
        exit 1
      fi
      GLOBAL_SETTINGS="$2"
      shift 2
      ;;
    --repo-root)
      if [[ $# -lt 2 ]]; then
        echo "audit-registered-hooks.sh: $1 requires a value" >&2
        exit 1
      fi
      REPO_ROOT="$2"
      shift 2
      ;;
    *)
      echo "audit-registered-hooks.sh: unknown argument: $1" >&2
      shift
      ;;
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


def resolve_absolute_tokens(cmd, repo_root):
    """Yield (raw_token, resolved_path) for every whitespace-separated
    token in the command that is an absolute path once the literal
    $CLAUDE_PROJECT_DIR token and a leading ~ are expanded. Every token is
    a candidate, not just the last -- see the D#2533 header comment for
    why the old last-token-only rule was a detector-evasion gap."""
    if not isinstance(cmd, str) or not cmd.strip():
        return
    for token in cmd.strip().split():
        expanded = token.replace("$CLAUDE_PROJECT_DIR", repo_root)
        expanded = os.path.expanduser(expanded)
        if not os.path.isabs(expanded):
            continue
        yield token, os.path.realpath(expanded)


def in_repo(resolved, repo_root):
    if resolved is None:
        return None
    return resolved == repo_root or resolved.startswith(repo_root + os.sep)


def classify_command(cmd, repo_root):
    """Fold every absolute-path token's verdict into one status for the
    command, FOREIGN beating IN-REPO beating UNRESOLVED, and return the
    single token (and its resolved path) that decided it -- the reported
    hash always belongs to that same file, never a different one."""
    candidates = list(resolve_absolute_tokens(cmd, repo_root))
    if not candidates:
        return "UNRESOLVED", None, None
    foreign = [c for c in candidates if not in_repo(c[1], repo_root)]
    if foreign:
        raw, resolved = foreign[0]
        return "FOREIGN", raw, resolved
    raw, resolved = candidates[0]
    return "IN-REPO", raw, resolved


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
        print(f"  {label} [OK] no settings file present -- nothing registered")
        return
    try:
        with open(path, "r") as f:
            raw = f.read()
    except OSError as e:
        print(f"  {label}: could not read file ({e})")
        print(f"  {label} [WARN] settings file present but unreadable -- cannot verify registered hooks")
        return
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  {label}: invalid JSON ({e})")
        print(f"  {label} [WARN] settings file present but invalid JSON -- cannot verify registered hooks")
        return
    if not isinstance(settings, dict) or "hooks" not in settings:
        print(f"  {label}: no hooks key")
        print(f"  {label} [OK] no hooks key present -- nothing registered")
        return
    entries = list(iter_entries(settings["hooks"]))
    if not entries:
        print(f"  {label}: hooks key present but no entries registered")
        print(f"  {label} [OK] no hook commands registered")
        return
    pretooluse_seen = False
    statuses = []
    for event, matcher, cmd in entries:
        if event == "PreToolUse":
            pretooluse_seen = True
        status, token, resolved = classify_command(cmd, repo_root)
        statuses.append(status)
        digest = sha256_of(resolved) if resolved else None
        print(
            f"  {label} {status} event={event} matcher={matcher!r} "
            f"command={cmd!r} token={token!r} resolved={resolved} sha256={digest}"
        )
    if not pretooluse_seen:
        print(f"  {label}: no PreToolUse entries registered")
    if any(s in ("FOREIGN", "UNRESOLVED") for s in statuses):
        print(f"  {label} [WARN] foreign or unresolved hook command registered — review the entries above")
    else:
        print(f"  {label} [OK] every registered hook command resolves in-repo")


report("project", project_path, repo_root)
report("global", global_path, repo_root)
PYEOF

# Always exit 0 on the reporting path -- report only, never a gate
# (D#2344 failure condition). A malformed CLI argument above already
# exited non-zero before reaching here; that is an argument-parse error,
# not a report outcome (D#2533 finding 2).
exit 0
