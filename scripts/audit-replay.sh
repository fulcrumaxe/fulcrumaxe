#!/usr/bin/env bash
# scripts/audit-replay.sh — Hash-chain verification and point-in-time lookup for audit.jsonl.
#
# Usage:
#   bash scripts/audit-replay.sh
#       Walk the full audit chain and print "OK" or name the first broken link.
#       Exit 0 = chain intact; exit 1 = chain broken or file not found.
#
#   bash scripts/audit-replay.sh <CLASS> <YYYY-MM-DDTHH:MM:SSZ>
#       Print:
#         (a) dial state for CLASS at the given timestamp
#         (b) all dial changes ±1h around it
#         (c) all spawn/agent actions recorded under that dial level
#       Exit 0 = found; exit 1 = not found or error.
#
# Environment:
#   AUTONOMOUS_TEAM_STATE_DIR — overrides the default state dir (for tests).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Resolve audit.jsonl path ────────────────────────────────────────────────
_AUDIT_PATH=$(python3 -c "
import sys, os
sys.path.insert(0, '$REPO_ROOT')
try:
    from backend.state_paths import STATE_DIR
    print(str(STATE_DIR / 'audit.jsonl'))
except Exception as e:
    # Fallback: use AUTONOMOUS_TEAM_STATE_DIR env var
    state_dir = os.environ.get('AUTONOMOUS_TEAM_STATE_DIR', os.path.expanduser('~/.fulcrumaxe-state'))
    print(os.path.join(state_dir, 'audit.jsonl'))
" 2>/dev/null)

if [[ -z "$_AUDIT_PATH" ]]; then
  echo "ERROR: could not resolve audit.jsonl path" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  # ── Mode 1: Full chain verification ─────────────────────────────────────
  # Dial rows carry prev_hash = SHA-256 of the preceding row in the file
  # (which may be a control-plane row, not another dial row).  Control-plane
  # and other row kinds carry no prev_hash and are not themselves verified —
  # they exist only as hash-chain anchors for subsequent dial rows.
  python3 - "$_AUDIT_PATH" <<'_PYEOF'
import sys, json, hashlib, pathlib

audit_path = pathlib.Path(sys.argv[1])

if not audit_path.exists():
  print(f"ERROR: audit.jsonl not found at {audit_path}", file=sys.stderr)
  sys.exit(1)

content = audit_path.read_bytes()
all_lines = [l for l in content.split(b"\n") if l.strip()]

# Build a set of all line-hashes so we can verify each dial row's prev_hash
# points to a line that actually exists in the file.
all_hashes: set[str] = {hashlib.sha256(l).hexdigest() for l in all_lines}

# Extract dial rows (those carrying prev_hash) in file order.
# We need both the line bytes (for hashing) and the parsed row (for reporting).
dial_entries: list[tuple[int, bytes, dict]] = []  # (file_line_idx, bytes, parsed)
for idx, line in enumerate(all_lines):
  try:
    row = json.loads(line)
    if "prev_hash" in row:
      dial_entries.append((idx, line, row))
  except json.JSONDecodeError:
    pass  # non-JSON lines are silently skipped

if not dial_entries:
  print("OK (0 dial rows, chain intact)")
  sys.exit(0)

# For each dial row: verify its prev_hash exists as a real line in the file.
# The chain design is: prev_hash = sha256(last_line_in_file_at_write_time),
# which is the immediately preceding line when written sequentially.
#
# Special case: "genesis" is the sentinel stored by _read_last_audit_hash()
# when the file is empty at write time (i.e. the very first dial row ever
# written).  A prev_hash of "genesis" is always valid — it's the chain root.
broken = False
broken_idx = None
for dial_i, (file_idx, _line_bytes, row) in enumerate(dial_entries):
  stored_ph = row.get("prev_hash", "")
  # "genesis" sentinel: chain anchor, no predecessor to verify
  if stored_ph == "genesis":
    continue
  if stored_ph not in all_hashes:
    print(
      f"BROKEN: dial row {dial_i} prev_hash {stored_ph[:16]}... does not match any line "
      f"(kind={row.get('kind','?')} ts={row.get('timestamp','?')})"
    )
    broken = True
    broken_idx = dial_i
    break

if not broken:
  print(f"OK ({len(dial_entries)} dial rows, chain intact)")
  sys.exit(0)
else:
  sys.exit(1)
_PYEOF
  exit $?

elif [[ $# -eq 2 ]]; then
  # ── Mode 2: Point-in-time lookup ─────────────────────────────────────────
  CLASS="$1"
  TIMESTAMP="$2"

  python3 - "$_AUDIT_PATH" "$CLASS" "$TIMESTAMP" <<'_PYEOF'
import sys, json, hashlib, pathlib
from datetime import datetime, timezone, timedelta

audit_path = pathlib.Path(sys.argv[1])
class_filter = sys.argv[2]
ts_str = sys.argv[3]

if not audit_path.exists():
  print(f"ERROR: audit.jsonl not found at {audit_path}", file=sys.stderr)
  sys.exit(1)

# Parse the query timestamp
try:
  query_ts = datetime.fromisoformat(ts_str)
  if query_ts.tzinfo is None:
    query_ts = query_ts.replace(tzinfo=timezone.utc)
except ValueError as e:
  print(f"ERROR: invalid timestamp {ts_str!r}: {e}", file=sys.stderr)
  sys.exit(1)

window_start = query_ts - timedelta(hours=1)
window_end   = query_ts + timedelta(hours=1)

content = audit_path.read_bytes()
lines = [l for l in content.split(b"\n") if l.strip()]

rows = []
for line in lines:
  try:
    rows.append(json.loads(line))
  except json.JSONDecodeError:
    continue

# (a) Dial state for CLASS at the given timestamp
#     Walk all dial_change rows for this class in chronological order;
#     find the last one at or before query_ts.
dial_state_at_ts = None
dial_rows_for_class = [
  r for r in rows
  if r.get("kind") == "dial_change" and r.get("class") == class_filter
]

for r in dial_rows_for_class:
  try:
    row_ts = datetime.fromisoformat(r["timestamp"])
    if row_ts.tzinfo is None:
      row_ts = row_ts.replace(tzinfo=timezone.utc)
  except (KeyError, ValueError):
    continue
  if row_ts <= query_ts:
    dial_state_at_ts = r

verb_labels = {1: "ask", 2: "propose-confirm", 3: "propose-timeout", 4: "announce", 5: "act"}

print(f"=== Dial state for '{class_filter}' at {ts_str} ===")
if dial_state_at_ts is not None:
  lvl = dial_state_at_ts.get("new_level", "?")
  verb = verb_labels.get(lvl, str(lvl)) if isinstance(lvl, int) else str(lvl)
  print(f"  level={lvl} ({verb})")
  print(f"  set at: {dial_state_at_ts.get('timestamp', '?')}")
  src = dial_state_at_ts.get("source")
  if src:
    print(f"  source: {json.dumps(src)}")
else:
  print(f"  (no dial_change rows for '{class_filter}' before {ts_str})")

# (b) All dial changes ±1h
print(f"\n=== Dial changes for '{class_filter}' ±1h of {ts_str} ===")
window_rows = []
for r in dial_rows_for_class:
  try:
    row_ts = datetime.fromisoformat(r["timestamp"])
    if row_ts.tzinfo is None:
      row_ts = row_ts.replace(tzinfo=timezone.utc)
  except (KeyError, ValueError):
    continue
  if window_start <= row_ts <= window_end:
    window_rows.append(r)

if window_rows:
  for r in window_rows:
    lvl = r.get("new_level", "?")
    prev = r.get("prev_level", "?")
    ts = r.get("timestamp", "?")
    src = r.get("source", {})
    src_str = src.get("login") if isinstance(src, dict) else str(src)
    print(f"  {ts}  {class_filter}: {prev} → {lvl}  (source={src_str})")
else:
  print(f"  (none)")

# (c) Spawn/agent actions under that dial level
#     Find all agent_run_start rows in the ±1h window
print(f"\n=== Agent actions ±1h of {ts_str} ===")
action_rows = []
for r in rows:
  kind = r.get("kind", "")
  if kind not in ("agent_run_start", "agent_run_complete", "dial_change", "spawn_notify"):
    continue
  try:
    row_ts = datetime.fromisoformat(r.get("timestamp", ""))
    if row_ts.tzinfo is None:
      row_ts = row_ts.replace(tzinfo=timezone.utc)
  except (ValueError, TypeError):
    continue
  if window_start <= row_ts <= window_end:
    action_rows.append(r)

if action_rows:
  for r in action_rows:
    ts = r.get("timestamp", "?")
    kind = r.get("kind", "?")
    role = r.get("role") or r.get("agent_role") or ""
    disc = r.get("discussion") or ""
    print(f"  {ts}  {kind}  role={role}  discussion={disc}")
else:
  print(f"  (none)")

sys.exit(0)
_PYEOF
  exit $?

else
  echo "Usage: $0 [CLASS TIMESTAMP]" >&2
  echo "  Without args: verify full hash chain" >&2
  echo "  With args: point-in-time lookup for CLASS at TIMESTAMP (ISO-8601)" >&2
  exit 1
fi
