#!/usr/bin/env bash
# scripts/coldstart.sh — one-command front door for spinning up a new
# autonomous-forever team on an existing repo.
#
# Usage:
#   bash scripts/coldstart.sh --path <repo-path> --name <project-name> \
#       [--language rust|python|typescript|polyglot] [--backlog <epics-dir>] \
#       [--mode existing|new] [--dry-run] [--phase provision|seed] [--resume]
#   bash scripts/coldstart.sh --self-test
#   bash scripts/coldstart.sh --help
#
# What it does (in order):
#   1. preflight       — check gh/node/python3 are present, gh is authenticated,
#                          and (when the target already has a github.com origin
#                          remote) that the active gh account can actually see
#                          that repo -- a 404 there means "wrong account", not
#                          "repo doesn't exist" (D#2227).
#   2. mechanical wiring — delegates to the existing scripts/coldstart-project.sh
#                          (state dir, symlinks, project.json, dashboard port).
#                          --mode is threaded straight through so --mode new
#                          scaffolds a genuinely empty dir instead of requiring
#                          a pre-existing git checkout.
#   3. dependency install — scripts/bootstrap-deps.sh: npm/bun install for
#                          any present Node component dir (dashboard/,
#                          ts-backend/, tui/) and venv+pip install for the
#                          Python backend. Idempotent, plain-English errors
#                          on failure (D#1637).
#   4. labels          — scripts/bootstrap-github-labels.sh (merge-gate labels).
#   5. sandbox hook     — scripts/install-sandbox-hook.sh.
#   6. engine-apply seam — manifest-gated; see below. Never enumerates engine
#                          files itself (owned by D#1528).
#   7. HALT             — orient -> interview -> deep-tutorial offer (D#1539
#                          Batch W). This is the single seam that wires
#                          D#1538's interview (its deferred "Slice C") AND
#                          the new-vs-existing branch AND the tutorial offer
#                          into one flow, replacing the old blind checklist:
#                            a. orient beat — one-paragraph mental model +
#                               the project_kind (new-vs-existing) answer,
#                               already known from --mode.
#                            b. interview beat — drives
#                               scripts/coldstart-interview/harness.sh one
#                               topic at a time, surfacing each question's
#                               `why` JIT micro-teaching inline.
#                            c. tutorial-offer beat — offers (never
#                               auto-launches) scripts/coldstart-tutorial/
#                               tutor.sh, recommended to run after the
#                               operator's first merge.
#                          Exits 0. The operator fills in the epic backlog,
#                          then re-runs with --resume to seed Discussions.
#   8. seed             — scripts/import-epic-tasks.py (only on --resume or
#                          --phase seed).
#
# Idempotent — every step it delegates to is itself idempotent (see their
# own headers). Safe to re-run at any point.
#
# --dry-run prints the ordered plan and exits 0 WITHOUT touching the target
# path, the state dir, or calling any GitHub API mutation.
#
# --self-test exercises the orient/interview/tutorial-offer flow end-to-end
# against a temp interview session (answers filled from questions.json
# defaults, no stdin reads, no GitHub API calls) and exits 0. Used by CI to
# verify the HALT flow without a human at the keyboard. Does not require
# --path/--name.
#
# --phase provision|seed exists so CI can exercise either half of the
# pipeline offline. It is NOT the newcomer's default front door — the
# default (no --phase) always HALTs for the human-judgment step.
#
# Engine-provisioning-into-a-fresh-repo (copying framework files) is owned
# by D#1528. This script only checks for engine/manifest.json and records
# intent; it never hardcodes a second file list. See D#1526 Spec.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/lib/coldstart-state-root.sh
source "$SCRIPT_DIR/lib/coldstart-state-root.sh"

# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: bash scripts/coldstart.sh --path <repo-path> --name <project-name> [options]

Required:
  --path <repo-path>       Existing local git checkout to wire up.
  --name <project-name>    Project name (used for state dir, ports, labels).

Options:
  --language <lang>        rust | python | typescript | polyglot (default: polyglot)
  --backlog <epics-dir>    Path to the epics/ directory to seed from
                            (default: <repo-path>/epics)
  --mode existing|new      existing: wrap an existing repo (default, back-compat).
                            new: scaffold a brand-new project from an empty dir —
                            reframes the interview questions and skips
                            code-analysis in the orient/interview beats.
  --dry-run                Print the ordered plan and exit 0. No mutations —
                            no files written, no state dir created, no
                            GitHub API calls.
  --self-test               Non-interactive end-to-end check of the
                            orient/interview/tutorial-offer HALT flow (CI).
                            No --path/--name required. Exits 0.
  --phase provision|seed   Run only one half of the pipeline (for CI). Not
                            the default newcomer front door.
  --resume                 Skip mechanical wiring (already done) and go
                            straight to the seed step — this is how you
                            continue after the HALT checkpoint, or after a
                            partial 403-interrupted seed run.
  --help                   Show this message and exit 0.

Ordered pipeline: preflight -> mechanical wiring -> dependency install ->
labels -> sandbox hook -> engine-apply seam -> HALT (orient -> interview ->
tutorial offer) -> seed.

See wiki/Coldstart-Runbook.md for the full walkthrough.
EOF
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

REPO_PATH=""
PROJECT_NAME=""
LANGUAGE="polyglot"
BACKLOG=""
MODE="existing"   # default — back-compat, all prior coldstarts were existing repos
DRY_RUN=0
SELF_TEST=0
PHASE=""
RESUME=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --path)
      REPO_PATH="${2:-}"; shift 2 || { echo "ERROR: --path requires a value" >&2; exit 1; } ;;
    --name)
      PROJECT_NAME="${2:-}"; shift 2 || { echo "ERROR: --name requires a value" >&2; exit 1; } ;;
    --language)
      LANGUAGE="${2:-polyglot}"; shift 2 || { echo "ERROR: --language requires a value" >&2; exit 1; } ;;
    --backlog)
      BACKLOG="${2:-}"; shift 2 || { echo "ERROR: --backlog requires a value" >&2; exit 1; } ;;
    --mode)
      MODE="${2:-existing}"; shift 2 || { echo "ERROR: --mode requires a value" >&2; exit 1; } ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    --self-test)
      SELF_TEST=1; shift ;;
    --phase)
      PHASE="${2:-}"; shift 2 || { echo "ERROR: --phase requires a value" >&2; exit 1; } ;;
    --resume)
      RESUME=1; shift ;;
    *)
      echo "Unknown flag: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$MODE" != "existing" && "$MODE" != "new" ]]; then
  echo "ERROR: --mode must be 'existing' or 'new' (got: $MODE)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# --self-test — exercises the HALT flow (orient/interview/tutorial-offer)
# non-interactively. Dispatched before the --path/--name requirement since
# self-test supplies its own synthetic values and never touches GitHub.
# ---------------------------------------------------------------------------

if [[ "$SELF_TEST" -eq 1 ]]; then
  source "$SCRIPT_DIR/lib/coldstart-halt-flow.sh"
  coldstart_halt_self_test "$MODE"
  exit $?
fi

if [[ -z "$REPO_PATH" || -z "$PROJECT_NAME" ]]; then
  echo "ERROR: --path and --name are required." >&2
  usage >&2
  exit 1
fi

# --name lands unvalidated in a $HOME-rooted path below (STATE_DIR). An
# agent following coldstart.md's instructions fills --name from user text,
# so "../../../tmp/pwn" or "/etc/passwd"-shaped input would otherwise escape
# $HOME entirely. Restrict to a safe filesystem-slug charset before it's
# used in any path.
if [[ ! "$PROJECT_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "ERROR: --name must match ^[A-Za-z0-9._-]+\$ (got: $PROJECT_NAME)" >&2
  exit 1
fi

if [[ -n "$PHASE" && "$PHASE" != "provision" && "$PHASE" != "seed" ]]; then
  echo "ERROR: --phase must be 'provision' or 'seed' (got: $PHASE)" >&2
  exit 1
fi

# Same resolver coldstart-project.sh (step 2) uses, so the two never disagree
# about where this project's state lives. Honours $COLDSTART_STATE_ROOT,
# defaulting to $HOME (D#2317 PR-c).
STATE_DIR="$(coldstart_state_dir "$PROJECT_NAME")"
EPICS_DIR="${BACKLOG:-$REPO_PATH/epics}"

# D#2216: export this NOW, not just compute it locally. coldstart-project.sh
# (mechanical wiring, step 2) already writes this same path into
# .autonomous-team/project.json's "state_dir" field, but nothing exported it
# into the environment for step 7's HALT flow -- so the interview harness
# (scripts/coldstart-interview/harness.sh) fell through to ITS OWN default
# ($HOME/.autonomous-forever-state, or the plugin's rewritten
# $HOME/.<engine-name>-state) instead of this project's. Exporting here
# covers coldstart.sh's own process tree: the mechanical-wiring subprocess
# below, and everything coldstart_halt_flow calls (including the read-loop
# interview path). It does NOT cover an agent's own later, separate shell
# calls made per the interview handoff -- see coldstart-halt-flow.sh's
# _coldstart_halt_interview_handoff for that half of the fix.
export AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR"

# ---------------------------------------------------------------------------
# --dry-run — print the ordered plan, touch nothing, exit 0
# ---------------------------------------------------------------------------

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "=== coldstart.sh --dry-run: $PROJECT_NAME ==="
  echo ""
  echo "Ordered plan (no mutations will be made):"
  echo "  1. preflight            — check gh/node/python3 present, gh authenticated,"
  echo "                            and (real run only) that gh can actually see the"
  echo "                            target repo if it already has a github.com remote"
  echo "  2. mechanical wiring     — scripts/coldstart-project.sh $REPO_PATH $PROJECT_NAME --language $LANGUAGE --mode $MODE"
  echo "  3. dependency install  — scripts/bootstrap-deps.sh (npm/bun install + Python venv+pip)"
  echo "  4. labels                — scripts/bootstrap-github-labels.sh"
  echo "  5. sandbox hook          — scripts/install-sandbox-hook.sh"
  echo "  6. engine-apply seam     — checks engine/manifest.json (manifest-gated, D#1528-owned)"
  echo "  7. HALT                  — orient ($MODE) -> interview -> deep-tutorial offer"
  echo "  8. seed                  — scripts/import-epic-tasks.py $REPO_PATH --repo <owner/name>"
  echo ""
  echo "  target repo path:  $REPO_PATH"
  echo "  project name:      $PROJECT_NAME"
  echo "  language:          $LANGUAGE"
  echo "  mode:               $MODE"
  echo "  would-be state dir: $STATE_DIR"
  echo "  epics/backlog dir: $EPICS_DIR"
  echo ""
  echo "Nothing was written. Re-run without --dry-run to execute."
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------

source "$SCRIPT_DIR/lib/coldstart-preflight.sh"

if [[ "$RESUME" -eq 0 ]]; then
  echo "=== coldstart.sh: preflight ==="
  if ! coldstart_preflight; then
    echo "ERROR: preflight failed. Fix the issues above and re-run." >&2
    exit 1
  fi
  echo ""

  # D#2227: a repo the active `gh` account can't see returns the exact same
  # 404 shape as a repo that genuinely doesn't exist. Left unchecked, that
  # ambiguity survives all the way to the HALT/interview step below, which
  # then has to guess at "does this repo exist" from a wrong premise -- and
  # one of its plausible guesses is offering to CREATE the repo, up to and
  # including publicly. Publishing is irreversible; a wrong active account
  # is not. Check the one thing that actually distinguishes them, before any
  # of that becomes reachable, using the same central assert start-the-day.sh
  # already relies on for the identical ambiguity (D#1787).
  #
  # Only meaningful when the target already has a resolvable github.com
  # origin remote -- a genuinely brand-new project (--mode new, no remote
  # yet) has nothing to check here, and that's the one legitimate case
  # where "the repo doesn't exist yet" is simply true.
  COLDSTART_REPO_SLUG="$(git -C "$REPO_PATH" remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?$#\1#')" || true
  if [[ -n "$COLDSTART_REPO_SLUG" ]]; then
    echo "=== coldstart.sh: repo visibility ==="
    # shellcheck source=scripts/lib/gh-precondition.sh
    source "$SCRIPT_DIR/lib/gh-precondition.sh"
    if ! assert_gh_can_see_repo "$COLDSTART_REPO_SLUG"; then
      {
        echo ""
        echo "ERROR: cannot confirm $COLDSTART_REPO_SLUG's real state from here -- see the"
        echo "diagnosis above. Do NOT work around this by having the interview create the"
        echo "repo (private or public) -- fix the active gh account and re-run instead."
      } >&2
      exit 1
    fi
    echo "[ok] gh account can see $COLDSTART_REPO_SLUG"
    echo ""
  fi
fi

# ---------------------------------------------------------------------------
# seed-only entry points (--resume or --phase seed) skip straight to step 7
# ---------------------------------------------------------------------------

run_seed() {
  echo "=== coldstart.sh: seed ==="
  if [[ ! -d "$EPICS_DIR" ]]; then
    echo "[!] No epics/ backlog dir found at $EPICS_DIR — nothing to seed. Skipping."
    return 0
  fi
  local repo_slug
  # Same set -e/pipefail interaction as the labels step above -- `|| true`
  # so a missing origin remote reaches the explicit ERROR below instead of
  # a bare exit (D#1872 item 19a).
  repo_slug="$(git -C "$REPO_PATH" remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?$#\1#')" || true
  if [[ -z "$repo_slug" ]]; then
    echo "ERROR: could not resolve owner/name from git remote at $REPO_PATH — pass a repo with an origin remote." >&2
    return 1
  fi
  python3 "$REPO_ROOT/scripts/import-epic-tasks.py" "$REPO_PATH" --repo "$repo_slug"
}

if [[ "$RESUME" -eq 1 || "$PHASE" == "seed" ]]; then
  run_seed
  exit $?
fi

# ---------------------------------------------------------------------------
# 2. Mechanical wiring — delegates to the existing, untouched script.
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: mechanical wiring ==="
bash "$REPO_ROOT/scripts/coldstart-project.sh" "$REPO_PATH" "$PROJECT_NAME" --language "$LANGUAGE" --mode "$MODE"
echo ""

# ---------------------------------------------------------------------------
# 2b. Repo-identity guard (D#2226) — project.json's "repo", config.json's
# "repo", and the slug stamped by loop-bootstrap/bootstrap.sh's sed pass into
# the installed agent cards are three independently-written sources for the
# same value. Nothing asserted they agreed before this existed; D#2226 was
# exactly that gap (one of the three silently ending up empty/None while the
# other two looked fine). Cheap to check here, right after the file each
# source lives in is guaranteed to exist.
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: repo-identity guard ==="
PROJECT_JSON_REPO="$(python3 -c "
import json
try:
    print(json.load(open('$REPO_PATH/.autonomous-team/project.json')).get('repo') or '')
except Exception:
    print('')
" 2>/dev/null)"
CONFIG_JSON_REPO="$(python3 -c "
import json
try:
    print(json.load(open('$REPO_PATH/.autonomous-team/config.json')).get('repo') or '')
except Exception:
    print('')
" 2>/dev/null)"
AGENT_CARD="$REPO_PATH/.claude/agents/executor.md"
CARD_REPO=""
if [[ -f "$AGENT_CARD" ]]; then
  CARD_REPO="$(grep -m1 -oE 'ONLY interact with .[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+.' "$AGENT_CARD" 2>/dev/null | grep -oE '[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' || true)"
fi

if [[ -z "$PROJECT_JSON_REPO" && -z "$CONFIG_JSON_REPO" && -z "$CARD_REPO" ]]; then
  echo "[!] WARN: repo-identity guard could not read any of the three sources — skipping (no origin remote and no --repo supplied upstream?)." >&2
elif [[ "$PROJECT_JSON_REPO" == "$CONFIG_JSON_REPO" && "$PROJECT_JSON_REPO" == "$CARD_REPO" ]]; then
  echo "[=] repo identity agrees across project.json, config.json, and agent cards: $PROJECT_JSON_REPO"
else
  echo "ERROR (D#2226 guard): repo identity disagrees across the three sources that must match:" >&2
  echo "         project.json  ('.autonomous-team/project.json'):  ${PROJECT_JSON_REPO:-<empty>}" >&2
  echo "         config.json   ('.autonomous-team/config.json'):   ${CONFIG_JSON_REPO:-<empty>}" >&2
  echo "         agent card    ('.claude/agents/executor.md'):     ${CARD_REPO:-<empty>}" >&2
  echo "       Fix whichever file(s) are wrong or empty before continuing." >&2
  exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# 3. Dependency install — all install logic lives in bootstrap-deps.sh
# (module-per-feature); this hub only calls it (D#1637).
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: dependency install ==="
bash "$REPO_ROOT/scripts/bootstrap-deps.sh" || { echo "ERROR: dependency install failed — see the [bootstrap-deps] error above and re-run." >&2; exit 1; }
echo ""

# ---------------------------------------------------------------------------
# 4. Labels
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: labels ==="
# `|| true` on both the git call and the substitution as a whole: under
# set -euo pipefail, a target repo with no origin remote makes `git remote
# get-url origin` exit non-zero, and pipefail propagates that through the
# `sed` pipe into the command substitution's own exit status -- which trips
# `set -e` and kills the script right here with a bare, uncaused exit 2,
# before the WARN message below ever prints (D#1872 item 19a; reproduced
# and fixed off PR #1880's README walkthrough).
REPO_SLUG_FOR_LABELS="$(git -C "$REPO_PATH" remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?$#\1#')" || true
if [[ -n "$REPO_SLUG_FOR_LABELS" ]]; then
  bash "$REPO_ROOT/scripts/bootstrap-github-labels.sh" --repo "$REPO_SLUG_FOR_LABELS" || \
    echo "[!] WARN: label bootstrap failed — continuing (re-run scripts/bootstrap-github-labels.sh later)." >&2
else
  echo "[!] WARN: no origin remote on $REPO_PATH — skipping label bootstrap. Run scripts/bootstrap-github-labels.sh --repo <owner/name> manually." >&2
fi
echo ""

# ---------------------------------------------------------------------------
# 5. Sandbox hook
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: sandbox hook ==="
bash "$REPO_ROOT/scripts/install-sandbox-hook.sh" || \
  echo "[!] WARN: sandbox hook install failed — continuing (run scripts/install-sandbox-hook.sh manually later)." >&2
echo ""

# ---------------------------------------------------------------------------
# 6. Engine-apply seam — manifest-gated, no hardcoded file list.
#
# D#1528 owns the manifest (single source of truth) and the actual apply
# logic. This step only checks whether a manifest exists on the checkout
# coldstart is running from and records intent — it never enumerates
# engine files (scripts/, hooks/, .claude/agents/) itself.
# ---------------------------------------------------------------------------

echo "=== coldstart.sh: engine-apply seam ==="
if [[ -f "$REPO_ROOT/engine/manifest.json" ]]; then
  echo "[coldstart] engine/manifest.json found on this checkout — engine files will be applied per D#1528's manifest (apply logic owned by D#1528, not enumerated here)."
else
  echo "[coldstart] engine-provisioning deferred to D#1528 (no engine/manifest.json on this checkout)"
fi
echo ""

# ---------------------------------------------------------------------------
# 7. HALT — orient -> interview -> deep-tutorial offer
#
# Replaces the old blind checklist-and-exit HALT. This single seam wires in
# D#1538's interview (its deferred "Slice C") via coldstart-interview/
# harness.sh, the new-vs-existing orient beat, and the tutorial offer via
# coldstart-tutorial/tutor.sh. See scripts/lib/coldstart-halt-flow.sh for the
# actual beat implementations (module-per-feature: this hub only sequences).
# ---------------------------------------------------------------------------

source "$SCRIPT_DIR/lib/coldstart-halt-flow.sh"
coldstart_halt_flow "$REPO_PATH" "$PROJECT_NAME" "$MODE"

cat <<EOF

What's still left is the initial epic backlog: fill in
epics/epic-<N>-<slug>/epic.md files (and task files under them) describing
the first bodies of work. A fill-in-the-blank template:

    # Epic <N>: <short title>

    ## Goal
    <one paragraph: what does this epic accomplish?>

    ## Why now
    <one paragraph: why is this the first thing to build?>

    ## Scope
    - <bullet: in scope>
    - <bullet: in scope>

    ## Out of scope
    - <bullet: explicitly deferred>

Once the backlog exists under $EPICS_DIR, re-run:

    bash scripts/coldstart.sh --path $REPO_PATH --name $PROJECT_NAME --resume

to seed GitHub Discussions from it. Re-run to continue is always safe —
seeding is idempotent (existing Discussion titles are skipped) and resumes
from .autonomous-team/pending-imports.json if a prior run was rate-limited.
EOF

exit 0
