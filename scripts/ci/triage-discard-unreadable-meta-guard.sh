#!/usr/bin/env bash
# scripts/ci/triage-discard-unreadable-meta-guard.sh — a sidecar we could not
# read must never be treated as 'untriaged' (D#2133).
#
# What this guards
# ----------------
# `scripts/triage-orphan-diffs.sh discard-older-than` decides which patches to
# archive by reading each patch's `<patch>.meta.json` sidecar. It used to read
# that sidecar with a per-patch interpreter whose whole error handling was
# `except Exception: print('untriaged')`, wrapped again in
# `2>/dev/null || echo "untriaged"`. Three distinct outcomes collapsed into
# one value:
#
#   no sidecar at all  -> untriaged  (correct: nobody has triaged this patch)
#   malformed sidecar  -> untriaged  (wrong: we could not read it)
#   read/crash failure -> untriaged  (wrong: we could not read it)
#
# and 'untriaged' is exactly the value that makes a patch eligible for
# discard. A failure to read therefore presented as a clean negative result —
# the same shape as the crash-vs-empty collapse in pre-spawn-check.sh
# (PR #2109) and the `git status` exit-code discard in worktree-registry.sh
# (PR #2126).
#
# Discard here is a `git mv` into archive/orphan-diffs-discarded-<date>/ with
# a generated README, never `rm` or `git rm`, so the worst case was a
# recoverable misfile rather than lost work. That is why this is a guard on a
# correctness property and not an incident.
#
# What it does NOT guard
# ----------------------
# Nothing else. Not the spawn count (that assertion lives in
# tests/test_triage_orphan_diffs.sh), not the archive protocol, not the rest
# of the script. One property, one guard.
#
# It builds its own fixture pile in a temp directory and tears it down. It
# never reads or writes the repo's real archive/orphan-diffs — which does not
# exist in this repo at all, and must not be what a guard depends on.
#
# The run below is a REAL `discard-older-than`, not `--dry-run`: the thing
# being asserted is which patches actually get moved (D#2149).
#
# Exit 0: only the sidecar-less patch was discarded.
# Exit 1: a patch with an unreadable or malformed sidecar was discarded, or
#         the sidecar-less one was not, or the run itself failed.

set -uo pipefail

REPO_ROOT_REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TRIAGE="$REPO_ROOT_REAL/scripts/triage-orphan-diffs.sh"
NAME="$(basename "${BASH_SOURCE[0]}")"

if [[ ! -f "$TRIAGE" ]]; then
  echo "$NAME: FAIL — $TRIAGE does not exist" >&2
  exit 1
fi

FIX="$(mktemp -d)" || { echo "$NAME: FAIL — mktemp -d failed" >&2; exit 1; }
cleanup() {
  # chmod back first: a mode-000 file in a directory rm -rf can still remove,
  # but be explicit rather than relying on that.
  chmod -R u+rwX "$FIX" 2>/dev/null || true
  rm -rf "$FIX"
}
trap cleanup EXIT

PILE="$FIX/archive/orphan-diffs"
mkdir -p "$PILE"

_make_patch() {
  printf 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n' > "$PILE/$1"
}

# Fixture 1 — no sidecar. Genuinely untriaged; SHOULD be discarded.
_make_patch "no-sidecar.patch"

# Fixture 2 — sidecar present but not valid JSON. We could not read it, so we
# do not know its status; must NOT be discarded.
_make_patch "malformed-sidecar.patch"
printf '{"status": "salv' > "$PILE/malformed-sidecar.patch.meta.json"

# Fixture 3 — sidecar present and well-formed, but unreadable. Same
# conclusion, reached a different way; must NOT be discarded. Note the
# content is valid JSON with a discardable status on purpose: if the read
# were to succeed after all, this fixture would stop testing anything, so
# the readability check below refuses to let that pass silently.
_make_patch "unreadable-sidecar.patch"
printf '{"status":"untriaged","note":"","tagged_at":null,"tagged_by":null}\n' \
  > "$PILE/unreadable-sidecar.patch.meta.json"

# The fixture pile has to be tracked, because discard uses `git mv`.
# Commit BEFORE making anything unreadable — `git add` cannot read a
# mode-000 file either.
git -C "$FIX" init --quiet . >/dev/null 2>&1
git -C "$FIX" config user.email "guard@localhost" >/dev/null 2>&1
git -C "$FIX" config user.name "D2133 guard" >/dev/null 2>&1
git -C "$FIX" add -A >/dev/null 2>&1
if ! git -C "$FIX" commit --quiet -m "fixture pile" >/dev/null 2>&1; then
  echo "$NAME: FAIL — could not commit the fixture pile; the discard path needs a git repo" >&2
  exit 1
fi

# Make fixture 3 genuinely unreadable. mode 000 is the realistic form, but it
# is not a read failure for uid 0, and a guard that silently degrades to
# testing nothing on a root runner is worse than no guard. So the mode is
# applied, then actually verified, and a readable result falls back to a
# directory at the sidecar path — which no uid can json.load().
UNREADABLE_FORM="mode 000"
chmod 000 "$PILE/unreadable-sidecar.patch.meta.json"
if cat "$PILE/unreadable-sidecar.patch.meta.json" >/dev/null 2>&1; then
  UNREADABLE_FORM="directory at the sidecar path (running as uid $(id -u); mode 000 is not a read failure here)"
  chmod 644 "$PILE/unreadable-sidecar.patch.meta.json"
  rm -f "$PILE/unreadable-sidecar.patch.meta.json"
  mkdir -p "$PILE/unreadable-sidecar.patch.meta.json"
fi
echo "$NAME: unreadable fixture form — $UNREADABLE_FORM"

# Age every patch past the cutoff used below.
touch -t 202001010000 "$PILE"/*.patch

OUT="$(REPO_ROOT="$FIX" bash "$TRIAGE" discard-older-than 30d 2>&1)"
RC=$?

echo "--- discard-older-than 30d (real run, not --dry-run) — exit $RC"
printf '%s\n' "$OUT" | sed 's/^/    /'

FAILED=0

if [[ "$RC" -ne 0 ]]; then
  echo "$NAME: FAIL — the discard run itself exited $RC" >&2
  FAILED=1
fi

# The assertions are about the tree after the move, not about what the script
# said it would do.
_still_in_pile() { [[ -e "$PILE/$1" ]]; }

if _still_in_pile "no-sidecar.patch"; then
  echo "$NAME: FAIL — no-sidecar.patch was NOT discarded. A patch with no sidecar is genuinely untriaged and must stay discard-eligible; this guard is not a licence to stop discarding anything." >&2
  FAILED=1
fi

if ! _still_in_pile "malformed-sidecar.patch"; then
  echo "$NAME: FAIL — malformed-sidecar.patch was discarded. A sidecar that does not parse tells us nothing about the patch's status, so it must not read as 'untriaged'." >&2
  FAILED=1
fi

if ! _still_in_pile "unreadable-sidecar.patch"; then
  echo "$NAME: FAIL — unreadable-sidecar.patch was discarded. A sidecar we could not open tells us nothing about the patch's status, so it must not read as 'untriaged'." >&2
  FAILED=1
fi

# Kept-but-silent is the failure mode this whole thing is about, so the two
# kept patches also have to be named in the output.
for kept in malformed-sidecar.patch unreadable-sidecar.patch; do
  if ! printf '%s\n' "$OUT" | grep -qF "$kept"; then
    echo "$NAME: FAIL — $kept was kept but never mentioned in the output. An unreadable sidecar has to be a reported decision, not a silent skip." >&2
    FAILED=1
  fi
done

if [[ "$FAILED" -ne 0 ]]; then
  exit 1
fi

echo "$NAME: OK — only the sidecar-less patch was discarded; malformed and unreadable sidecars were kept and reported"
