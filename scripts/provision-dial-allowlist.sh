#!/usr/bin/env bash
# scripts/provision-dial-allowlist.sh — seed the dial directive allowlist so
# set_dial() isn't a no-op on a fresh install (D#1883).
#
# The allowlist file is NOT missing on a fresh install — dial_registry.py's
# _load_allowlist() already creates it, empty, on first read. The gap is
# authorization content, not file existence: an empty allowlist refuses
# every set_dial() call, raising *and* lowering alike, forever, because
# nothing ever puts an entry in it. This script is that "something" — the
# one seeding point, run once from coldstart-project.sh. It never runs
# inside set_dial() itself or the long-lived server process (see the
# module docstring in backend/dial_registry.py for why: STATE_DIR is
# resolved once per process, so in-process seeding there would target a
# stale directory).
#
# `empty == deny-all` is NOT changed by this script — that fail-closed
# default is untouched by design (D#1883 Decision 1). This only seeds
# content into a fresh state dir; it never weakens the gate.
#
# Idempotent: safe to re-run. Never duplicates an entry, never removes one
# — including an operator-added entry this script did not write.
#
# Usage:
#   bash scripts/provision-dial-allowlist.sh [repo-slug]
#
#   repo-slug   optional "owner/name", the same string coldstart-project.sh
#               already derives from the git `origin` remote. Used ONLY as
#               a fallback operator identity when `gh api user` can't
#               resolve one (gh absent, or not logged in). Never guessed
#               beyond that, never prompted for.
#
# Seeds up to two directive-source entries into
# <STATE_DIR>/dial-directive-allowlist.json:
#   {"kind": "system", "reason": "dashboard_rpc"}   — always
#   {"kind": "github_user", "login": "<resolved>"}  — only if a login resolves
#
# This script is also the command the runtime refusal message in
# dial_registry.py names — it must keep working standalone, with no args,
# with gh absent, and with gh unauthenticated. It never halts: a login
# that fails to resolve is a warning on stderr, not a failure exit.

set -uo pipefail

REPO_SLUG="${1:-}"

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
mkdir -p "$STATE_DIR"

ALLOWLIST_PATH="$STATE_DIR/dial-directive-allowlist.json"

# Resolve the operator login: gh first (the real identity), then the
# repo-owner fallback coldstart-project.sh already derived from the origin
# remote. Neither resolving is fine — dashboard-only is still a working
# brake per D#1883 Decision 5. Never prompt, never guess further, never halt.
OPERATOR_LOGIN=""
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    OPERATOR_LOGIN="$(gh api user --jq .login 2>/dev/null || true)"
fi
if [[ -z "$OPERATOR_LOGIN" && -n "$REPO_SLUG" ]]; then
    OPERATOR_LOGIN="${REPO_SLUG%%/*}"
fi
if [[ -z "$OPERATOR_LOGIN" ]]; then
    echo "[!] WARN: could not resolve a GitHub operator login (gh absent/unauthenticated, no repo-slug fallback available) — skipping the operator allowlist entry. Dashboard entry is still seeded; add your own entry to $ALLOWLIST_PATH by hand, or re-run this script once 'gh auth login' succeeds." >&2
fi

ALLOWLIST_PATH="$ALLOWLIST_PATH" OPERATOR_LOGIN="$OPERATOR_LOGIN" python3 - <<'PYEOF'
import json
import os
import pathlib
import sys
import time

path = pathlib.Path(os.environ["ALLOWLIST_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)

existing = []
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            existing = data
        else:
            raise ValueError(f"expected a JSON list, got {type(data).__name__}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # SEC-3 (D#1883 security review round 2): a corrupt or non-list
        # allowlist used to be swallowed silently and treated as empty —
        # deny-all became allow-2 with no warning, and any operator entries
        # already in the unparseable file were gone with no trace. Move it
        # aside instead of discarding it, and say so on stderr.
        backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        try:
            path.rename(backup)
            print(
                f"[!] WARN: {path} was not a valid JSON list ({exc}) — moved aside "
                f"to {backup} rather than discarded. Provisioning a fresh allowlist; "
                "recover any lost entries from the backup by hand.",
                file=sys.stderr,
            )
        except OSError as rename_exc:
            print(
                f"[!] WARN: {path} was not a valid JSON list ({exc}) and could not "
                f"be moved aside ({rename_exc}) — provisioning a fresh allowlist "
                "over it.",
                file=sys.stderr,
            )
        existing = []

desired = [{"kind": "system", "reason": "dashboard_rpc"}]
login = os.environ.get("OPERATOR_LOGIN", "")
if login:
    desired.append({"kind": "github_user", "login": login})

merged = list(existing)
for entry in desired:
    if entry not in merged:
        merged.append(entry)

if merged != existing:
    path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[+] Provisioned {path} ({len(merged)} entries)")
else:
    print(f"[=] {path} already provisioned")
PYEOF
