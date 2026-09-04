#!/usr/bin/env bash
# scripts/agent-feed-tail.sh — CLI consumer for agent-feed.jsonl
#
# Usage:
#   scripts/agent-feed-tail.sh [options]
#
# Options:
#   -n N               Tail last N events (default: 50)
#   --follow, -f       Stream new events as they are appended (tail -F | jq)
#   --filter KEY=VALUE Filter events where event[KEY] == VALUE
#                      Supported KEY types: string comparison for role/event_type/verdict/model;
#                      integer comparison for discussion/pr.
#                      Example: --filter role=executor
#                               --filter discussion=335
#   --since DURATION   Only events newer than DURATION ago.
#                      Accepts: Ns (seconds), Nm (minutes), Nh (hours), Nd (days)
#                      Example: --since 30m, --since 2h, --since 1d
#   --json             Raw JSONL passthrough (no formatting)
#   --help, -h         Show this help

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FEED_PATH="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"

# ── Defaults ──────────────────────────────────────────────────────────────────
N=50
FOLLOW=false
FILTER_KEY=""
FILTER_VAL=""
SINCE_SECS=""
JSON_MODE=false

# ── ANSI colors ───────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RESET="\033[0m"
  C_DIM="\033[2m"
  C_BOLD="\033[1m"
  C_CYAN="\033[36m"
  C_GREEN="\033[32m"
  C_YELLOW="\033[33m"
  C_RED="\033[31m"
  C_MAGENTA="\033[35m"
else
  C_RESET="" C_DIM="" C_BOLD="" C_CYAN="" C_GREEN="" C_YELLOW="" C_RED="" C_MAGENTA=""
fi

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n)
      N="$2"; shift 2 ;;
    --follow|-f)
      FOLLOW=true; shift ;;
    --filter)
      FILTER_KEY="${2%%=*}"
      FILTER_VAL="${2#*=}"
      shift 2 ;;
    --since)
      DURATION="$2"
      # Parse duration string: 30m, 2h, 1d, 60s
      if [[ "$DURATION" =~ ^([0-9]+)([smhd])$ ]]; then
        AMOUNT="${BASH_REMATCH[1]}"
        UNIT="${BASH_REMATCH[2]}"
        case "$UNIT" in
          s) SINCE_SECS=$AMOUNT ;;
          m) SINCE_SECS=$(( AMOUNT * 60 )) ;;
          h) SINCE_SECS=$(( AMOUNT * 3600 )) ;;
          d) SINCE_SECS=$(( AMOUNT * 86400 )) ;;
        esac
      else
        echo "[agent-feed-tail] Invalid --since format: $DURATION (use Ns, Nm, Nh, Nd)" >&2
        exit 1
      fi
      shift 2 ;;
    --json)
      JSON_MODE=true; shift ;;
    --help|-h)
      head -30 "$0" | grep "^#" | sed 's/^# \?//'
      exit 0 ;;
    *)
      echo "[agent-feed-tail] Unknown argument: $1" >&2
      exit 1 ;;
  esac
done

# ── Check feed exists ─────────────────────────────────────────────────────────
if [[ ! -f "$FEED_PATH" ]]; then
  echo "[agent-feed-tail] Feed not found: $FEED_PATH" >&2
  exit 0
fi

# ── Compute since cutoff (epoch seconds) ─────────────────────────────────────
SINCE_EPOCH=""
if [[ -n "$SINCE_SECS" ]]; then
  NOW_EPOCH=$(date +%s)
  SINCE_EPOCH=$(( NOW_EPOCH - SINCE_SECS ))
fi

# ── Format a single JSON line for human display ───────────────────────────────
format_event() {
  local line="$1"
  python3 - "$line" <<'PYEOF'
import sys, json
from datetime import datetime, timezone

line = sys.argv[1].strip()
if not line:
    sys.exit(0)
try:
    e = json.loads(line)
except Exception:
    print(f"[malformed] {line[:100]}")
    sys.exit(0)

ts = e.get("ts", "")
try:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    ts_fmt = dt.strftime("%H:%M:%S")
except Exception:
    ts_fmt = ts[:8] if ts else "??"

role       = e.get("role", "?")
event_type = e.get("event_type", "log")
message    = e.get("message", "")
disc       = e.get("discussion")
pr         = e.get("pr")
verdict    = e.get("verdict", "")

extras = []
if disc:   extras.append(f"D#{disc}")
if pr:     extras.append(f"PR#{pr}")
if verdict: extras.append(verdict)

suffix = " ".join(extras)
print(f"{ts_fmt} {role:20s} {event_type:15s} {suffix:20s} {message}")
PYEOF
}

# ── Apply filters via Python (returns 0/1 for match) ─────────────────────────
matches_filter() {
  local line="$1"
  if [[ -z "$FILTER_KEY" ]]; then
    return 0
  fi
  python3 - "$line" "$FILTER_KEY" "$FILTER_VAL" <<'PYEOF'
import sys, json

line = sys.argv[1].strip()
key  = sys.argv[2]
val  = sys.argv[3]

if not line:
    sys.exit(1)
try:
    e = json.loads(line)
except Exception:
    sys.exit(1)

ev_val = e.get(key)
if ev_val is None:
    sys.exit(1)

# Integer fields
if key in ("discussion", "pr"):
    try:
        sys.exit(0 if int(ev_val) == int(val) else 1)
    except (TypeError, ValueError):
        sys.exit(1)

# String fields
sys.exit(0 if str(ev_val) == val else 1)
PYEOF
}

# ── Since filter ──────────────────────────────────────────────────────────────
matches_since() {
  local line="$1"
  if [[ -z "$SINCE_EPOCH" ]]; then
    return 0
  fi
  python3 - "$line" "$SINCE_EPOCH" <<'PYEOF'
import sys, json
from datetime import datetime, timezone

line       = sys.argv[1].strip()
cutoff_ep  = float(sys.argv[2])

if not line:
    sys.exit(1)
try:
    e = json.loads(line)
except Exception:
    sys.exit(1)

ts = e.get("ts", "")
try:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    sys.exit(0 if dt.timestamp() >= cutoff_ep else 1)
except Exception:
    sys.exit(0)  # Malformed ts — include the event
PYEOF
}

# ── Follow mode ───────────────────────────────────────────────────────────────
if [[ "$FOLLOW" == "true" ]]; then
  echo "[agent-feed-tail] Following $FEED_PATH (Ctrl-C to stop)" >&2
  tail -F "$FEED_PATH" | while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if ! matches_since "$line"; then continue; fi
    if ! matches_filter "$line"; then continue; fi
    if [[ "$JSON_MODE" == "true" ]]; then
      echo "$line"
    else
      format_event "$line"
    fi
  done
  exit 0
fi

# ── Static tail mode ─────────────────────────────────────────────────────────
# Read the file, apply filters, take last N matching lines
python3 - "$FEED_PATH" "$N" "$FILTER_KEY" "$FILTER_VAL" "$SINCE_EPOCH" "$JSON_MODE" <<'PYEOF'
import sys, json
from datetime import datetime, timezone

feed_path  = sys.argv[1]
n          = int(sys.argv[2])
filter_key = sys.argv[3]
filter_val = sys.argv[4]
since_ep   = float(sys.argv[5]) if sys.argv[5] else None
json_mode  = sys.argv[6] == "true"

try:
    with open(feed_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
except OSError as e:
    print(f"[agent-feed-tail] Cannot read feed: {e}", file=sys.stderr)
    sys.exit(0)

matching = []
for raw in lines:
    raw = raw.strip()
    if not raw:
        continue
    try:
        e = json.loads(raw)
    except json.JSONDecodeError:
        continue

    # Since filter
    if since_ep is not None:
        ts = e.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.timestamp() < since_ep:
                continue
        except Exception:
            pass  # Malformed ts — include

    # Key=value filter
    if filter_key:
        ev_val = e.get(filter_key)
        if ev_val is None:
            continue
        if filter_key in ("discussion", "pr"):
            try:
                if int(ev_val) != int(filter_val):
                    continue
            except (TypeError, ValueError):
                continue
        else:
            if str(ev_val) != filter_val:
                continue

    matching.append((raw, e))

# Take last N
matching = matching[-n:]

for raw, e in matching:
    if json_mode:
        print(raw)
        continue
    # Human-readable format
    ts = e.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        ts_fmt = dt.strftime("%H:%M:%S")
    except Exception:
        ts_fmt = ts[:8] if ts else "??"

    role       = e.get("role", "?")
    event_type = e.get("event_type", "log")
    message    = e.get("message", "")
    disc       = e.get("discussion")
    pr         = e.get("pr")
    verdict    = e.get("verdict", "")

    extras = []
    if disc:    extras.append(f"D#{disc}")
    if pr:      extras.append(f"PR#{pr}")
    if verdict: extras.append(verdict)

    suffix = " ".join(extras)
    print(f"{ts_fmt} {role:20s} {event_type:15s} {suffix:20s} {message}")
PYEOF
