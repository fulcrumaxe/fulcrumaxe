#!/usr/bin/env bash
# scripts/install-bash-retry-warn-hook.sh
#
# Idempotent installer: registers hooks/bash_retry_warn.py in
# ~/.claude/settings.json under hooks.PreToolUse[matcher=Bash].
#
# The bash-retry-warn hook is warn-only (always exits 0). It fires on every
# Bash tool call and prints a warning to stderr when the agent is about to
# retry a cosmetic variant of a recently failed command.
#
# Usage: bash scripts/install-bash-retry-warn-hook.sh
# Prints "installed" on first run, "already installed" on subsequent runs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/hooks/bash_retry_warn.py"
SETTINGS_FILE="$HOME/.claude/settings.json"

if [[ ! -f "$HOOK_SCRIPT" ]]; then
  echo "ERROR: hook script not found at $HOOK_SCRIPT" >&2
  exit 1
fi

mkdir -p "$(dirname "$SETTINGS_FILE")"

if [[ ! -f "$SETTINGS_FILE" ]]; then
  echo '{}' > "$SETTINGS_FILE"
fi

python3 - "$SETTINGS_FILE" "$HOOK_SCRIPT" <<'PYEOF'
import json
import sys
import os

settings_path = sys.argv[1]
hook_path = sys.argv[2]

with open(settings_path, "r") as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
pre_tool_use = hooks.setdefault("PreToolUse", [])

hook_command = f"python3 {hook_path}"

def _has_hook(entry):
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and h.get("command") == hook_command:
            return True
    return False

# Only Bash matcher needed (warn-only hook).
already = any(
    _has_hook(e)
    for e in pre_tool_use
    if isinstance(e, dict) and e.get("matcher") == "Bash"
)

if already:
    print("already installed")
    sys.exit(0)

# Add to the existing Bash matcher entry, or create one.
bash_entry = next(
    (e for e in pre_tool_use if isinstance(e, dict) and e.get("matcher") == "Bash"),
    None,
)
if bash_entry is not None:
    bash_entry.setdefault("hooks", []).append(
        {"type": "command", "command": hook_command}
    )
else:
    pre_tool_use.append({
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": hook_command}],
    })

tmp_path = settings_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp_path, settings_path)
print("installed")
PYEOF
