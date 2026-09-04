#!/usr/bin/env bash
# scripts/install-enforcement-hooks.sh
#
# Idempotent installer: registers enforcement hooks in
# .claude/settings.local.json under hooks.PreToolUse[matcher=Bash].
#
# Hooks registered:
#   hooks/runaway_loop_guard.py   — blocks 'until x; do sleep N' patterns
#   hooks/repo_scope_warn.py      — warns on gh calls missing --repo flag
#
# Usage: bash scripts/install-enforcement-hooks.sh
# Prints each hook status ("installed" or "already installed").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTINGS_FILE="$REPO_ROOT/.claude/settings.local.json"

if [[ ! -f "$SETTINGS_FILE" ]]; then
  echo '{}' > "$SETTINGS_FILE"
fi

python3 - "$SETTINGS_FILE" "$REPO_ROOT" <<'PYEOF'
import json
import sys
import os

settings_path = sys.argv[1]
repo_root = sys.argv[2]

with open(settings_path, "r") as f:
    settings = json.load(f)

hooks_to_add = [
    f"python3 {repo_root}/hooks/runaway_loop_guard.py",
    f"python3 {repo_root}/hooks/repo_scope_warn.py",
]

hooks = settings.setdefault("hooks", {})
pre_tool_use = hooks.setdefault("PreToolUse", [])

def _has_hook(entries, command):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []) or []:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False

# Find or create the Bash matcher entry
bash_entry = next(
    (e for e in pre_tool_use if isinstance(e, dict) and e.get("matcher") == "Bash"),
    None,
)
if bash_entry is None:
    bash_entry = {"matcher": "Bash", "hooks": []}
    pre_tool_use.append(bash_entry)

changed = False
for hook_command in hooks_to_add:
    if _has_hook(pre_tool_use, hook_command):
        print(f"already installed: {os.path.basename(hook_command.split()[-1])}")
        continue
    bash_entry.setdefault("hooks", []).append(
        {"type": "command", "command": hook_command}
    )
    print(f"installed: {os.path.basename(hook_command.split()[-1])}")
    changed = True

if changed:
    tmp_path = settings_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, settings_path)

PYEOF
