#!/usr/bin/env bash
# scripts/install-snapshot-timer.sh — install the 5-minute snapshot refresh timer.
#
# Idempotent: run it as many times as you like. It rewrites the two unit files,
# reloads, and re-enables. There is exactly one timer afterwards, never two.
#
# Why a systemd user timer and not cron: crontab is not on PATH on this NixOS
# host and the cron service is not enabled, so a cron entry would be
# unverifiable here and would need a system-level NixOS module edit. A user
# timer gives identical semantics with zero system-level change and is
# inspectable right now with `systemctl --user list-timers`.
#
# This installs a status-blob refresher. It does not re-enable the triggered
# loop; gates.loop_start stays false and nothing here spawns an agent.
#
# Usage:
#   bash scripts/install-snapshot-timer.sh            # install / re-install
#   bash scripts/install-snapshot-timer.sh --uninstall # stop, disable, remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_SRC="$REPO_ROOT/systemd"
UNIT_DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

SERVICE="loop-snapshot-refresh.service"
TIMER="loop-snapshot-refresh.timer"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "install-snapshot-timer: systemctl not found — cannot install a user timer." >&2
  echo "  The snapshot will go stale unless something else calls" >&2
  echo "  scripts/refresh-loop-snapshot.sh at least every 10 minutes." >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now "$TIMER" 2>/dev/null || true
  rm -f "$UNIT_DEST/$TIMER" "$UNIT_DEST/$SERVICE"
  systemctl --user daemon-reload
  echo "install-snapshot-timer: removed $TIMER and $SERVICE"
  exit 0
fi

mkdir -p "$UNIT_DEST"

# Rewrite both units from source every run — that is what makes re-running safe
# and what picks up a moved checkout. __REPO_ROOT__ is the only substitution.
for unit in "$SERVICE" "$TIMER"; do
  if [ ! -f "$UNIT_SRC/$unit" ]; then
    echo "install-snapshot-timer: missing source unit $UNIT_SRC/$unit" >&2
    exit 1
  fi
  sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$UNIT_SRC/$unit" > "$UNIT_DEST/$unit"
done

systemctl --user daemon-reload
# `enable --now` is itself idempotent: enabling an enabled unit re-links the
# symlink and starting a started timer is a no-op.
systemctl --user enable --now "$TIMER"

echo "install-snapshot-timer: $TIMER installed and active"
systemctl --user list-timers --all --no-pager | grep -E "NEXT|loop-snapshot-refresh" || true
