#!/usr/bin/env bash
# spawn-hourly-stats.sh — emit hourly metrics to stats.duckdb
#
# Metrics emitted:
#   wasted_tokens_ratio    — tokens on fail/needs-fix agents ÷ total tokens (past 24h)
#   impersonation_rate     — reviewer_skipped findings ÷ impl-coord runs (past 24h)
#   hard_rule_violation_count — forbidden_subagent_type + git_rm_usage + team_lead_self_edit
#
# Wire into /loop step 7.5:
#   bash scripts/spawn-hourly-stats.sh 2>/dev/null || true
#
# Safe to re-run multiple times — DuckDB INSERT OR IGNORE deduplicates by (ts, metric, tags).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FEED="$REPO_ROOT/.autonomous-team/agent-feed.jsonl"
RETROS="$REPO_ROOT/.autonomous-team/agent-retros.jsonl"
RUN_REPORTS_DIR="$REPO_ROOT/.autonomous-team/run-reports"

# ── Helper: emit one metric row ───────────────────────────────────────────────
emit_metric() {
  local metric="$1"
  local value="$2"
  local unit="$3"
  local tags_json="$4"
  local source="spawn-hourly-stats"

  STATS_DB_PATH="" \
  python3 -c "
import sys, os
sys.path.insert(0, '${REPO_ROOT}/backend')
import stats_writer
import json

stats_writer.record(
    metric=${metric@Q},
    value=float(${value@Q}),
    unit=${unit@Q},
    tags=json.loads(${tags_json@Q}),
    source=${source@Q},
)
print('[hourly-stats] recorded ${metric}=${value} ${unit}')
"
}

# ── 1. wasted_tokens_ratio ────────────────────────────────────────────────────
WASTED_RATIO=0.0
if [[ -f "$FEED" ]]; then
  WASTED_RATIO=$(FEED_PATH="$FEED" python3 -c "
import json, os, datetime

feed_path = os.environ['FEED_PATH']
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()

fail_tokens = 0
total_tokens = 0

with open(feed_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = e.get('ts', '')
        if ts < cutoff:
            continue
        tok = e.get('tokens') or {}
        inp = tok.get('input', 0) or 0
        out = tok.get('output', 0) or 0
        t = inp + out
        total_tokens += t
        if e.get('verdict') in ('fail', 'needs-fix', 'needs_fix'):
            fail_tokens += t

if total_tokens == 0:
    print(0.0)
else:
    print(round(fail_tokens / total_tokens, 4))
" 2>/dev/null || echo "0.0")
fi

STATS_DB_PATH="" python3 -c "
import sys, os
sys.path.insert(0, '${REPO_ROOT}/backend')
import stats_writer
stats_writer.record(
    metric='wasted_tokens_ratio',
    value=float('${WASTED_RATIO}'),
    unit='ratio',
    tags={'window': '24h'},
    source='spawn-hourly-stats',
)
print('[hourly-stats] recorded wasted_tokens_ratio=${WASTED_RATIO} ratio')
"

# ── 2. impersonation_rate ─────────────────────────────────────────────────────
IMPERSONATION_RATE=0.0
if [[ -f "$FEED" ]]; then
  IMPERSONATION_RATE=$(RETROS_PATH="$RETROS" FEED_PATH="$FEED" python3 -c "
import json, os, datetime

retros_path = os.environ.get('RETROS_PATH', '')
feed_path   = os.environ.get('FEED_PATH', '')
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()

impl_runs = 0
with open(feed_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = e.get('ts', '')
        if ts < cutoff:
            continue
        if e.get('role') == 'executor' and e.get('event_type') in ('agent_end', 'merge'):
            impl_runs += 1

skipped = 0
try:
    with open(retros_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get('ts', e.get('created_at', ''))
            if ts < cutoff:
                continue
            cat = e.get('classifier') or ''
            if 'reviewer_skipped' in cat or 'impersonat' in cat:
                skipped += 1
except FileNotFoundError:
    pass

if impl_runs == 0:
    print(0.0)
else:
    print(round(skipped / impl_runs, 4))
" 2>/dev/null || echo "0.0")
fi

STATS_DB_PATH="" python3 -c "
import sys, os
sys.path.insert(0, '${REPO_ROOT}/backend')
import stats_writer
stats_writer.record(
    metric='impersonation_rate',
    value=float('${IMPERSONATION_RATE}'),
    unit='ratio',
    tags={'window': '24h'},
    source='spawn-hourly-stats',
)
print('[hourly-stats] recorded impersonation_rate=${IMPERSONATION_RATE} ratio')
"

# ── 3. hard_rule_violation_count ──────────────────────────────────────────────
VIOLATION_COUNT=0
VIOLATION_COUNT=$(REPORTS_DIR="$RUN_REPORTS_DIR" python3 -c "
import json, os, glob, datetime

reports_dir = os.environ.get('REPORTS_DIR', '')
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()

VIOLATION_CATEGORIES = {'forbidden_subagent_type', 'git_rm_usage', 'team_lead_self_edit'}

count = 0
for path in glob.glob(os.path.join(reports_dir, '*.json')):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        continue
    report_at = data.get('report_at', '')
    if report_at < cutoff:
        continue
    for finding in data.get('findings', []):
        cat = finding.get('category', '')
        if cat in VIOLATION_CATEGORIES:
            count += 1

print(count)
" 2>/dev/null || echo "0")

STATS_DB_PATH="" python3 -c "
import sys, os
sys.path.insert(0, '${REPO_ROOT}/backend')
import stats_writer
stats_writer.record(
    metric='hard_rule_violation_count',
    value=float('${VIOLATION_COUNT}'),
    unit='count',
    tags={'window': '24h'},
    source='spawn-hourly-stats',
)
print('[hourly-stats] recorded hard_rule_violation_count=${VIOLATION_COUNT} count')
"

echo "[hourly-stats] done"
