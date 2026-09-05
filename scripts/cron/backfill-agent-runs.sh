#!/usr/bin/env bash
# scripts/cron/backfill-agent-runs.sh — reconcile orphan agent_run rows from
# Claude Code session transcripts.
#
# Problem: agents that crash or are killed before SubagentStop fires leave no
# agent_run end_ts/verdict.  This script scans Claude Code session directories,
# resolves symlinks (Claude rotates sessions via symlinks), reads each transcript,
# and calls complete_run() for any session whose agent_id appears in agent_run
# with a NULL end_ts.
#
# Usage (run nightly via cron or manually):
#   bash scripts/cron/backfill-agent-runs.sh
#   bash scripts/cron/backfill-agent-runs.sh --sessions-dir /path/to/sessions
#   bash scripts/cron/backfill-agent-runs.sh --dry-run
#
# Also wraps the existing backfill-agent-runs.sh (audit.jsonl path) for a full
# reconciliation pass in one invocation.
#
# Exit codes:
#   0 — completed (rows updated count printed to stdout)
#   1 — unexpected error

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Default sessions root: ~/.claude/projects/*/sessions/
# Claude Code stores sessions under a per-project directory named after the
# canonicalized project path, e.g.:
#   ~/.claude/projects/-home-agent-autonomous-forever/sessions/
# Session dirs contain transcript JSONL files and may themselves be symlinks
# (Claude rotates the active session symlink on each new session).
SESSIONS_GLOB="${HOME}/.claude/projects/*/sessions"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sessions-dir)
      SESSIONS_GLOB="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--sessions-dir <glob>] [--dry-run]" >&2
      exit 1
      ;;
  esac
done

echo "[backfill-cron] Starting orphan reconciliation (dry_run=$DRY_RUN)"

# ── 1. Full audit-trail backfill (existing logic) ─────────────────────────────
echo "[backfill-cron] Running audit.jsonl backfill..."
python3 -m backend.agent_run_tracker backfill 2>/dev/null \
  || echo "[backfill-cron] WARN: audit backfill failed (non-fatal)" >&2

# ── 2. Transcript scan for orphan rows ────────────────────────────────────────
# Resolve symlinks before reading so we see the real transcript files, not stale
# symlink targets that Claude Code may have rotated away.
TRANSCRIPT_BACKFILL_PY="$(cat <<'PYEOF'
import json
import os
import sys
from pathlib import Path

sessions_glob = sys.argv[1]
dry_run = sys.argv[2] == "1"
repo_root = sys.argv[3]

# Add repo to path so we can import agent_run_tracker, and scripts/lib so we
# can import the shared spawn-tag extractor.  This body runs via `python3 -c`
# and therefore has no __file__ — both paths derive from argv, not __file__.
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "scripts", "lib"))

try:
    import duckdb
    from backend.agent_run_tracker import _db_path, _ensure_schema, complete_run
    from transcript_event_id import extract_event_id
except ImportError as e:
    print(f"[backfill-cron] SKIP: cannot import dependencies: {e}", file=sys.stderr)
    sys.exit(0)

import glob

# Collect all transcript files under the sessions glob.
# Resolve symlinks at both the session-dir level and individual file level so
# we read actual content rather than potentially-stale dangling symlinks.
transcript_paths: list[Path] = []
for sessions_dir in glob.glob(sessions_glob):
    sessions_path = Path(sessions_dir)
    if not sessions_path.is_dir():
        # May be a symlink — resolve it
        try:
            sessions_path = sessions_path.resolve()
        except Exception:
            continue
    if not sessions_path.is_dir():
        continue
    for entry in sessions_path.iterdir():
        # Resolve each entry to its real path (handles symlinked session dirs)
        try:
            real = entry.resolve()
        except Exception:
            continue
        if real.is_file() and real.suffix in (".jsonl", ".json"):
            transcript_paths.append(real)
        elif real.is_dir():
            # Some Claude Code versions nest transcripts one level deeper
            for sub in real.iterdir():
                try:
                    sub_real = sub.resolve()
                except Exception:
                    continue
                if sub_real.is_file() and sub_real.suffix in (".jsonl", ".json"):
                    transcript_paths.append(sub_real)

if not transcript_paths:
    print("[backfill-cron] No transcripts found under sessions glob", file=sys.stderr)
    sys.exit(0)

print(f"[backfill-cron] Found {len(transcript_paths)} transcript(s)", file=sys.stderr)

# For each transcript, extract hook_event_id and token usage from the final
# assistant message.  Then check if the agent_run row needs end_ts populated.
db = _db_path()
if not db.exists():
    print("[backfill-cron] stats.duckdb not found — skipping transcript scan", file=sys.stderr)
    sys.exit(0)

conn = duckdb.connect(str(db))
try:
    _ensure_schema(conn)
    updated = 0
    for tpath in transcript_paths:
        try:
            # Shared, shape-validated extractor (scripts/lib/transcript_event_id.py).
            # It walks tool_result payloads as well as text blocks, which is how
            # the tag actually reaches an agent, and returns the first CANONICAL
            # match rather than the last match of any shape.
            hook_event_id = extract_event_id(str(tpath))
            if not hook_event_id:
                continue  # can't reconcile without an agent_id

            # Second pass for token usage from the final assistant message.
            # Deliberately after the id check: a transcript with no usable id is
            # never read twice.
            last_usage = {}
            with open(tpath, "r", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    role = obj.get("role", "")
                    usage = {}
                    if isinstance(obj.get("message"), dict):
                        msg = obj["message"]
                        if not role:
                            role = msg.get("role", "")
                        usage = msg.get("usage", {})

                    if role == "assistant" and isinstance(usage, dict) and usage:
                        last_usage = usage

            # Check if this agent_id has a NULL end_ts in agent_run
            row = conn.execute(
                "SELECT end_ts FROM agent_run WHERE agent_id = ?",
                [hook_event_id],
            ).fetchone()

            if row is None:
                # Row doesn't exist at all — skip (backfill from audit handles new rows)
                continue
            if row[0] is not None:
                continue  # already complete

            # Row exists but end_ts is NULL — fill it in from usage
            input_tok = last_usage.get("input_tokens")
            output_tok = last_usage.get("output_tokens")
            cache_read = last_usage.get("cache_read_input_tokens")
            cache_creation = last_usage.get("cache_creation_input_tokens")

            if dry_run:
                print(f"[backfill-cron] DRY RUN: would update {hook_event_id} "
                      f"input={input_tok} output={output_tok}", file=sys.stderr)
            else:
                complete_run(
                    agent_id=hook_event_id,
                    verdict="unknown",   # we don't know verdict from transcript scan
                    input_tok=int(input_tok) if input_tok is not None else None,
                    output_tok=int(output_tok) if output_tok is not None else None,
                    cache_read=int(cache_read) if cache_read is not None else None,
                    cache_creation_tokens=int(cache_creation) if cache_creation is not None else None,
                )
                print(f"[backfill-cron] Updated orphan row: {hook_event_id}", file=sys.stderr)
                updated += 1
        except Exception as exc:
            print(f"[backfill] skip {tpath}: {exc}", file=sys.stderr)
            continue

    print(f"[backfill-cron] Orphan rows updated: {updated}")
finally:
    conn.close()
PYEOF
)"

python3 -c "$TRANSCRIPT_BACKFILL_PY" "$SESSIONS_GLOB" "$DRY_RUN" "$REPO_ROOT" 2>&1 \
  || echo "[backfill-cron] WARN: transcript scan failed (non-fatal)" >&2

echo "[backfill-cron] Done."
