#!/usr/bin/env bash
# scripts/mark-memories-hardwired.sh
#
# Post-merge step: adds 'hardwire_status: enforced' frontmatter to the memory
# files corresponding to the 4 rules now enforced by scripts/spawn-agent.sh
# and hooks/runaway_loop_guard.py / hooks/repo_scope_warn.py.
#
# Run once after PR #(D874-sub3) merges:
#   bash scripts/mark-memories-hardwired.sh
#
# Idempotent — safe to run multiple times.

set -euo pipefail

MEMORY_DIR="$HOME/.claude/projects/-home-agent-autonomous-forever/memory"

if [[ ! -d "$MEMORY_DIR" ]]; then
  echo "WARN: memory dir not found at $MEMORY_DIR — skipping frontmatter updates" >&2
  exit 0
fi

# List of (filename, description) pairs to update
declare -A FILES_TO_UPDATE
FILES_TO_UPDATE=(
  [feedback_concurrency_caps.md]="concurrency cap"
  [feedback_no_runaway_loops.md]="runaway loop prevention"
  [feedback_use_all_subsystems.md]="repo-scope / use all subsystems"
)

python3 - "$MEMORY_DIR" <<'PYEOF'
import sys
import os
import re

memory_dir = sys.argv[1]

files = {
    "feedback_concurrency_caps.md": "concurrency cap",
    "feedback_no_runaway_loops.md": "runaway loop prevention",
    "feedback_use_all_subsystems.md": "repo-scope / use all subsystems",
}

for filename, description in files.items():
    path = os.path.join(memory_dir, filename)
    if not os.path.exists(path):
        print(f"  SKIP (not found): {filename}")
        continue

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Already has hardwire_status: enforced — idempotent skip
    if "hardwire_status: enforced" in content:
        print(f"  already marked: {filename}")
        continue

    # Insert after the first '---' line in the frontmatter
    # Pattern: '---\nkey: value\n...\n---'
    # We add the line before the closing '---'
    new_content = re.sub(
        r"^(---\n(?:.*\n)*?)(---)$",
        r"\1hardwire_status: enforced\n\2",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if new_content == content:
        # Fallback: no frontmatter found — prepend minimal frontmatter marker
        print(f"  WARN: no YAML frontmatter found in {filename} — skipping")
        continue

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  marked: {filename} ({description})")

print("done")
PYEOF
