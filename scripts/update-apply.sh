#!/usr/bin/env bash
# scripts/update-apply.sh — application half of /update (D#2335 Spec, PR 2).
#
# PR 1 shipped scripts/update-check.sh: it tells you truthfully whether this
# install is behind upstream. This script is the other half — it applies the
# update, and it applies it the one way the operator specified: by re-running
# loop-bootstrap/bootstrap.sh against this tree. There is no second updater
# in here. Every file this script causes to be written is written by
# bootstrap.sh; this script only decides *whether* to run it, shows you what
# it will do first, and reports honestly afterwards.
#
# Like update-check.sh, this file is a whole scripts/ BOOTSTRAP_PATHS entry,
# so it reaches every adopter repo with no manifest edit.
#
# Exit codes — same discipline as update-check.sh, and for the same reason:
#
#   0   applied, or already up to date (nothing needed doing)
#   10  preview only — the change set was printed and NOTHING was written
#   20  cannot proceed — the message carries a reason=<token>
#   2   usage error — bad arguments; message on stderr
#
# 1 is deliberately unused. A crash under `set -euo pipefail`, an unset
# variable under `set -u`, or a no-match `grep` all hand you 1 by accident;
# reserving it means a crash can never be misread as "applied fine".
#
# There is no default-to-0 branch anywhere below. If this script cannot
# establish that bootstrap ran and succeeded, it does not exit 0.
#
# ---------------------------------------------------------------------------
# Why the first run is always a preview, and why the preview is a REAL run
# ---------------------------------------------------------------------------
#
# D#2149 is the standing precedent here and it is worth restating, because
# this script is the sharpest instance of it in the repo: reap-worktrees.sh's
# --dry-run promised 116 removals the real run never made, because the guard
# that mattered sat inside the `dry_run == false` arm the preview never
# entered. A preview computed by a *different code path* than the apply is
# not evidence about the apply.
#
# So this script's preview is not bootstrap's `--dry-run` arm. It is the real
# `bootstrap.sh`, with no dry-run flag, run against a throwaway rsync mirror
# of this tree; the change set is then the file-level diff between that
# mirror and this tree. Same code path, same flags, same engine — the only
# difference is which directory it lands in.
#
# Two divergences between that mirror run and the real run exist, and they
# are disclosed rather than claimed away — both here and in the preview's own
# printed output:
#
#   1. The memory destination path is derived from the target's absolute
#      path (bootstrap.sh's TARGET_SLUG), so the mirror writes into
#      .claude/projects/-<mirror-slug>/ where the real run writes into
#      .claude/projects/-<target-slug>/. The diff normalizes the mirror side
#      back to the target slug before comparing, so those files are
#      classified against their real counterparts rather than all showing up
#      as creations. This is a genuine preview/real divergence, caught and
#      corrected — not one we discovered by trusting the preview.
#
#   2. The mirror run is executed with `gh` de-authenticated (empty
#      GH_TOKEN/GITHUB_TOKEN, throwaway GH_CONFIG_DIR) so that previewing an
#      update cannot create labels or open a team-log Issue on the real
#      GitHub repo. Two bootstrap steps are gated on `gh auth status`
#      succeeding — the bot_account merge into .autonomous-team/config.json
#      (step 16b) and the team-log Issue creation (step 21) — so the preview
#      cannot observe them. Both are guarded no-ops on an install that
#      already has those values, which is every install an update runs
#      against; on an install that does not, the real run may additionally
#      write a `bot_account` key into config.json. The preview says so out
#      loud rather than pretending it covered it.
#
# ---------------------------------------------------------------------------
# Why this does not front scripts/engine-sync/
# ---------------------------------------------------------------------------
#
# engine-sync (D#1535/D#1586) solves an adjacent but genuinely different
# problem: it FETCHES upstream content over the network, verifies it against
# a signed tag, a TOFU-pinned key and per-file SHA-256 manifest, classifies
# every path against a recorded baseline so local patches survive, and then
# opens a PR in the sibling repo for a human to review. It never writes a
# working tree directly and never merges.
#
# /update is the operator's local reinstall: an engine tree they already have
# on disk and already trust, re-run over their project, writing the working
# tree immediately. No fetch, no signature to verify, no PR. Routing it
# through engine-sync would require a signed tag and a blob mirror that do
# not exist for a local clone, and would contradict the mechanism the
# operator fixed ("the mechanism is bootstrap").
#
# The overlap is real and worth naming: both are "make this install current".
# The honest split today is that engine-sync updates *the engine* across
# repos with verification and review, while /update propagates *an engine you
# already have* into *this* project. This script keeps that seam visible
# instead of hiding it — after a successful apply it re-runs update-check.sh
# and, if the freshly-stamped baseline is still behind upstream, says so
# loudly: that means the local ENGINE_ROOT is itself stale, which is exactly
# the gap engine-sync's fetch half covers. Unifying the two is a real design
# question but it is larger than this PR; see the PR description.
#
# ---------------------------------------------------------------------------
# What is never touched
# ---------------------------------------------------------------------------
#
#   $AUTONOMOUS_TEAM_STATE_DIR (~/.autonomous-forever-state/) — the
#   blackboard, stats.duckdb, audit.jsonl. bootstrap.sh contains zero
#   references to it (grepped: no hits for AUTONOMOUS_TEAM_STATE_DIR,
#   autonomous-forever-state, state_paths, or setup-state-dir), and this
#   script adds none. Nothing in an update reads or writes it.
#
#   Nothing is ever removed, so nothing is ever archived. bootstrap.sh's only
#   bulk copy is `rsync -a` with no --delete, and no step in it removes a
#   file from the target. The Archive Protocol therefore has nothing to
#   apply to here; scripts/ci/update-apply-guard.py asserts that mechanically
#   rather than leaving it as a claim in a comment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATE_CHECK="$REPO_ROOT/scripts/update-check.sh"
PREVIEW_MARKER="$REPO_ROOT/.autonomous-team/update-preview.json"

usage() {
  cat <<'EOF'
Usage: scripts/update-apply.sh --engine-root <path> [--repo OWNER/NAME] [--dry-run]
       scripts/update-apply.sh --help

Applies a pending fulcrumaxe update to THIS repo by re-running the engine's
loop-bootstrap/bootstrap.sh over it. Run scripts/update-check.sh first (this
script runs it for you) to see whether there is anything to apply.

The first invocation for a given engine commit ALWAYS previews and writes
nothing, whatever flags you pass. It prints every path that would be created
or overwritten, then stops. Run it a second time to actually apply.

Options:
  --engine-root <path>   The fulcrumaxe engine tree to update from: a clone,
                         an export, or an installed plugin root. Must contain
                         loop-bootstrap/bootstrap.sh, scripts/ and backend/.
                         Falls back to $FULCRUMAXE_ENGINE_ROOT when omitted.
                         Never guessed by searching the filesystem (D#2214).
  --repo OWNER/NAME      Target repo slug passed to bootstrap. Defaults to
                         the slug resolved from .autonomous-team/config.json
                         (scripts/lib/repo-resolve.sh).
  --dry-run              Preview and stop, even if a preview was already
                         shown for this engine commit.
  --help, -h             Print this message and exit 0.

Exit codes:
  0    applied, or already up to date (nothing needed doing)
  10   preview only — change set printed, nothing written
  20   cannot proceed — see the reason=<token> in the message
  2    usage error (message on stderr)
EOF
}

die_usage() {
  echo "error: $1" >&2
  usage >&2
  exit 2
}

cannot_proceed() {
  # One shape for every refusal, so a caller can grep reason= the same way
  # it does for update-check.sh. Never prints anything resembling a verdict.
  echo "cannot proceed: $1" >&2
  exit 20
}

ENGINE_ROOT="${FULCRUMAXE_ENGINE_ROOT:-}"
TARGET_REPO=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --engine-root)
      [[ $# -ge 2 ]] || die_usage "--engine-root requires a path"
      ENGINE_ROOT="$2"; shift 2 ;;
    --repo)
      [[ $# -ge 2 ]] || die_usage "--repo requires OWNER/NAME"
      TARGET_REPO="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. Verdict first — never apply blind, and never restate a verdict we did
#    not get. update-check.sh prints 0/10 on stdout and 20/2 on stderr, so
#    capture both streams together.
# ---------------------------------------------------------------------------

[[ -f "$UPDATE_CHECK" ]] || cannot_proceed "scripts/update-check.sh is missing from this install (reason=update_check_missing) — re-run bootstrap from the engine to reinstall it"

set +e
CHECK_OUT="$(bash "$UPDATE_CHECK" 2>&1)"
CHECK_RC=$?
set -e

case "$CHECK_RC" in
  0)
    # Idempotence: the second /update --apply in a row lands here. Nothing
    # is written, and any stale preview marker is cleared so a later real
    # update starts from a fresh preview.
    rm -f "$PREVIEW_MARKER"
    echo "already up to date — nothing to apply"
    echo "  update-check.sh says: $CHECK_OUT"
    exit 0
    ;;
  10)
    echo "$CHECK_OUT"
    ;;
  20)
    # Applying is legitimate here (re-running bootstrap is in fact the
    # documented remedy for reason=no_baseline_recorded, and it writes a
    # fresh stamp) — but we must not dress this up as a measured verdict.
    echo "note: could not verify how far behind this install is, and will not guess."
    echo "  update-check.sh says: $CHECK_OUT"
    echo "  Re-running bootstrap is still valid, and records a fresh baseline."
    ;;
  2)
    cannot_proceed "scripts/update-check.sh rejected its own invocation (reason=update_check_usage_error) — this install's update-check.sh is out of step with update-apply.sh: $CHECK_OUT"
    ;;
  *)
    cannot_proceed "scripts/update-check.sh exited $CHECK_RC, which is not one of its four documented codes (reason=update_check_failed) — treating as a crash, not a verdict: $CHECK_OUT"
    ;;
esac

# ---------------------------------------------------------------------------
# 2. Resolve ENGINE_ROOT. Never by searching the filesystem: D#2214 measured
#    that a search silently finds *some other* engine tree and bypasses the
#    installed plugin with no error at all. The plugin case cannot be
#    resolved from a shell (CLAUDE_PLUGIN_ROOT is not exported into the Bash
#    tool's environment) — .claude/commands/update.md resolves it from its
#    own substituted marker line and passes it in via --engine-root.
# ---------------------------------------------------------------------------

if [[ -z "$ENGINE_ROOT" ]]; then
  cannot_proceed "no engine tree given (reason=engine_root_unresolved) — pass --engine-root <path> or set FULCRUMAXE_ENGINE_ROOT. Run /update rather than this script directly and it will resolve the installed plugin's root for you"
fi

if [[ ! -d "$ENGINE_ROOT" ]]; then
  cannot_proceed "engine root '$ENGINE_ROOT' is not a directory (reason=engine_root_unresolved)"
fi
ENGINE_ROOT="$(cd "$ENGINE_ROOT" && pwd)"

# Self-reference is checked BEFORE the content check below: an adopter's own
# project has scripts/ and backend/ but no loop-bootstrap/, so pointing
# --engine-root at it would otherwise be diagnosed as "this is not an engine
# tree" when the actionable truth is "you pointed at yourself".
if [[ "$ENGINE_ROOT" == "$REPO_ROOT" ]]; then
  cannot_proceed "engine root and this repo are the same directory (reason=engine_root_is_target) — /update installs an engine INTO a project; point --engine-root at the engine clone, export or plugin instead"
fi

# Content check, not a path-shape check — same assertion /coldstart Step 0
# makes, for the same reason: a legitimately installed plugin can live
# anywhere on disk, so assert the tree IS the engine.
if [[ ! -f "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" ]] || [[ ! -d "$ENGINE_ROOT/scripts" ]] || [[ ! -d "$ENGINE_ROOT/backend" ]]; then
  cannot_proceed "engine root '$ENGINE_ROOT' is missing loop-bootstrap/bootstrap.sh, scripts/ or backend/ (reason=engine_root_not_engine) — it does not look like a fulcrumaxe engine tree"
fi

# ---------------------------------------------------------------------------
# 3. Resolve the target repo slug bootstrap needs for its identifier rewrite.
# ---------------------------------------------------------------------------

if [[ -z "$TARGET_REPO" ]]; then
  if [[ -f "$REPO_ROOT/scripts/lib/repo-resolve.sh" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
    TARGET_REPO="$(_resolve_repo 2>/dev/null || true)"
  fi
fi

if [[ -z "$TARGET_REPO" ]]; then
  cannot_proceed "could not resolve this project's repo slug (reason=target_repo_unresolved) — pass --repo OWNER/NAME, set AUTONOMOUS_TEAM_REPO, or add a \"repo\" field to .autonomous-team/config.json"
fi

if [[ ! "$TARGET_REPO" =~ ^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]; then
  cannot_proceed "resolved repo slug '$TARGET_REPO' is not OWNER/NAME (reason=target_repo_malformed) — bootstrap uses it as a sed replacement value across the installed tree, so it is not passed through unvalidated"
fi

ENGINE_COMMIT="$(git -C "$ENGINE_ROOT" rev-parse HEAD 2>/dev/null || echo "unknown")"

echo ""
echo "engine root:  $ENGINE_ROOT (commit ${ENGINE_COMMIT})"
echo "this project: $REPO_ROOT (repo $TARGET_REPO)"

# ---------------------------------------------------------------------------
# 4. Has this exact engine commit already been previewed for this tree?
# ---------------------------------------------------------------------------

marker_authorizes_apply() {
  [[ -f "$PREVIEW_MARKER" ]] || return 1
  python3 - "$PREVIEW_MARKER" "$ENGINE_COMMIT" "$ENGINE_ROOT" <<'MARKER_PY'
import json
import sys

path, commit, engine_root = sys.argv[1:4]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(1)
# The marker authorizes applying ONE specific engine commit into this tree.
# A preview of engine commit A must not silently authorize applying engine
# commit B. When the engine has no .git to read a commit from (an export or
# a plugin tarball), the commit is the literal "unknown" on both sides and
# the engine root path is what has to match instead — stated here rather
# than pretended otherwise.
sys.exit(0 if data.get("engine_commit") == commit and data.get("engine_root") == engine_root else 1)
MARKER_PY
}

if [[ "$DRY_RUN" != "true" ]] && marker_authorizes_apply; then
  PREVIEWED_AT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("generated_at",""))' "$PREVIEW_MARKER" 2>/dev/null || true)"
  echo "applying the change set previewed at ${PREVIEWED_AT:-an earlier run}"
  echo ""
else
  # -------------------------------------------------------------------------
  # 5. Preview — the REAL bootstrap, into a throwaway mirror of this tree.
  # -------------------------------------------------------------------------
  command -v rsync >/dev/null 2>&1 || cannot_proceed "rsync is not on PATH (reason=rsync_missing) — bootstrap needs it too, so this install cannot be updated without it"

  MIRROR_PARENT="$(mktemp -d)"
  trap 'rm -rf "$MIRROR_PARENT"' EXIT
  MIRROR="$MIRROR_PARENT/mirror"
  mkdir -p "$MIRROR"

  echo ""
  echo "==> computing the change set by running the real bootstrap against a throwaway mirror"

  # .git is excluded and replaced with a fresh empty repo: bootstrap only
  # requires that the target IS a git repo (`git rev-parse --git-dir`), and
  # copying a large history in would cost far more than it tells us. The
  # other excludes are dependency/build caches; no step in bootstrap.sh
  # reads any path under them, so omitting them cannot change what it does.
  rsync -a \
    --exclude='.git/' \
    --exclude='node_modules/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='.mypy_cache/' \
    --exclude='.pytest_cache/' \
    "$REPO_ROOT/" "$MIRROR/"
  git -C "$MIRROR" init -q
  git -C "$MIRROR" config user.email "update-preview@localhost"
  git -C "$MIRROR" config user.name "update preview"

  MIRROR_LOG="$MIRROR_PARENT/bootstrap.log"
  GH_SANDBOX="$MIRROR_PARENT/gh-config"
  mkdir -p "$GH_SANDBOX"

  set +e
  env GH_TOKEN="" GITHUB_TOKEN="" GH_CONFIG_DIR="$GH_SANDBOX" \
    bash "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" --repo "$TARGET_REPO" "$MIRROR" \
    >"$MIRROR_LOG" 2>&1
  MIRROR_RC=$?
  set -e

  if [[ $MIRROR_RC -ne 0 ]]; then
    echo "--- bootstrap output (mirror run) ---" >&2
    tail -40 "$MIRROR_LOG" >&2
    cannot_proceed "the engine's bootstrap.sh failed against a throwaway mirror of this tree (reason=bootstrap_failed, exit $MIRROR_RC) — nothing was written to this repo. Full log above"
  fi

  MIRROR_REAL="$(cd "$MIRROR" && pwd)"
  echo ""
  python3 - "$MIRROR_REAL" "$REPO_ROOT" <<'DIFF_PY'
import os
import sys
from pathlib import Path

mirror, target = Path(sys.argv[1]), Path(sys.argv[2])


def slug(p: Path) -> str:
    # bootstrap.sh: TARGET_SLUG=$(echo "$TARGET" | sed 's|^/||; s|/|-|g')
    return str(p).lstrip("/").replace("/", "-")


mirror_marker = f".claude/projects/-{slug(mirror)}/"
target_marker = f".claude/projects/-{slug(target)}/"

SKIP_DIRS = {".git"}
SKIP_RELS = {".autonomous-team/update-preview.json"}

created, overwritten, unchanged = [], [], 0

for root, dirs, files in os.walk(mirror):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for name in files:
        src = Path(root) / name
        rel = str(src.relative_to(mirror))
        if rel in SKIP_RELS:
            continue
        # Normalize the memory destination back to the real target's slug —
        # see this script's header: bootstrap derives it from the target's
        # absolute path, so the mirror writes to a different directory than
        # the real run would. Comparing the un-normalized path would report
        # every memory file as a creation.
        norm = rel.replace(mirror_marker, target_marker, 1) if rel.startswith(mirror_marker) else rel
        dst = target / norm
        if not dst.exists():
            created.append(norm)
        else:
            try:
                same = src.read_bytes() == dst.read_bytes()
            except OSError:
                same = False
            if same:
                unchanged += 1
            else:
                overwritten.append(norm)

created.sort()
overwritten.sort()

print(f"change set: {len(created)} created, {len(overwritten)} overwritten, "
      f"{unchanged} unchanged, 0 removed/archived")
print()
if created:
    print(f"would CREATE ({len(created)}):")
    for p in created:
        print(f"  + {p}")
    print()
if overwritten:
    print(f"would OVERWRITE ({len(overwritten)}):")
    for p in overwritten:
        print(f"  ~ {p}")
    print()
if not created and not overwritten:
    print("no file in this tree would change.")
    print()
print("would REMOVE/ARCHIVE (0): bootstrap.sh never removes a file from the "
      "target — its only bulk copy is `rsync -a` with no --delete — so an "
      "update has nothing to archive under the Archive Protocol.")
DIFF_PY

  # What the apply will NOT do. bootstrap prints this itself, on stdout, at
  # the end of its agent-install pass: every .claude/agents/*.md file that
  # differs from the engine's copy is SKIPPED (local overrides are preserved
  # unless you pass --force), and it reports them once as a batch. The mirror
  # run reproduces that faithfully — the mirror is a copy of this tree, so the
  # same files diverge there — but the whole mirror log is captured to a file
  # and was previously only surfaced when bootstrap FAILED. That meant the one
  # message describing what an apply deliberately leaves alone never reached
  # the operator, and a complete-looking change set read as "your agents were
  # updated" when they were not. Surface it on the success path, which is the
  # only path it is ever printed on.
  #
  # bootstrap writes its own $TARGET into those lines, which is the mirror
  # here; rewrite it to this repo so the remedy it prints is runnable. (Plain
  # substring replacement: a mktemp -d path contains no glob metacharacters.)
  AGENT_REPORT="$(awk '/^[0-9]+ agent definition\(s\) have upstream updates you are not receiving:/{f=1} f&&/^==>/{f=0} f' "$MIRROR_LOG")"
  if [[ -n "$AGENT_REPORT" ]]; then
    echo "Upstream agent-definition updates this apply will NOT take:"
    echo "${AGENT_REPORT//$MIRROR_REAL/$REPO_ROOT}"
    cat <<EOF
  (the Accept: command above is relative to $ENGINE_ROOT)

  Two things to read carefully in that list, both measured against a real
  install rather than assumed:

  - Some of those files may ALSO appear as overwritten in the change set
    above. That is the repo-identifier rewrite — bootstrap sweeps
    .claude/agents/ with it on every run — not the upstream content update.
    The upstream content update is the part being withheld.
  - The count is an upper bound, not a count of real upstream changes.
    bootstrap compares the engine's own copy of each agent file against
    yours AFTER the identifier rewrite has been applied to yours, so on an
    install whose slug differs from the engine's, files that differ ONLY by
    that rewrite are listed too. It does not err the other way: a file with
    a genuine upstream change is always listed.
EOF
    echo ""
  fi

  cat <<EOF

Not covered by the preview above (disclosed, not claimed away):

  - An apply never takes the upstream content of a .claude/agents/*.md file
    that differs from the engine's copy, and never updates CLAUDE.md at all
    after the first install. Any such file is named in the report above; if
    nothing is named, none are diverging. Taking those updates is a
    separate, explicit --force run of bootstrap, because it also discards
    local edits to them.
  - The mirror run had gh de-authenticated so previewing could not create
    labels or open a team-log Issue on $TARGET_REPO. The two bootstrap steps
    gated on an authenticated gh are the bot_account merge into
    .autonomous-team/config.json and team-log Issue creation; both are
    no-ops when those values already exist, which is the normal case for an
    install being updated.
EOF

  mkdir -p "$REPO_ROOT/.autonomous-team"
  python3 - "$PREVIEW_MARKER" "$ENGINE_COMMIT" "$ENGINE_ROOT" <<'WRITE_MARKER_PY'
import json
import sys
from datetime import datetime, timezone

path, commit, engine_root = sys.argv[1:4]
with open(path, "w") as f:
    json.dump({
        "engine_commit": commit,
        "engine_root": engine_root,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, f, indent=2)
    f.write("\n")
WRITE_MARKER_PY

  echo ""
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "dry run — nothing was written to this repo."
  else
    echo "preview only — nothing was written to this repo."
  fi
  echo "To apply the change set above, run the same command again:"
  echo "  bash scripts/update-apply.sh --engine-root \"$ENGINE_ROOT\""
  exit 10
fi

# ---------------------------------------------------------------------------
# 6. Apply — the same bootstrap invocation, against this tree for real.
# ---------------------------------------------------------------------------

echo "==> re-running the engine's bootstrap against this repo"
set +e
bash "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" --repo "$TARGET_REPO" "$REPO_ROOT"
APPLY_RC=$?
set -e

if [[ $APPLY_RC -ne 0 ]]; then
  cannot_proceed "the engine's bootstrap.sh exited $APPLY_RC (reason=bootstrap_failed) — this tree may be partially updated; re-run once the cause above is fixed"
fi

# The preview authorized exactly one engine commit. It has been consumed, so
# the next update previews again from scratch.
rm -f "$PREVIEW_MARKER"

# ---------------------------------------------------------------------------
# 7. Report honestly on what the install now is. bootstrap's step 19a
#    refreshed .autonomous-team/engine-install.json, so this is a real
#    re-measurement, not an assumption that the apply worked.
# ---------------------------------------------------------------------------

set +e
POST_OUT="$(bash "$UPDATE_CHECK" 2>&1)"
POST_RC=$?
set -e

echo ""
echo "applied engine commit ${ENGINE_COMMIT} to $REPO_ROOT"
case "$POST_RC" in
  0)
    echo "update-check.sh now reports: $POST_OUT"
    ;;
  10)
    echo "update-check.sh now reports: $POST_OUT"
    echo ""
    echo "This install now matches the engine tree at $ENGINE_ROOT, but that engine"
    echo "tree is itself behind upstream — so this project is still behind upstream."
    echo "Bring the engine current first (git -C \"$ENGINE_ROOT\" pull), then run"
    echo "/update again. This is the seam scripts/engine-sync/ covers; /update"
    echo "deliberately does not fetch from the network on your behalf."
    ;;
  *)
    echo "update-check.sh could not confirm the result: $POST_OUT"
    echo "The apply itself succeeded (bootstrap exited 0); only the follow-up"
    echo "measurement is unavailable. Not reporting this as 'up to date'."
    ;;
esac

exit 0
