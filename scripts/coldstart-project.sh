#!/usr/bin/env bash
# scripts/coldstart-project.sh — bootstrap an autonomous-team state dir for any repo.
#
# Usage:
#   bash scripts/coldstart-project.sh <repo-path> <project-name> [--language rust|python|typescript|polyglot] [--mode existing|new]
#   bash scripts/coldstart-project.sh --self-test
#
# Idempotent — safe to re-run. Existing project.json is merged (not overwritten).
#
# What it does:
#   1. Resolves <repo-path> to absolute, verifies it's a git repo.
#      With --mode new, an EMPTY (not just non-git) <repo-path> is scaffolded
#      instead of hard-failing: git init + README.md + initial commit. A
#      non-git, non-empty directory always errors -- never clobbered, mode
#      or no mode (Spec item 16).
#   2. Creates ~/.<project-name>-state/ with placeholder files.
#      The `~` here is $COLDSTART_STATE_ROOT, defaulting to $HOME. Set
#      COLDSTART_STATE_ROOT to an absolute path to write the state dir
#      somewhere else -- this is how a test coldstarts without permanently
#      enlarging the operator's fleet (D#2317). Operator behaviour with the
#      variable unset is unchanged.
#   3. Creates <repo-path>/.autonomous-team/ and symlinks for state files.
#   4. Generates or merges <repo-path>/.autonomous-team/project.json,
#      including the project_mode field (Spec item 17).
#   5. Prints a summary with next-step hints.
#
# --self-test: non-interactive end-to-end check (CI) of the --mode new
# empty-dir scaffold path and the populated-non-git-dir refusal. Exits 0
# on success. See self_test() below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/lib/coldstart-state-root.sh
source "$SCRIPT_DIR/lib/coldstart-state-root.sh"

# ---------------------------------------------------------------------------
# --self-test — dispatched before normal arg parsing since it takes no
# positional repo-path/project-name args.
# ---------------------------------------------------------------------------
self_test() {
    local fail=0

    # The self-test coldstarts two throwaway projects. Redirect their state
    # dirs into a scratch root so a killed self-test cannot leave anything
    # under the operator's $HOME -- the rm -rf lines below are hygiene, and
    # hygiene does not survive a SIGKILL (D#2317 PR-c).
    local selftest_state_root
    selftest_state_root="$(mktemp -d)"
    export COLDSTART_STATE_ROOT="$selftest_state_root"

    # 1. --mode new pointed at an EMPTY dir must scaffold, not hard-fail.
    local empty_dir
    empty_dir="$(mktemp -d)"
    if bash "${BASH_SOURCE[0]}" "$empty_dir" "selftestnew$$" --mode new > /tmp/coldstart-project-selftest-empty-$$.log 2>&1; then
        if [[ ! -d "$empty_dir/.git" ]]; then
            echo "[self-test] FAIL: --mode new on empty dir did not git init" >&2
            fail=1
        elif [[ ! -f "$empty_dir/README.md" ]]; then
            echo "[self-test] FAIL: --mode new on empty dir did not write README.md" >&2
            fail=1
        elif ! git -C "$empty_dir" log --oneline -1 > /dev/null 2>&1; then
            echo "[self-test] FAIL: --mode new on empty dir did not create an initial commit" >&2
            fail=1
        else
            echo "[self-test] PASS: --mode new scaffolded empty dir ($empty_dir)"
        fi
    else
        echo "[self-test] FAIL: --mode new on empty dir exited non-zero:" >&2
        cat /tmp/coldstart-project-selftest-empty-$$.log >&2
        fail=1
    fi
    rm -f "/tmp/coldstart-project-selftest-empty-$$.log"
    rm -rf "$empty_dir" "$selftest_state_root/.selftestnew$$-state"

    # 2. --mode new pointed at a POPULATED non-git dir must error, and must
    #    NOT run git init (no clobbering an existing, unrelated directory).
    local populated_dir
    populated_dir="$(mktemp -d)"
    echo "pre-existing file" > "$populated_dir/existing-file.txt"
    if bash "${BASH_SOURCE[0]}" "$populated_dir" "selftestpop$$" --mode new > /tmp/coldstart-project-selftest-pop-$$.log 2>&1; then
        echo "[self-test] FAIL: --mode new on populated non-git dir should have errored but exited 0" >&2
        fail=1
    else
        if [[ -d "$populated_dir/.git" ]]; then
            echo "[self-test] FAIL: --mode new on populated non-git dir ran git init (clobber!)" >&2
            fail=1
        else
            echo "[self-test] PASS: --mode new refused populated non-git dir, no git init"
        fi
    fi
    rm -f "/tmp/coldstart-project-selftest-pop-$$.log"
    rm -rf "$populated_dir" "$selftest_state_root/.selftestpop$$-state"
    rm -rf "$selftest_state_root"

    if [[ "$fail" -eq 0 ]]; then
        echo "[self-test] PASS"
        return 0
    else
        echo "[self-test] one or more checks FAILED" >&2
        return 1
    fi
}

if [[ "${1:-}" == "--self-test" ]]; then
    self_test
    exit $?
fi

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
if [[ $# -lt 2 ]]; then
    echo "Usage: bash scripts/coldstart-project.sh <repo-path> <project-name> [--language rust|python|typescript|polyglot] [--mode existing|new] [--pr-categories <comma-separated>]" >&2
    echo "       bash scripts/coldstart-project.sh --self-test" >&2
    exit 1
fi

REPO_PATH="$1"
PROJECT_NAME="$2"
LANGUAGE="polyglot"  # default
PR_CATEGORIES=""     # default empty
MODE="existing"      # default — back-compat, all prior coldstarts were existing repos

shift 2
while [[ $# -gt 0 ]]; do
    case "$1" in
        --language)
            LANGUAGE="${2:-polyglot}"
            shift 2 || { echo "ERROR: --language requires a value" >&2; exit 1; }
            ;;
        --pr-categories)
            PR_CATEGORIES="${2:-}"
            shift 2 || { echo "ERROR: --pr-categories requires a value" >&2; exit 1; }
            ;;
        --mode)
            MODE="${2:-existing}"
            shift 2 || { echo "ERROR: --mode requires a value" >&2; exit 1; }
            ;;
        *)
            echo "Unknown flag: $1" >&2
            exit 1
            ;;
    esac
done

case "$MODE" in
    existing|new) ;;
    *)
        echo "Unsupported mode: $MODE. Choose one of: existing, new" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Validate project_name — reject path traversal attempts
# ---------------------------------------------------------------------------
if [[ ! "$PROJECT_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "ERROR: project_name must match ^[a-zA-Z0-9_-]+$ (got: $PROJECT_NAME)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate language
# ---------------------------------------------------------------------------
case "$LANGUAGE" in
    rust|python|typescript|polyglot) ;;
    *)
        echo "Unsupported language: $LANGUAGE. Choose one of: rust, python, typescript, polyglot" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Resolve repo path
# ---------------------------------------------------------------------------
REPO_ABS="$(cd "$REPO_PATH" 2>/dev/null && pwd)" || {
    echo "Error: repo-path '$REPO_PATH' does not exist or is not accessible." >&2
    exit 1
}

if ! git -C "$REPO_ABS" rev-parse --git-dir >/dev/null 2>&1; then
    if [[ "$MODE" == "new" ]]; then
        # Greenfield scaffold: git init ONLY on a genuinely empty directory.
        # Never clobber a populated non-git directory, regardless of mode.
        if [[ -z "$(ls -A "$REPO_ABS" 2>/dev/null)" ]]; then
            echo "[+] --mode new: '$REPO_ABS' is empty — scaffolding a new git repo"
            git -C "$REPO_ABS" init -q
            printf '# %s\n' "$PROJECT_NAME" > "$REPO_ABS/README.md"
            git -C "$REPO_ABS" add README.md
            git -C "$REPO_ABS" \
                -c user.email="coldstart@localhost" \
                -c user.name="coldstart" \
                commit -q -m "Initial commit"
            echo "[+] Initialized git repo and created initial commit"
        else
            echo "Error: '$REPO_ABS' is not a git repository and is not empty." >&2
            echo "       --mode new only scaffolds an EMPTY directory (never clobbers existing files)." >&2
            echo "       Initialize git yourself (git init) or point --mode new at an empty directory." >&2
            exit 1
        fi
    else
        echo "Error: '$REPO_ABS' is not a git repository." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Detect git remote for repo field
# ---------------------------------------------------------------------------
REMOTE_URL="$(git -C "$REPO_ABS" remote get-url origin 2>/dev/null || true)"
# Normalise SSH and HTTPS urls → owner/name
REPO_SLUG=""
if [[ "$REMOTE_URL" =~ github\.com[:/]([^/]+/[^/.]+)(\.git)?$ ]]; then
    REPO_SLUG="${BASH_REMATCH[1]}"
fi

# D#1905: no owner can be derived when there's no `origin` remote yet (e.g.
# a fresh --mode new scaffold) or `origin` isn't a github.com URL. Never
# invent an owner here — a hard-coded org literal would silently point an
# adopter's project.json at someone else's GitHub org. Leave "repo" unset
# instead; backend/_repo.py already fails loudly the first time something
# actually needs it, with an actionable message telling the operator to set
# AUTONOMOUS_TEAM_REPO or add a "repo" field by hand.
if [[ -z "$REPO_SLUG" ]]; then
    echo "[!] WARN: could not derive an owner/name from 'origin' remote (${REMOTE_URL:-<no remote>})." >&2
    echo "    project.json will be written WITHOUT a \"repo\" field." >&2
    echo "    Add one yourself, or set AUTONOMOUS_TEAM_REPO, before running the loop." >&2
fi

# ---------------------------------------------------------------------------
# State dir
# ---------------------------------------------------------------------------
STATE_DIR="$(coldstart_state_dir "$PROJECT_NAME")"
echo "=== coldstart-project: $PROJECT_NAME ==="
echo ""

# Create state dir
if [[ ! -d "$STATE_DIR" ]]; then
    mkdir -p "$STATE_DIR"
    echo "[+] Created state dir: $STATE_DIR"
else
    echo "[=] State dir exists: $STATE_DIR"
fi

# Create placeholder files (Python init will populate schemas later)
for placeholder in state.db audit.jsonl agent-feed.jsonl circuit-breaker-history.jsonl; do
    target="$STATE_DIR/$placeholder"
    if [[ ! -f "$target" ]]; then
        touch "$target"
        echo "[+] Created placeholder: $target"
    else
        echo "[=] Placeholder exists: $target"
    fi
done

# D#1883 — seed the dial directive allowlist so set_dial() isn't a no-op.
AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR" bash "$SCRIPT_DIR/provision-dial-allowlist.sh" "$REPO_SLUG" || true

# stats.duckdb requires a valid DuckDB file header — touch creates an empty
# file that DuckDB rejects with "not a valid DuckDB database file".
# Initialize it properly, with touch as a safe fallback when duckdb is absent.
DUCKDB_TARGET="$STATE_DIR/stats.duckdb"
if [[ ! -f "$DUCKDB_TARGET" ]]; then
    if python3 -c "import duckdb; duckdb.connect('$DUCKDB_TARGET').close()" 2>/dev/null; then
        echo "[+] Initialized DuckDB: $DUCKDB_TARGET"
    else
        touch "$DUCKDB_TARGET"
        echo "[+] Created placeholder (duckdb python not available): $DUCKDB_TARGET"
    fi
else
    echo "[=] Placeholder exists: $DUCKDB_TARGET"
fi

# Create blackboard subdir
if [[ ! -d "$STATE_DIR/blackboard" ]]; then
    mkdir -p "$STATE_DIR/blackboard"
    echo "[+] Created $STATE_DIR/blackboard/"
fi

# ---------------------------------------------------------------------------
# .autonomous-team dir in repo
# ---------------------------------------------------------------------------
TEAM_DIR="$REPO_ABS/.autonomous-team"
if [[ ! -d "$TEAM_DIR" ]]; then
    mkdir -p "$TEAM_DIR"
    echo "[+] Created $TEAM_DIR/"
else
    echo "[=] .autonomous-team dir exists: $TEAM_DIR"
fi

# loop-metrics.jsonl — tracks /loop iteration history; readers fail if absent.
# Canonical location: <repo>/.autonomous-team/loop-metrics.jsonl (not in state dir).
LOOP_METRICS_TARGET="$TEAM_DIR/loop-metrics.jsonl"
if [[ ! -f "$LOOP_METRICS_TARGET" ]]; then
    touch "$LOOP_METRICS_TARGET"
    echo "[+] Created placeholder: $LOOP_METRICS_TARGET"
else
    echo "[=] Placeholder exists: $LOOP_METRICS_TARGET"
fi

# Symlinks: state.db, stats.duckdb, audit.jsonl
for fname in state.db stats.duckdb audit.jsonl; do
    link="$TEAM_DIR/$fname"
    target="$STATE_DIR/$fname"
    if [[ -L "$link" ]]; then
        echo "[=] Symlink exists: $link -> $(readlink "$link")"
    elif [[ -e "$link" ]]; then
        echo "[!] File already exists (not a symlink): $link — skipping symlink creation"
    else
        ln -s "$target" "$link"
        echo "[+] Symlink: $link -> $target"
    fi
done

# ---------------------------------------------------------------------------
# Language-specific defaults
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Call Python to get language defaults as JSON, then merge into project.json
DEFAULTS_JSON="$(python3 "$REPO_ROOT/backend/project_config.py" defaults "$LANGUAGE" 2>/dev/null || echo '{}')"

# ---------------------------------------------------------------------------
# project.json — generate or merge
# ---------------------------------------------------------------------------
PROJECT_JSON="$TEAM_DIR/project.json"

# Build base document
BASE_JSON="$(python3 - <<PYEOF
import json, sys, os
from pathlib import Path

language = "$LANGUAGE"
state_dir = "$STATE_DIR"
repo_abs = "$REPO_ABS"
project_name = "$PROJECT_NAME"
# D#1905: empty when 'origin' has no derivable github.com owner/name (see
# the bash WARN emitted above). Deliberately NOT defaulted to a hard-coded
# org — "repo" is simply omitted below, and backend/_repo.py fails loudly
# the first time something actually needs it instead of silently pointing
# at an org the operator doesn't own.
repo_slug = "$REPO_SLUG"
pr_categories_raw = "$PR_CATEGORIES"
project_mode = "$MODE"

defaults_raw = '''$DEFAULTS_JSON'''
try:
    defaults = json.loads(defaults_raw) if defaults_raw.strip() else {}
except Exception:
    defaults = {}

# Parse pr_categories from comma-separated string
pr_categories = [c.strip() for c in pr_categories_raw.split(",") if c.strip()] if pr_categories_raw.strip() else []

# Build the canonical project.json structure
doc = {
    "project_name": project_name,
    "repo_path": repo_abs,
    "language": language,
    "project_mode": project_mode,
    "state_dir": state_dir,
    "branch_pattern": "task-{epic}-{task}",
    "commit_pattern": "feat(epic-{epic}): complete task {task} — {title}",
    "hub_files": [],
    "preflight": defaults.get("preflight", {
        "check": "",
        "lint": "",
        "test": "",
        "build": ""
    }),
    "toolchain": defaults.get("toolchain", {}),
    "concurrency_cap": defaults.get("concurrency_cap", 2),
    "executor_token_cap": defaults.get("executor_token_cap", 60000),
    "mcp_servers": [],
    "task_source": {
        "type": "github_discussions",
        "imported_from": "epic_files",
        "epic_dir": "epics"
    },
    "project_claude_md": "CLAUDE.md",
}
if repo_slug:
    doc["repo"] = repo_slug
if pr_categories:
    doc["pr_categories"] = pr_categories

# Rust-specific toolchain defaults
if language == "rust":
    doc["toolchain"] = {
        "cargo_target_dir": state_dir + "/cargo-target",
        "sccache": True,
        "rust_toolchain_file": "rust-toolchain.toml"
    }

print(json.dumps(doc, indent=2))
PYEOF
)"

if [[ -f "$PROJECT_JSON" ]]; then
    # Merge: preserve user-edited keys, fill in missing ones from base
    MERGED="$(python3 - <<PYEOF
import json, sys
from pathlib import Path

existing = json.loads(Path("$PROJECT_JSON").read_text())
base = json.loads('''$BASE_JSON'''.replace("'", "'"))

# Deep-merge: base keys fill gaps, existing keys win
def deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result

merged = deep_merge(base, existing)
print(json.dumps(merged, indent=2))
PYEOF
)"
    printf '%s\n' "$MERGED" > "$PROJECT_JSON"
    echo "[=] Merged project.json: $PROJECT_JSON"
else
    printf '%s\n' "$BASE_JSON" > "$PROJECT_JSON"
    echo "[+] Created project.json: $PROJECT_JSON"
fi

# ---------------------------------------------------------------------------
# dashboard_port — scan-and-claim (idempotent: skip if already set)
# ---------------------------------------------------------------------------
echo ""
echo "--- Claiming dashboard port ---"
if python3 -c "
import json, sys
from pathlib import Path
d = json.loads(Path('$PROJECT_JSON').read_text())
port = d.get('dashboard_port')
sys.exit(0 if isinstance(port, int) else 1)
" 2>/dev/null; then
    echo "[=] dashboard_port already set — skipping port claim"
else
    CLAIMED_PORT="$(PYTHONPATH="$REPO_ROOT" python3 -m backend.fleet.port_claim "$PROJECT_NAME" "$STATE_DIR" 2>&1)" || {
        echo "[!] WARN: could not claim dashboard port: $CLAIMED_PORT" >&2
        echo "    Dashboard will fall back to default port 5173." >&2
    }
    if [[ "$CLAIMED_PORT" =~ ^[0-9]+$ ]]; then
        echo "[+] Claimed dashboard_port=$CLAIMED_PORT for $PROJECT_NAME"
        # Write dashboard_port and derived ports block into the repo-side project.json.
        # The ports block lets start-dashboard.sh assign all 4 service ports
        # from a single dashboard_port without colliding with other projects.
        python3 - <<PYEOF
import json, pathlib
p = pathlib.Path("$PROJECT_JSON")
d = json.loads(p.read_text()) if p.exists() else {}
dp = int("$CLAIMED_PORT")
d["dashboard_port"] = dp
# Only write ports block if not already present (idempotent)
if not d.get("ports"):
    d["ports"] = {
        "vite": dp,
        "api":  dp + 100,
        "rpc":  dp + 200,
        "sse":  dp + 300,
    }
p.write_text(json.dumps(d, indent=2) + "\n")
print(f"[+] Written dashboard_port={dp} and ports block to {p}")
PYEOF
    fi
fi

# ---------------------------------------------------------------------------
# State-side sentinel project.json — required for fleet discovery
# ---------------------------------------------------------------------------
# fleet.discovery scans ~/.{name}-state/project.json as the sentinel file.
# coldstart only wrote the repo-side project.json; without the state-side copy
# fleet discovery skips this project entirely (showing only other projects).
# Write a minimal sentinel with the required fields fleet.discovery needs.
STATE_PROJECT_JSON="$STATE_DIR/project.json"
if [[ ! -f "$STATE_PROJECT_JSON" ]]; then
    python3 - <<PYEOF
import json, pathlib, sys
repo_json = pathlib.Path("$PROJECT_JSON")
state_json = pathlib.Path("$STATE_PROJECT_JSON")

try:
    src = json.loads(repo_json.read_text())
except Exception as e:
    print(f"[!] WARN: could not read repo project.json: {e}", file=sys.stderr)
    src = {}

# Minimal sentinel — only fields fleet.discovery requires
sentinel = {
    "project_name": src.get("project_name", "$PROJECT_NAME"),
    "version": 1,
    "repo": src.get("repo", ""),
    "language": src.get("language", "$LANGUAGE"),
}
# Include dashboard_port if already claimed
port = src.get("dashboard_port")
if isinstance(port, int):
    sentinel["dashboard_port"] = port

state_json.write_text(json.dumps(sentinel, indent=2) + "\n")
print(f"[+] Created state-side sentinel: {state_json}")
PYEOF
else
    echo "[=] State-side sentinel exists: $STATE_PROJECT_JSON"
    # Sync dashboard_port, repo, and language into sentinel from repo-side project.json
    python3 - <<PYEOF 2>/dev/null || true
import json, pathlib
repo_json = pathlib.Path("$PROJECT_JSON")
state_json = pathlib.Path("$STATE_PROJECT_JSON")
try:
    src = json.loads(repo_json.read_text())
    sentinel = json.loads(state_json.read_text())
    changed = False
    port = src.get("dashboard_port")
    if isinstance(port, int) and sentinel.get("dashboard_port") != port:
        sentinel["dashboard_port"] = port
        changed = True
        print(f"[=] Synced dashboard_port={port} into state-side sentinel")
    # Also merge repo and language so fleet.discovery sees complete project metadata
    for field in ("repo", "language"):
        val = src.get(field)
        if val and not sentinel.get(field):
            sentinel[field] = val
            changed = True
            print(f"[=] Synced {field}={val!r} into state-side sentinel")
    if changed:
        state_json.write_text(json.dumps(sentinel, indent=2) + "\n")
except Exception:
    pass
PYEOF
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Summary ==="
echo "  State dir:    $STATE_DIR"
echo "  project.json: $PROJECT_JSON"
echo "  State sentinel: $STATE_PROJECT_JSON"
echo "  Language:     $LANGUAGE"
echo ""
echo "Next steps:"
echo "  1. Edit $PROJECT_JSON to set hub_files, preflight commands, and mcp_servers."
if [[ -d "$REPO_ABS/epics" ]]; then
    echo "  2. Import epic tasks:"
    if [[ -n "$REPO_SLUG" ]]; then
        echo "     python3 scripts/import-epic-tasks.py $REPO_ABS --repo $REPO_SLUG --dry-run"
    else
        echo "     python3 scripts/import-epic-tasks.py $REPO_ABS --repo <your-org>/<your-repo> --dry-run"
    fi
fi
echo "  3. Run scripts/start-dashboard.sh to launch the dashboard on the project-specific port."
echo ""
echo "Tip: re-run this script at any time — it merges existing project.json, never overwrites user edits."
