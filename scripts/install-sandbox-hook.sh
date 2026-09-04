#!/usr/bin/env bash
# scripts/install-sandbox-hook.sh
#
# Idempotent installer: registers sandbox hooks in THIS PROJECT's own
# .claude/settings.json — never the user-global ~/.claude/settings.json.
# Registered commands use $CLAUDE_PROJECT_DIR (expanded by Claude Code at
# hook-invocation time, not by this installer) so the same repo cloned to
# two different paths, or two different projects on one machine, never
# clobber each other's registration.
#
# PreToolUse hooks (hooks/sandbox.py) — matchers: Bash, Edit, Write, Agent.
#   Blocks sub-agents from writing outside worktrees, merging PRs, posting gh
#   api mutations, and spawning child agents.
#
# SubagentStop hook (hooks/subagent_stop_dial_audit.py) — defense-in-depth.
#   Scans each completed subagent transcript for Agent() calls from worktree
#   CWDs and emits audit rows if any are found (catches cases where the
#   PreToolUse hook silently failed to fire).
#
# Usage: bash scripts/install-sandbox-hook.sh   (run once per project)
# Prints "installed" or "already installed" as appropriate.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/hooks/sandbox.py"
STOP_HOOK_SCRIPT="$REPO_ROOT/hooks/subagent_stop_dial_audit.py"
SETTINGS_FILE="$REPO_ROOT/.claude/settings.json"

if [[ ! -f "$HOOK_SCRIPT" ]]; then
  echo "ERROR: hook script not found at $HOOK_SCRIPT" >&2
  exit 1
fi

if [[ ! -f "$STOP_HOOK_SCRIPT" ]]; then
  echo "ERROR: SubagentStop hook not found at $STOP_HOOK_SCRIPT" >&2
  exit 1
fi

# Ensure the project's .claude directory exists
mkdir -p "$(dirname "$SETTINGS_FILE")"

# Create settings.json if it doesn't exist
if [[ ! -f "$SETTINGS_FILE" ]]; then
  echo '{}' > "$SETTINGS_FILE"
fi

# Use Python to update settings.json atomically and idempotently.
#
# The commands written below use the LITERAL string $CLAUDE_PROJECT_DIR —
# Claude Code expands it at hook-invocation time, not this installer. Do
# not f-string/interpolate an actual filesystem path in here; that is
# exactly the bug this script exists to fix.
python3 - "$SETTINGS_FILE" "$REPO_ROOT" <<'PYEOF'
import json
import sys
import os
import tempfile

settings_path = sys.argv[1]
repo_root = os.path.realpath(sys.argv[2])

with open(settings_path, "r") as f:
    settings = json.load(f)

# -----------------------------------------------------------------------
# Part 1: PreToolUse entries (Bash, Edit, Write, Agent)
# -----------------------------------------------------------------------

# Claude Code's PreToolUse schema:
#   { "matcher": "<ToolName>", "hooks": [{"type": "command", "command": "..."}] }
# A flat {"matcher","command"} shape is rejected with "hooks: Expected array".
hooks = settings.setdefault("hooks", {})
pre_tool_use = hooks.setdefault("PreToolUse", [])

hook_command = "python3 $CLAUDE_PROJECT_DIR/hooks/sandbox.py"
_HOOK_LEGACY_SUFFIX = "/hooks/sandbox.py"

# Agent was added in D#1136; existing installs may be missing it. This is
# also the allowlist of matchers we are ever willing to rewrite or upgrade
# in place — an entry for any other matcher (e.g. "Read") is never ours to
# touch, even if its command happens to end in /hooks/sandbox.py.
required_matchers = {"Bash", "Edit", "Write", "Agent"}

def _legacy_path_is_ours(cmd):
    """True only for the old absolute-path form THIS repo's installer used
    to write (pre-D#1814) -- i.e. 'python3 <abs-path>/hooks/sandbox.py'
    where <abs-path> resolves under this repo's own root. Never matches an
    unrelated tool's hook that happens to share the file name."""
    if not isinstance(cmd, str):
        return False
    if not cmd.startswith("python3 ") or not cmd.endswith(_HOOK_LEGACY_SUFFIX):
        return False
    candidate = os.path.realpath(cmd[len("python3 "):].strip())
    return candidate == os.path.join(repo_root, "hooks", "sandbox.py")

def _is_our_hook_command(cmd):
    """True for both the current $CLAUDE_PROJECT_DIR form and the old
    absolute-path form this installer used to write (pre-D#1814)."""
    if not isinstance(cmd, str):
        return False
    if cmd == hook_command:
        return True
    return _legacy_path_is_ours(cmd)

def _entry_has_our_hook(entry):
    if not isinstance(entry, dict):
        return False
    # New schema: hooks is a list of {type, command}
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and _is_our_hook_command(h.get("command")):
            return True
    # Legacy flat schema (pre-fix) — count as installed so we can rewrite later
    return _is_our_hook_command(entry.get("command"))

# Check which matchers already have our hook (Bash, Edit, Write, Agent)
installed_matchers = set()
for entry in pre_tool_use:
    if not isinstance(entry, dict):
        continue
    matcher = entry.get("matcher", "")
    if matcher in required_matchers and _entry_has_our_hook(entry):
        installed_matchers.add(matcher)

missing_matchers = required_matchers - installed_matchers

# Rewrite any legacy flat entries we previously wrote into the proper shape.
# Restricted to matchers we manage -- never touches an entry for a matcher
# outside required_matchers, even if its command matches by coincidence.
rewrote_legacy = False
for i, entry in enumerate(list(pre_tool_use)):
    if not isinstance(entry, dict):
        continue
    if entry.get("matcher", "") not in required_matchers:
        continue
    if "command" in entry and "hooks" not in entry and _is_our_hook_command(entry.get("command")):
        pre_tool_use[i] = {
            "matcher": entry.get("matcher", ""),
            "hooks": [{"type": "command", "command": hook_command}],
        }
        rewrote_legacy = True

# Upgrade any new-schema entries still carrying the legacy absolute-path
# command to the $CLAUDE_PROJECT_DIR form, in place. Same matcher
# restriction as above.
for entry in pre_tool_use:
    if not isinstance(entry, dict):
        continue
    if entry.get("matcher", "") not in required_matchers:
        continue
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and h.get("command") != hook_command and _is_our_hook_command(h.get("command")):
            h["command"] = hook_command
            rewrote_legacy = True

# Add entries for any missing matchers
for tool_name in sorted(missing_matchers):
    pre_tool_use.append({
        "matcher": tool_name,
        "hooks": [{"type": "command", "command": hook_command}],
    })

# -----------------------------------------------------------------------
# Part 2: SubagentStop hook (hooks/subagent_stop_dial_audit.py)
# -----------------------------------------------------------------------

subagent_stop = hooks.setdefault("SubagentStop", [])
stop_hook_command = "python3 $CLAUDE_PROJECT_DIR/hooks/subagent_stop_dial_audit.py"
_STOP_HOOK_LEGACY_SUFFIX = "/hooks/subagent_stop_dial_audit.py"

def _legacy_stop_path_is_ours(cmd):
    if not isinstance(cmd, str):
        return False
    if not cmd.startswith("python3 ") or not cmd.endswith(_STOP_HOOK_LEGACY_SUFFIX):
        return False
    candidate = os.path.realpath(cmd[len("python3 "):].strip())
    return candidate == os.path.join(repo_root, "hooks", "subagent_stop_dial_audit.py")

def _is_our_stop_hook_command(cmd):
    if not isinstance(cmd, str):
        return False
    if cmd == stop_hook_command:
        return True
    return _legacy_stop_path_is_ours(cmd)

def _stop_hook_installed():
    for entry in subagent_stop:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []) or []:
            if isinstance(h, dict) and _is_our_stop_hook_command(h.get("command")):
                return True
        # Legacy flat
        if _is_our_stop_hook_command(entry.get("command")):
            return True
    return False

# Upgrade any new-schema entries still carrying the legacy absolute-path
# stop-hook command to the $CLAUDE_PROJECT_DIR form, in place. The
# repo-root gate in _is_our_stop_hook_command is what keeps this from
# ever touching an unrelated tool's SubagentStop entry.
upgraded_stop_hook = False
for entry in subagent_stop:
    if not isinstance(entry, dict):
        continue
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and h.get("command") != stop_hook_command and _is_our_stop_hook_command(h.get("command")):
            h["command"] = stop_hook_command
            upgraded_stop_hook = True

added_stop_hook = False
if not _stop_hook_installed():
    subagent_stop.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": stop_hook_command}],
    })
    added_stop_hook = True

# -----------------------------------------------------------------------
# Write back
# -----------------------------------------------------------------------

if not missing_matchers and not rewrote_legacy and not added_stop_hook and not upgraded_stop_hook:
    print("already installed")
    sys.exit(0)

# Write via mkstemp (unpredictable name, created O_EXCL with mode 0600) so a
# symlink pre-planted at a predictable ".tmp" path by another local user
# can't be followed by the write. .claude/ is not guaranteed to be
# user-only-writable the way the old ~/.claude was.
settings_dir = os.path.dirname(settings_path) or "."
fd, tmp_path = tempfile.mkstemp(prefix=".settings.json.", suffix=".tmp", dir=settings_dir)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, settings_path)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise
print("installed")
PYEOF

# ---------------------------------------------------------------------------
# Read-only warning: a pre-existing GLOBAL sandbox registration this
# installer did not write. We never modify $HOME/.claude/settings.json —
# cleanup there is a documented operator step (wiki/Sub-Agent-Sandbox.md,
# "Cleaning up a pre-existing global registration"). This check never
# changes the installer's exit code.
# ---------------------------------------------------------------------------
GLOBAL_SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$GLOBAL_SETTINGS" ]]; then
  python3 - "$GLOBAL_SETTINGS" <<'PYEOF' || true
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r") as f:
        settings = json.load(f)
except (OSError, json.JSONDecodeError):
    sys.exit(0)

offenders = []
for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
    if not isinstance(entry, dict):
        continue
    matcher = entry.get("matcher", "")
    for h in entry.get("hooks", []) or []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and cmd.endswith("/hooks/sandbox.py"):
            offenders.append((matcher, cmd))
    cmd = entry.get("command")
    if isinstance(cmd, str) and cmd.endswith("/hooks/sandbox.py"):
        offenders.append((matcher, cmd))

if offenders:
    print(f"WARNING: {path} has a pre-existing global sandbox registration", file=sys.stderr)
    print("this installer did not write and will not modify:", file=sys.stderr)
    for matcher, cmd in offenders:
        print(f"  matcher={matcher!r} command={cmd!r}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"Remediation (manual, operator step — not automated):", file=sys.stderr)
    print(f"  Remove the entries above from {path} by hand, one matcher at a", file=sys.stderr)
    print("  time. Before removing an entry, confirm the project it points at", file=sys.stderr)
    print("  already has an equivalent project-local registration for that", file=sys.stderr)
    print("  matcher (re-run this installer from that project's root — it", file=sys.stderr)
    print("  prints 'already installed' if so). The Agent matcher is the one", file=sys.stderr)
    print("  most likely to be missing locally: removing a global Agent entry", file=sys.stderr)
    print("  with nothing project-local to replace it drops that project's", file=sys.stderr)
    print("  only sub-agent spawn gate.", file=sys.stderr)
PYEOF
fi
