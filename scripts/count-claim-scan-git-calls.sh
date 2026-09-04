#!/usr/bin/env bash
# scripts/count-claim-scan-git-calls.sh — D#2158
#
# PATH-shim subprocess counter for a single `wtc_cmd_list` run (the claim
# scan in scripts/lib/worktree-claims.sh, run on every agent spawn via
# scripts/spawn-agent.sh section 0c).
#
# Why this exists: D#2158's panel found every wall-clock figure taken on
# this host untrustworthy — three back-to-back runs of unchanged code spread
# 44.1s-48.3s depending on concurrent agent load. Subprocess counts are
# load-invariant, so the Spec's acceptance criteria assert on counts and
# this script is the one thing that measures the same way twice. It counts
# invocations. It never times them.
#
# Usage:
#   bash scripts/count-claim-scan-git-calls.sh [--json]
#
# Emits one of these shapes on stdout, for exactly one `wtc_cmd_list` run
# against the real, live worktree population of this repo:
#   n_worktrees_enumerated      — worktrees `git worktree list` reports,
#                                 after skipping entry 0 (the primary
#                                 checkout — wtc_list_worktrees does the same)
#   n_worktrees_existing        — of those, how many still have a directory
#                                 on disk (the `[[ ! -d ]]` guard wtc_cmd_list
#                                 itself applies before scanning one)
#   git_subprocess_total        — every git invocation made during the run
#   git_status_porcelain_count  — of those, "status --porcelain" calls
#     (--json): {"n_worktrees_enumerated": N, "n_worktrees_existing": N,
#                "git_subprocess_total": N, "git_status_porcelain_count": N}
#     (default): one "key=value" line per field above
#
# How it counts: a wrapper named `git` is placed first on PATH for the
# duration of one `wtc_cmd_list` call. Every invocation appends its argument
# list to a log file and then execs the REAL git, so worktree-claims.sh gets
# real answers throughout — this is a count taken on the genuine classifier,
# not a mock standing in for it.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

REAL_GIT="$(command -v git)"
if [[ -z "$REAL_GIT" ]]; then
  echo "ERROR: no git found on PATH" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

CALLS_LOG="$WORKDIR/git-calls.log"
WT_LIST_OUT="$WORKDIR/worktree-list.out"
: > "$CALLS_LOG"

SHIM_DIR="$WORKDIR/shim"
mkdir -p "$SHIM_DIR"

# The shim also mirrors the raw output of the ONE `worktree list --porcelain`
# call the scan itself makes (via `tee`, so the real caller downstream still
# gets the same bytes) — population numbers come from that exact call rather
# than a second priming invocation, so every number here is derived from
# ONE `wtc_cmd_list` run, not one-and-a-half.
cat > "$SHIM_DIR/git" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALLS_LOG"
if [[ "\$*" == *"worktree list --porcelain"* ]]; then
  "$REAL_GIT" "\$@" | tee "$WT_LIST_OUT"
  exit "\${PIPESTATUS[0]}"
fi
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$SHIM_DIR/git"

PATH="$SHIM_DIR:$PATH" bash -c '
  set -uo pipefail
  # shellcheck source=scripts/lib/worktree-claims.sh
  source "'"$SCRIPT_DIR"'/lib/worktree-claims.sh"
  wtc_cmd_list
' >/dev/null 2>"$WORKDIR/stderr.log"

N_ENUM=0
N_EXIST=0
if [[ -f "$WT_LIST_OUT" ]]; then
  N_ENUM_RAW=$(grep -c '^worktree ' "$WT_LIST_OUT" || true)
  # entry 0 is always the primary/main checkout — wtc_list_worktrees skips
  # it and never counts it as a claim-bearing worktree; mirror that here.
  N_ENUM=$(( N_ENUM_RAW > 0 ? N_ENUM_RAW - 1 : 0 ))
  while IFS= read -r wt_path; do
    [[ -z "$wt_path" ]] && continue
    [[ -d "$wt_path" ]] && N_EXIST=$(( N_EXIST + 1 ))
  done < <(grep '^worktree ' "$WT_LIST_OUT" | tail -n +2 | sed 's/^worktree //')
fi

GIT_SUBPROCESS_TOTAL=$(wc -l < "$CALLS_LOG" | tr -d ' ')
GIT_STATUS_PORCELAIN_COUNT=$(grep -c -- 'status --porcelain' "$CALLS_LOG" || true)

if [[ "$JSON" -eq 1 ]]; then
  printf '{"n_worktrees_enumerated": %d, "n_worktrees_existing": %d, "git_subprocess_total": %d, "git_status_porcelain_count": %d}\n' \
    "$N_ENUM" "$N_EXIST" "$GIT_SUBPROCESS_TOTAL" "$GIT_STATUS_PORCELAIN_COUNT"
else
  printf 'n_worktrees_enumerated=%d\n' "$N_ENUM"
  printf 'n_worktrees_existing=%d\n' "$N_EXIST"
  printf 'git_subprocess_total=%d\n' "$GIT_SUBPROCESS_TOTAL"
  printf 'git_status_porcelain_count=%d\n' "$GIT_STATUS_PORCELAIN_COUNT"
fi
