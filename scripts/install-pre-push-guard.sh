#!/usr/bin/env bash
# scripts/install-pre-push-guard.sh
#
# Installs a local `pre-push` hook into THIS checkout that refuses a
# non-fast-forward update of the remote `main` — the shape that rewinds or
# rewrites published history. Everything else is left alone.
#
# Why this exists: branch protection and rulesets both return 403 on this
# repo's plan, so `origin/main` has no server-side guard. A local hook is the
# only push-side option actually available. It is per-checkout, unversioned
# (`.git/` is not tracked), and trivially bypassed with `--no-verify`.
#
# It is a GUARDRAIL, NOT A SECURITY BOUNDARY. It catches the accidental
# shape — a stray `reset --hard` followed by a force push — and nothing more.
# Read scripts/pre-push-guard.README.md before changing what it refuses; the
# scoring rule there is that over-blocking is the worse failure, because a
# guard that gets in the way of real work gets deleted by hand.
#
# Usage:  bash scripts/install-pre-push-guard.sh
#
# Idempotent. Prints one of "installed", "already installed", or "updated",
# and exits 0 in all three cases. Running it twice leaves exactly one hook.
#
# Exit 1 = could not install: a foreign `pre-push` already present (never
# clobbered), or `core.hooksPath` is set, which would make the installed file
# dead weight that never runs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $REPO_ROOT is not a git working tree — nothing to install into" >&2
  exit 1
fi

# `core.hooksPath` redirects git away from the common dir entirely. If it is
# set we refuse rather than write a file git will never execute: a hook that
# silently never runs is worse than no hook, because the operator believes
# they are covered. Report it and let them decide.
HOOKS_PATH_CONFIG="$(git -C "$REPO_ROOT" config --get core.hooksPath || true)"
if [[ -n "$HOOKS_PATH_CONFIG" ]]; then
  echo "ERROR: core.hooksPath is set to '$HOOKS_PATH_CONFIG' in this checkout." >&2
  echo "       git would ignore .git/hooks/pre-push, so installing there would" >&2
  echo "       produce a hook that never runs. Unset it (git config --unset" >&2
  echo "       core.hooksPath) and re-run, or install the guard into that path" >&2
  echo "       by hand." >&2
  exit 1
fi

# --git-common-dir, not --git-dir: in a linked worktree the latter points at
# .git/worktrees/<name>/, which has no hooks directory of its own. It can come
# back relative, so resolve it against the repo root.
GIT_COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$REPO_ROOT/$GIT_COMMON_DIR" ;;
esac
HOOKS_DIR="$(cd "$GIT_COMMON_DIR" && pwd)/hooks"
HOOK_FILE="$HOOKS_DIR/pre-push"

# The marker line is how a re-run recognises its own output. It has to appear
# verbatim in the hook body below.
MARKER="pre-push-guard: managed by scripts/install-pre-push-guard.sh"

mkdir -p "$HOOKS_DIR"

# ---------------------------------------------------------------------------
# The hook body. Quoted heredoc — nothing here is expanded by the installer;
# every expansion below happens when git runs the hook.
# ---------------------------------------------------------------------------
read -r -d '' HOOK_BODY <<'HOOKEOF' || true
#!/usr/bin/env bash
# pre-push-guard: managed by scripts/install-pre-push-guard.sh
#
# Refuses one shape only: an update to the remote `main` that is not a
# fast-forward — a rewind, a rewrite, or a deletion. Pushes to every other
# ref, force or not, are untouched, and a fast-forward of `main` is untouched.
#
# Bypass, documented on purpose rather than hidden:  git push --no-verify
#
# git hands us, on stdin, one line per ref being pushed:
#   <local ref> <local sha> <remote ref> <remote sha>
# and, in argv, the remote name and URL.
#
# Deliberately no `set -e`. This hook's failure mode matters: an unexpected
# non-zero from any probe below would exit non-zero and BLOCK a legitimate
# push. Every branch that cannot reach a verdict allows the push instead.

GUARDED_REF="refs/heads/main"
REMOTE_NAME="${1:-origin}"

# All-zeros of any length — sha1 (40) and sha256 (64) both.
is_zero() { case "$1" in ""|*[!0]*) return 1 ;; *) return 0 ;; esac; }

verdict=0

while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$remote_ref" = "$GUARDED_REF" ] || continue

  if is_zero "$local_sha"; then
    echo "pre-push guard: refusing to DELETE $GUARDED_REF on '$REMOTE_NAME'." >&2
    echo "  Intentional? Re-run the same push with --no-verify." >&2
    verdict=1
    continue
  fi

  # Nothing to rewind: the remote does not have this ref yet.
  is_zero "$remote_sha" && continue

  # The remote's commit has to be present locally for the ancestry test to
  # mean anything. If it is not (a stale or never-fetched remote), we cannot
  # tell a rewind from a fast-forward — say so and allow.
  # `</dev/null` on both git calls: they sit inside a `while read` loop that
  # is being fed the ref list, and a child that touched stdin would eat it.
  if ! git cat-file -e "${remote_sha}^{commit}" </dev/null 2>/dev/null; then
    echo "pre-push guard: $remote_sha not present locally; cannot check $GUARDED_REF, allowing." >&2
    continue
  fi

  git merge-base --is-ancestor "$remote_sha" "$local_sha" </dev/null 2>/dev/null
  rc=$?
  if [ "$rc" -eq 0 ]; then
    continue                       # fast-forward
  elif [ "$rc" -ne 1 ]; then
    # 1 means "not an ancestor"; anything else is an error, not a verdict.
    echo "pre-push guard: ancestry check failed (git exit $rc); allowing $GUARDED_REF push." >&2
    continue
  fi

  echo "pre-push guard: refusing non-fast-forward push to $GUARDED_REF on '$REMOTE_NAME'." >&2
  echo "  Remote is at ${remote_sha:0:12}, which is not an ancestor of ${local_sha:0:12} — this rewinds published history." >&2
  echo "  Intentional? Re-run the same push with --no-verify." >&2
  verdict=1
done

exit "$verdict"
HOOKEOF

if [[ -e "$HOOK_FILE" ]]; then
  if ! grep -qF "$MARKER" "$HOOK_FILE" 2>/dev/null; then
    echo "ERROR: $HOOK_FILE already exists and was not written by this script." >&2
    echo "       Refusing to overwrite it. Merge the guard in by hand, or move" >&2
    echo "       the existing hook aside and re-run." >&2
    exit 1
  fi
  if [[ "$(cat "$HOOK_FILE")" == "$HOOK_BODY" ]]; then
    # Repair the mode even on a no-op content match: a hook that is not
    # executable is a hook git skips.
    chmod 755 "$HOOK_FILE"
    echo "already installed: $HOOK_FILE"
    exit 0
  fi
  ACTION="updated"
else
  ACTION="installed"
fi

# Write through a temp file in the same directory so an interrupted run never
# leaves a half-written hook in place.
TMP_HOOK="$(mktemp "$HOOKS_DIR/.pre-push.XXXXXX")"
trap 'rm -f "$TMP_HOOK"' EXIT
printf '%s\n' "$HOOK_BODY" > "$TMP_HOOK"
chmod 755 "$TMP_HOOK"
mv -f "$TMP_HOOK" "$HOOK_FILE"
trap - EXIT

echo "$ACTION: $HOOK_FILE"
