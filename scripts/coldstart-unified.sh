#!/usr/bin/env bash
# scripts/coldstart-unified.sh — the one-command onboarding front door (D#1872).
#
# Runs loop-bootstrap/bootstrap.sh's POPULATION (23 agent definitions,
# CLAUDE.md, backend/, .claude/commands/) followed by scripts/coldstart.sh's
# PROVISIONING (state dir, deps, labels, sandbox hook, HALT/interview, seed)
# against the same target repo, in that order.
#
# The order is measured, not assumed (D#1872 item 1): running provisioning
# before population leaves the target repo with only a state dir and a
# project.json -- no agents, no CLAUDE.md, no backend/ -- while still
# exiting 0 and reaching the interview HALT. That is the reported defect
# this script exists to close. Step 2 below asserts population actually
# happened before provisioning runs, so reversing the two calls in this
# script (or skipping step 1) fails loudly here instead of silently
# producing that half-populated repo (D#1872 item 2).
#
# Usage:
#   bash scripts/coldstart-unified.sh --repo OWNER/NAME --path /path/to/target \
#       --name PROJECT_NAME [--mode existing|new] [--language rust|python|typescript|polyglot] \
#       [--backlog <epics-dir>] [--dry-run] [--resume] [--force]
#
# --dry-run prints the planned bootstrap.sh AND coldstart.sh actions and
# exits 0 with no mutations (delegates to each script's own --dry-run seam).
# --resume skips straight to coldstart.sh's --resume (seed-only) path --
# population is assumed already done, since --resume is only valid on a
# target that has already been through this script once.
#
# This script must be run from a tree that has loop-bootstrap/bootstrap.sh
# alongside it -- it invokes that script by relative path, which only
# exists here (a clone of the fulcrumaxe engine repo, the open-source
# export, or an installed fulcrumaxe plugin), not in the target repo it
# operates on.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOTSTRAP_SH="$REPO_ROOT/loop-bootstrap/bootstrap.sh"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart.sh"

if [[ ! -f "$BOOTSTRAP_SH" ]]; then
  echo "ERROR: $BOOTSTRAP_SH not found." >&2
  echo "       This script must be run from a tree that has loop-bootstrap/" >&2
  echo "       alongside it — a clone of the fulcrumaxe engine repo, the" >&2
  echo "       open-source export, or an installed fulcrumaxe plugin all" >&2
  echo "       carry it. It won't be found in a repo bootstrap.sh itself" >&2
  echo "       populated (loop-bootstrap/ deliberately doesn't install" >&2
  echo "       itself into a target — see open-source/MANIFEST.md)." >&2
  exit 1
fi

TARGET_REPO=""
TARGET_PATH=""
PROJECT_NAME=""
MODE="existing"
LANGUAGE="polyglot"
BACKLOG=""
DRY_RUN=false
RESUME=false
FORCE=false
EXTRA_COLDSTART_ARGS=()
MODE_SEEN=false
LANGUAGE_SEEN=false

# Repeats of the same canonical flag are rejected outright, rather than
# letting the last one silently win, for two reasons: (1) EXTRA_COLDSTART_ARGS
# is appended AFTER these canonical args in the real coldstart.sh invocation
# below, and coldstart.sh's own parser is last-wins, so a pass-through extra
# that happens to repeat one of these flag names would override this
# script's own value in the child call without this script (or its ordering
# gate, which reads $TARGET_PATH etc. directly) ever seeing the override;
# (2) even a same-loop repeat of one of these flags silently redirects
# population, the ordering gate, and provisioning to a different target with
# no warning that the first value was discarded. Both are CWE-88 shaped.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ -n "$TARGET_REPO" ]] && { echo "ERROR: --repo specified more than once" >&2; exit 1; }
      TARGET_REPO="${2:-}"; shift 2 || { echo "ERROR: --repo requires OWNER/NAME" >&2; exit 1; } ;;
    --path)
      [[ -n "$TARGET_PATH" ]] && { echo "ERROR: --path specified more than once" >&2; exit 1; }
      TARGET_PATH="${2:-}"; shift 2 || { echo "ERROR: --path requires a value" >&2; exit 1; } ;;
    --name)
      [[ -n "$PROJECT_NAME" ]] && { echo "ERROR: --name specified more than once" >&2; exit 1; }
      PROJECT_NAME="${2:-}"; shift 2 || { echo "ERROR: --name requires a value" >&2; exit 1; } ;;
    --mode)
      [[ "$MODE_SEEN" == "true" ]] && { echo "ERROR: --mode specified more than once" >&2; exit 1; }
      MODE_SEEN=true
      MODE="${2:-}"; shift 2 || { echo "ERROR: --mode requires a value" >&2; exit 1; } ;;
    --language)
      [[ "$LANGUAGE_SEEN" == "true" ]] && { echo "ERROR: --language specified more than once" >&2; exit 1; }
      LANGUAGE_SEEN=true
      LANGUAGE="${2:-}"; shift 2 || { echo "ERROR: --language requires a value" >&2; exit 1; } ;;
    --backlog)
      BACKLOG="${2:-}"; shift 2 || { echo "ERROR: --backlog requires a value" >&2; exit 1; } ;;
    --dry-run) DRY_RUN=true; shift ;;
    --resume) RESUME=true; shift ;;
    --force) FORCE=true; shift ;;
    --help|-h)
      cat <<'USAGE'
Usage: bash scripts/coldstart-unified.sh --repo OWNER/NAME --path /path/to/target \
    --name PROJECT_NAME [--mode existing|new] [--language rust|python|typescript|polyglot] \
    [--backlog <epics-dir>] [--dry-run] [--resume] [--force]

Runs loop-bootstrap/bootstrap.sh's population followed by scripts/coldstart.sh's
provisioning against the same target repo, in that order. --repo and --path are
required; --name defaults to the target directory's basename. --dry-run prints
the planned actions with no mutations.
USAGE
      exit 0 ;;
    -*) EXTRA_COLDSTART_ARGS+=("$1"); shift ;;
    *) echo "Unknown positional arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET_REPO" || -z "$TARGET_PATH" ]]; then
  echo "Usage: bash scripts/coldstart-unified.sh --repo OWNER/NAME --path /path/to/target --name PROJECT_NAME [options]" >&2
  exit 1
fi
if [[ -z "$PROJECT_NAME" ]]; then
  PROJECT_NAME="$(basename "$TARGET_PATH")"
fi

# ---------------------------------------------------------------------------
# Refuse dangerous targets — BEFORE any path below can write anything.
# Mirrors loop-bootstrap/bootstrap.sh's own guard (realpath + refuse / and
# $HOME), duplicated here rather than relied upon because two paths through
# this script reach a write before bootstrap.sh ever runs its own check:
# the --mode new pre-scaffold block below (git init straight into
# TARGET_PATH) and the --resume branch immediately below this, which execs
# straight into coldstart.sh and never calls bootstrap.sh at all.
# ---------------------------------------------------------------------------
TARGET_PATH="$(realpath "$TARGET_PATH")"
# Resolve $HOME too, not just the target — a symlinked $HOME (NixOS,
# impermanence, some corp NFS layouts) would otherwise never compare equal
# to the realpath'd target, letting the real home directory slip past this
# guard entirely (security review, PR #1885 fix round).
HOME_REAL="$(realpath "$HOME" 2>/dev/null || printf '%s' "$HOME")"
if [[ "$TARGET_PATH" == "/" ]] || [[ "$TARGET_PATH" == "$HOME_REAL" ]]; then
  echo "ERROR: refusing to target / or \$HOME — pick a project subdirectory" >&2
  exit 1
fi

if [[ "$RESUME" == "true" ]]; then
  echo "=== coldstart-unified: --resume — skipping population, going straight to provisioning's seed step ==="
  exec bash "$COLDSTART_SH" --path "$TARGET_PATH" --name "$PROJECT_NAME" --mode "$MODE" --resume "${EXTRA_COLDSTART_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# --mode new pre-scaffold — measured, not assumed (D#1872): bootstrap.sh
# (population, step 1) hard-refuses a non-git target ("ERROR: ... is not a
# git repository") for ANY mode, including new. The empty-dir git-init
# scaffold only lives in coldstart-project.sh (provisioning, step 2). Left
# alone, population-then-provisioning — the measured correct order for
# everything else — breaks --mode new outright: step 1 fails before step 2
# ever gets a chance to scaffold. Reproduced by running bootstrap.sh
# directly against a fresh empty directory with --mode new intended.
#
# Fix: run the identical scaffold coldstart-project.sh would have run
# (git init + README.md + initial commit), but do it here, first, only when
# the target isn't already a git repo. coldstart-project.sh's own scaffold
# branch only triggers when `git -C "$REPO_ABS" rev-parse --git-dir` fails,
# so once this runs, its step 2 call takes the ordinary "existing repo"
# path — no double-scaffold, no behavior change for --mode existing (which
# never reaches this block) or for a target that's already a git repo.
#
# --dry-run handling: bootstrap.sh's "is a git repository" check is
# unconditional — it fires even when bootstrap.sh itself is invoked with
# --dry-run — so a real call to bootstrap.sh below would still hard-refuse
# an empty non-git target under --dry-run (this script's own doc comment
# above promises --dry-run exits 0 with a preview and no mutations). Rather
# than actually git-init the target during a dry run, NEW_MODE_NEEDS_SCAFFOLD
# records that this combination applies, and step 1 below substitutes a
# printed preview for the real bootstrap.sh invocation in that case only.
NEW_MODE_NEEDS_SCAFFOLD=false
if [[ "$MODE" == "new" ]] && [[ -d "$TARGET_PATH" ]] && ! git -C "$TARGET_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  if [[ -z "$(ls -A "$TARGET_PATH" 2>/dev/null)" ]]; then
    NEW_MODE_NEEDS_SCAFFOLD=true
  fi
  # A populated non-git dir is left alone here too -- bootstrap.sh's own
  # "not a git repository" error is the correct, loud failure for that
  # case, same as coldstart-project.sh's refusal to clobber it.
fi

if [[ "$NEW_MODE_NEEDS_SCAFFOLD" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "=== coldstart-unified: --mode new pre-scaffold (dry-run preview) — '$TARGET_PATH' is empty and not a git repo ==="
    echo "would run: git init; printf '# $PROJECT_NAME' > README.md; git add README.md; git commit -m 'Initial commit'"
    echo ""
  else
    echo "=== coldstart-unified: --mode new pre-scaffold — '$TARGET_PATH' is empty, git-init'ing before population ==="
    git -C "$TARGET_PATH" init -q
    printf '# %s\n' "$PROJECT_NAME" > "$TARGET_PATH/README.md"
    git -C "$TARGET_PATH" add README.md
    git -C "$TARGET_PATH" \
        -c user.email="coldstart@localhost" \
        -c user.name="coldstart" \
        commit -q -m "Initial commit"
    echo ""
  fi
fi

# ---------------------------------------------------------------------------
# Step 1 of 2 — POPULATE (loop-bootstrap/bootstrap.sh)
# ---------------------------------------------------------------------------
if [[ "$NEW_MODE_NEEDS_SCAFFOLD" == "true" && "$DRY_RUN" == "true" ]]; then
  # The pre-scaffold preview above did not actually create a git repo, so a
  # real invocation of bootstrap.sh would still hit its unconditional
  # "not a git repository" refusal. Substitute a preview instead of falling
  # through to that refusal.
  echo "=== coldstart-unified: step 1 of 2 — populate (loop-bootstrap/bootstrap.sh), dry-run preview ==="
  echo "(not invoking bootstrap.sh for real here: its git-repository guard requires the pre-scaffold above to have actually run, which --dry-run does not do)"
  echo "would run: bash $BOOTSTRAP_SH --repo $TARGET_REPO --dry-run $TARGET_PATH"
  echo ""
else
  echo "=== coldstart-unified: step 1 of 2 — populate (loop-bootstrap/bootstrap.sh) ==="
  BOOTSTRAP_ARGS=(--repo "$TARGET_REPO")
  [[ "$DRY_RUN" == "true" ]] && BOOTSTRAP_ARGS+=(--dry-run)
  [[ "$FORCE" == "true" ]] && BOOTSTRAP_ARGS+=(--force)
  BOOTSTRAP_ARGS+=("$TARGET_PATH")
  bash "$BOOTSTRAP_SH" "${BOOTSTRAP_ARGS[@]}"
  echo ""
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "=== coldstart-unified: step 2 of 2 — provision (scripts/coldstart.sh), --dry-run ==="
  bash "$COLDSTART_SH" --path "$TARGET_PATH" --name "$PROJECT_NAME" --mode "$MODE" --dry-run
  echo ""
  echo "coldstart-unified --dry-run: both steps planned, no mutations made."
  exit 0
fi

# ---------------------------------------------------------------------------
# Ordering gate (D#1872 item 2) — population MUST have actually run before
# provisioning does. This is what makes the order enforced by the command,
# not just documented in prose: reverse the two calls above, or delete
# step 1, and this assertion is what fails.
# ---------------------------------------------------------------------------
ORDER_GATE_FAIL=false
if [[ ! -f "$TARGET_PATH/CLAUDE.md" ]]; then
  echo "ERROR: ordering gate failed — $TARGET_PATH/CLAUDE.md is missing after the population step." >&2
  ORDER_GATE_FAIL=true
fi
if [[ ! -d "$TARGET_PATH/.claude/agents" ]] || [[ -z "$(ls -A "$TARGET_PATH/.claude/agents" 2>/dev/null)" ]]; then
  echo "ERROR: ordering gate failed — $TARGET_PATH/.claude/agents is missing or empty after the population step." >&2
  ORDER_GATE_FAIL=true
fi
if [[ ! -d "$TARGET_PATH/backend" ]]; then
  echo "ERROR: ordering gate failed — $TARGET_PATH/backend is missing after the population step." >&2
  ORDER_GATE_FAIL=true
fi
if [[ "$ORDER_GATE_FAIL" == "true" ]]; then
  echo "" >&2
  echo "Population did not complete before provisioning was about to start." >&2
  echo "This is the exact failure D#1872 measured for provision-before-populate:" >&2
  echo "coldstart.sh alone exits 0 but leaves the repo with a state dir and" >&2
  echo "project.json and nothing else — no agents, no CLAUDE.md, no backend/." >&2
  echo "Re-run this script; do not run scripts/coldstart.sh directly first." >&2
  exit 1
fi
echo "=== coldstart-unified: ordering gate passed — CLAUDE.md, .claude/agents/, backend/ all present ==="
echo ""

# ---------------------------------------------------------------------------
# Step 2 of 2 — PROVISION (scripts/coldstart.sh)
# ---------------------------------------------------------------------------
echo "=== coldstart-unified: step 2 of 2 — provision (scripts/coldstart.sh) ==="
COLDSTART_ARGS=(--path "$TARGET_PATH" --name "$PROJECT_NAME" --mode "$MODE" --language "$LANGUAGE")
[[ -n "$BACKLOG" ]] && COLDSTART_ARGS+=(--backlog "$BACKLOG")
COLDSTART_ARGS+=("${EXTRA_COLDSTART_ARGS[@]}")
bash "$COLDSTART_SH" "${COLDSTART_ARGS[@]}"
