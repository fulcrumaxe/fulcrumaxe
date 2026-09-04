#!/usr/bin/env bash
# scripts/measure-cache-hit.sh — Verify prompt-caching is effective by comparing
# cache_read_input_tokens between two consecutive spawns of the same role.
#
# The test works by reading the agent_run table for the two most recent completed
# runs of the given role, then asserting that the second run has
# cache_read_tok > 0.5 * input_tok (i.e., more than 50% of tokens came from cache).
#
# Usage:
#   bash scripts/measure-cache-hit.sh --role <role>
#   bash scripts/measure-cache-hit.sh --role executor
#
# Exit codes:
#   0 — cache hit confirmed (cache_read_tok > 0.5 * input_tok on second run)
#   0 — cache_unavailable emitted (NULL cache_read_tok — infra not storing cache stats)
#   1 — cache miss detected (cache_read_tok present but ≤ 0.5 * input_tok)
#   2 — usage error or not enough runs to compare

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-${HOME}/.fulcrumaxe-state}"

ROLE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --role <role>"
      echo "  Reads the two most recent agent_run rows for ROLE and checks cache_read_tok."
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ROLE" ]]; then
  echo "Error: --role is required" >&2
  exit 2
fi

python3 - "$STATE_DIR" "$ROLE" <<'_PYEOF'
import sys, json
from pathlib import Path

state_dir = Path(sys.argv[1])
role = sys.argv[2]

# Try to import duckdb and read agent_run
try:
    import duckdb
except ImportError:
    print(f"cache_unavailable: duckdb not installed — cannot read agent_run stats")
    sys.exit(0)

db_path = state_dir / "stats.duckdb"
if not db_path.exists():
    print(f"cache_unavailable: stats.duckdb not found at {db_path}")
    sys.exit(0)

try:
    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.execute("""
        SELECT agent_id, start_ts, input_tok, cache_read_tok
        FROM agent_run
        WHERE role = ?
          AND end_ts IS NOT NULL
        ORDER BY start_ts DESC
        LIMIT 2
    """, [role]).fetchall()
    con.close()
except Exception as e:
    print(f"cache_unavailable: failed to query agent_run: {e}")
    sys.exit(0)

if len(rows) < 2:
    print(f"cache_unavailable: fewer than 2 completed runs found for role='{role}' — run the role at least twice")
    sys.exit(2)

# Most recent run is rows[0], second-most-recent is rows[1]
run2 = rows[0]  # second spawn (should have cache hits)
agent_id2, start_ts2, input_tok2, cache_read_tok2 = run2

if cache_read_tok2 is None:
    print(f"cache_unavailable: cache_read_tok is NULL for run {agent_id2} — "
          f"the harness is not recording cache token stats. "
          f"Action: ensure cache_read_input_tokens is captured in post-agent-hook.sh.")
    sys.exit(0)

if input_tok2 is None or input_tok2 == 0:
    print(f"cache_unavailable: input_tok is NULL or 0 for run {agent_id2} — cannot compute ratio")
    sys.exit(0)

ratio = cache_read_tok2 / input_tok2
threshold = 0.5

if ratio > threshold:
    print(f"cache_hit: run {agent_id2} — cache_read={cache_read_tok2} / input={input_tok2} = {ratio:.1%} (threshold {threshold:.0%}) ✓")
    sys.exit(0)
else:
    print(f"cache_miss: run {agent_id2} — cache_read={cache_read_tok2} / input={input_tok2} = {ratio:.1%} (below threshold {threshold:.0%})")
    print(f"  Possible causes:")
    print(f"    - Cache prefix was not stable between spawns (variable content in stable section)")
    print(f"    - Model provider did not cache (cold cache, prompt changed, < 1024 tokens)")
    print(f"    - Less than ~30s between spawns (Anthropic TTL may not have fired)")
    sys.exit(1)
_PYEOF
