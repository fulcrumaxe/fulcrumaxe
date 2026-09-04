#!/usr/bin/env bash
# setup-state-dir.sh — bootstrap (or repair) external runtime state symlinks.
#
# Moves real (non-symlink) runtime state files from .autonomous-team/ to an
# external directory so that git worktree checkouts can never wipe them.
# Safe to run in main repo AND inside any worktree — it locates the manifest
# via the git work-tree root of the current directory, not via BASH_SOURCE.
#
# Usage:
#   bash scripts/setup-state-dir.sh
#   AUTONOMOUS_TEAM_STATE_DIR=/tmp/test-state bash scripts/setup-state-dir.sh
#
# Idempotent: running it a second time is a no-op.
#
# What it does:
#   1. Reads .autonomous-team/state-symlinks.json for the list of managed paths.
#   2. Creates $AUTONOMOUS_TEAM_STATE_DIR (default: ~/.fulcrumaxe-state).
#   3. For each entry, ensures the in-repo path is a symlink pointing to the
#      external target, migrating real files with backup when needed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the git work-tree root for $PWD (works in both main and worktrees).
# Priority:
#   1. SETUP_STATE_REPO_ROOT env var (test isolation override)
#   2. git rev-parse --show-toplevel (git work-tree detection)
#   3. The directory containing this script's parent (fallback for non-git use)
# ---------------------------------------------------------------------------
if [[ -n "${SETUP_STATE_REPO_ROOT:-}" ]]; then
  REPO_ROOT="$SETUP_STATE_REPO_ROOT"
elif git_root=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$git_root"
else
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.fulcrumaxe-state}"
TEAM_DIR="$REPO_ROOT/.autonomous-team"
MANIFEST="$TEAM_DIR/state-symlinks.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[setup-state-dir] $*"; }
warn()  { echo "[setup-state-dir] WARN: $*" >&2; }

# ---------------------------------------------------------------------------
# Guard: manifest must exist
# ---------------------------------------------------------------------------
if [[ ! -f "$MANIFEST" ]]; then
  warn "Manifest not found at $MANIFEST — cannot determine which paths to symlink."
  warn "Expected file: .autonomous-team/state-symlinks.json"
  exit 1
fi

# ---------------------------------------------------------------------------
# Parse manifest — requires jq
# ---------------------------------------------------------------------------
if ! command -v jq &>/dev/null; then
  warn "jq is required but not found. Install jq and re-run."
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Create external state dir + blackboard subdir
# ---------------------------------------------------------------------------
mkdir -p "$STATE_DIR/blackboard"
info "State dir  : $STATE_DIR"
info "Manifest   : $MANIFEST"

# ---------------------------------------------------------------------------
# 2. Process each tracked entry
# ---------------------------------------------------------------------------
migrated=0
already_linked=0
skipped_external_exists=0

while IFS=$'\t' read -r name ext_name entry_type; do
  [[ -z "$name" ]] && continue

  repo_path="$TEAM_DIR/$name"
  ext_path="$STATE_DIR/$ext_name"

  # Case A: already a symlink — verify target is correct; re-point if stale
  if [ -L "$repo_path" ]; then
    current_target=$(readlink "$repo_path" 2>/dev/null || true)
    if [ "$current_target" = "$ext_path" ]; then
      info "  $name — already symlinked (skipping)"
      ((already_linked++)) || true
    else
      ln -sf "$ext_path" "$repo_path"
      info "  $name — re-pointed: was $current_target → $ext_path"
      ((migrated++)) || true
    fi
    continue
  fi

  # Case B: real file/dir exists in repo
  if [ -e "$repo_path" ]; then
    if [ -e "$ext_path" ]; then
      # External target already exists — back up the in-repo copy, then symlink
      BACKUP_DIR="$TEAM_DIR/backups"
      mkdir -p "$BACKUP_DIR"
      TS=$(date -u +%Y%m%dT%H%M%SZ)
      BACKUP_PATH="$BACKUP_DIR/${name}-${TS}"
      warn "  $name — real file exists in worktree and external target exists; backing up to backups/${name}-${TS}"
      mv "$repo_path" "$BACKUP_PATH"
      ln -s "$ext_path" "$repo_path"
      info "  $name — backed up and symlinked to existing external target"
      ((already_linked++)) || true
    else
      # Move and symlink — external target does not exist yet
      if [ "$entry_type" = "dir" ] && [ -d "$repo_path" ]; then
        # Ensure blackboard subdir exists in external location
        mv "$repo_path" "$ext_path"
        mkdir -p "$ext_path"  # in case it was empty
      else
        mv "$repo_path" "$ext_path"
      fi
      ln -s "$ext_path" "$repo_path"
      info "  $name — moved to $ext_path and symlinked"
      ((migrated++)) || true
    fi
    continue
  fi

  # Case C: neither exists in repo — create symlink pointing at external
  # (the external target may or may not exist yet; that's fine)
  ln -s "$ext_path" "$repo_path"
  info "  $name — created symlink (external target may not exist yet)"
  ((migrated++)) || true

done < <(jq -r '.entries[] | "\(.in_repo)\t\(.external)\t\(.type)"' "$MANIFEST")

# ---------------------------------------------------------------------------
# 3. Summary
# ---------------------------------------------------------------------------
echo ""
info "Done."
info "  Migrated      : $migrated"
info "  Already linked: $already_linked"
info "  Skipped (backed up + symlinked): $skipped_external_exists"
echo ""
info "To use a custom state dir in future sessions, export:"
info "  export AUTONOMOUS_TEAM_STATE_DIR=\"$STATE_DIR\""
info "Or add it to your shell profile / .envrc."
