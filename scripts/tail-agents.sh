#!/usr/bin/env bash
# scripts/tail-agents.sh — Stream all active agent transcripts to stdout.
#
# Discovers the 20 most-recently-modified subagent JSONL transcripts under
#   ~/.claude/projects/-home-agent-fulcrumaxe/*/subagents/agent-*.jsonl
# and multiplexes their content to stdout with a [agent-XXXX] prefix.
#
# Each line is secret-scrubbed by transcript_tailer.py before emission.
#
# Usage:
#   bash scripts/tail-agents.sh [--max-spawns N] [--no-scrub] [--help]
#
# Options:
#   --max-spawns N   Cap active spawn discovery at N (default: 20)
#   --no-scrub       Disable secret scrubbing (NOT recommended in production)
#   --help / -h      Show this help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"

MAX_SPAWNS=20
NO_SCRUB=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-spawns) MAX_SPAWNS="$2"; shift 2 ;;
    --no-scrub)   NO_SCRUB=1; shift ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,2\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# All multiplexing logic lives in Python so the hot-path is testable.
exec python3 - "$MAX_SPAWNS" "$NO_SCRUB" "$BACKEND" <<'PYEOF'
"""Inline Python multiplexer for tail-agents.sh.

Reads MAX_SPAWNS / NO_SCRUB / BACKEND from sys.argv[1..3].
Discovers active spawns, tails each one, and streams to stdout with prefix.
"""
import sys
import threading

max_spawns = int(sys.argv[1])
no_scrub   = sys.argv[2] == "1"
backend    = sys.argv[3]

sys.path.insert(0, backend)
from transcript_tailer import discover_active_spawns, tail_transcript, agent_label_from_path

spawns = discover_active_spawns(max_spawns=max_spawns)

if not spawns:
    print("[tail-agents] No active agent transcripts found.", file=sys.stderr)
    print(
        "[tail-agents] Glob: ~/.claude/projects/-home-agent-fulcrumaxe"
        "/*/subagents/agent-*.jsonl",
        file=sys.stderr,
    )
    sys.exit(0)

print(f"[tail-agents] Found {len(spawns)} active spawn(s). Streaming...", file=sys.stderr)

_print_lock = threading.Lock()

def make_emitter(label: str):
    def emit(line: str) -> None:
        with _print_lock:
            print(f"[{label}] {line}", flush=True)
    return emit

threads = []
for path in spawns:
    label = agent_label_from_path(path)
    t = threading.Thread(
        target=tail_transcript,
        args=(path, make_emitter(label)),
        kwargs={"scrub": not no_scrub},
        daemon=True,
        name=f"tailer-{label}",
    )
    threads.append(t)
    t.start()

for t in threads:
    t.join()
PYEOF
