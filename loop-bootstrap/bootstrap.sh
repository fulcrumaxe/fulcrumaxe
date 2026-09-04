#!/usr/bin/env bash
# bootstrap.sh — install the autonomous loop kit into a target repo
#
# Usage:
#   bash loop-bootstrap/bootstrap.sh --repo OWNER/NAME /path/to/target-repo
#   bash loop-bootstrap/bootstrap.sh --repo OWNER/NAME --dry-run /path/to/target-repo
#   bash loop-bootstrap/bootstrap.sh --repo OWNER/NAME --force /path/to/target-repo
#
# Idempotent: re-running on an already-bootstrapped repo produces no diff.
#
# Flags:
#   --repo OWNER/NAME   Target repo identifier — sed-replaces source-repo
#                        identifier in copied agent/template/script files.
#                        REQUIRED to avoid agents pointing at the wrong repo.
#   --dry-run           Print planned actions without writing.
#   --force             Allow overwrite of populated .claude/agents/ in target.
#   --simulate-missing <relative-path>
#                        TEST-ONLY. After install, removes <relative-path>
#                        from the target so the post-install import-smoke
#                        test below can be exercised against a deliberately
#                        broken install (D#1890 Spec §1.8). Never use this
#                        in a real bootstrap.
#
# After install:
#   cd <target> && claude
#   /start-the-day   ← runs scripts/start-the-day.sh and drives the day
#
# --- D#1890: derived, not hand-maintained -----------------------------------
# The payload this script installs (backend/, scripts/, hooks/,
# .claude/agents/*.md, .claude/commands/*.md, requirements.txt* — see
# open-source/MANIFEST.md's BOOTSTRAP_PATHS block) is derived directly from
# the live tree at install time, not from a second hand-frozen copy living
# under loop-bootstrap/. A hand-frozen copy is what this repo shipped for
# three months without anyone noticing 79 of 239 backend/*.py files had
# drifted and the shipped engine's server.py imported a package that had
# already been archived. A file that isn't a second copy can't drift from
# the first. See the Discussion for the full case.
#
# Three files remain hand-maintained on purpose, not by oversight: they are
# DELIBERATE project-agnostic variants of files that also exist live, not
# stale copies of them — scripts/start-dashboard.sh, scripts/merge-and-hook.sh,
# and scripts/start-the-day.sh each self-describe in their own header as
# reading identity from .autonomous-team/project.json where the live copy at
# the same path hardcodes this repo's own values. Every other loop-bootstrap/
# file that used to duplicate a live path was archived — see
# archive/loop-bootstrap-snapshot-2026-08-17/README.md and
# open-source/bootstrap-classification.md for the per-file classification.
# ------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# scripts/ ships as a whole-directory BOOTSTRAP_PATHS entry (see
# open-source/MANIFEST.md), so this resolves the same way in an engine
# clone and in an exported/plugin-installed tree, where open-source/ is
# absent but scripts/ still sits alongside loop-bootstrap/. Fail loudly if
# it's missing rather than silently falling back to a GNU-only sed call.
PLATFORM_COMPAT_LIB="$REPO_ROOT/scripts/lib/platform-compat.sh"
if [[ ! -f "$PLATFORM_COMPAT_LIB" ]]; then
  echo "ERROR: missing $PLATFORM_COMPAT_LIB — bootstrap cannot run without its platform-compat helpers" >&2
  exit 1
fi
# shellcheck source=../scripts/lib/platform-compat.sh
source "$PLATFORM_COMPAT_LIB"

MANIFEST_FILE="$REPO_ROOT/open-source/MANIFEST.md"
MANIFEST_PATHS_LIB="$REPO_ROOT/open-source/lib/manifest_paths.sh"
RSYNC_EXCLUDES_LIB="$REPO_ROOT/open-source/lib/rsync-excludes.sh"
# open-source/ never ships (it's MANIFEST.md's own exclusion, defended by
# verify-export.sh's check_excluded_dir "open-source") -- BOOTSTRAP_PATHS
# and RSYNC_EXCLUDES below are resolved from it when it's present (an
# engine clone) or from this generated, comment-free data file that
# export.sh bakes into an exported/plugin-installed loop-bootstrap/ when
# it isn't. See the resolution block right before BOOTSTRAP_PATHS is used.
GENERATED_PATHS_FILE="$SCRIPT_DIR/bootstrap-paths.generated"

# Source repo identifier — the literal string rewrite_tree_identifiers's sed
# searches for and replaces with --repo's value in every installed file.
#
# This MUST equal whatever slug is actually embedded in the BOOTSTRAP_PATHS
# corpus (backend/, scripts/, hooks/, agents/, commands/) today — not "the
# current correct repo identity". Those two sound the same but aren't: large
# parts of the corpus still literally carry the pre-rename
# "fulcrumaxe/fulcrumaxe" slug (it was never migrated to
# "fulcrumaxe/fulcrumaxe" file-by-file — see D#1893, filed by D#1890
# PR 1 for exactly this gap). Resolving this value from
# .autonomous-team/project.json instead — which correctly holds the *current*
# slug — silently breaks the rewrite for the whole corpus: the sed search key
# no longer matches anything, so every installed file ships with the stale
# slug untouched. Confirmed by running tests/test_loop_bootstrap_extended.sh's
# PORTABILITY assertions against that approach before reverting it.
#
# LOOP_BOOTSTRAP_SOURCE_REPO overrides this for tests only. In an exported
# tree (not this checkout), open-source/export.sh's identifier pass has
# already rewritten both this literal and the corpus's matching literal to
# the same export placeholder (IDENTIFIER-RULES.txt), which keeps the sed
# self-consistent there too — see item 8's amendment on D#1872.
#
# tests/test_loop_bootstrap_extended.sh's "SOURCE_REPO drift" check fails
# loudly if this literal and the corpus's actual embedded slug ever
# diverge (e.g. after a future rename touches one but not the other) —
# that's the check this Discussion's item 8 asks for in place of silent
# staleness.
SOURCE_REPO="${LOOP_BOOTSTRAP_SOURCE_REPO:-fulcrumaxe/fulcrumaxe}"

DRY_RUN=false
FORCE=false
TARGET=""
TARGET_REPO=""
SIMULATE_MISSING=""

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    --simulate-missing)
      SIMULATE_MISSING="${2:-}"; shift 2 || { echo "ERROR: --simulate-missing requires a relative path" >&2; exit 1; } ;;
    --repo)
      TARGET_REPO="${2:-}"; shift 2 || { echo "ERROR: --repo requires OWNER/NAME" >&2; exit 1; } ;;
    -*) echo "Unknown flag: $1" >&2; exit 1 ;;
    *) TARGET="$1"; shift ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "Usage: bash loop-bootstrap/bootstrap.sh --repo OWNER/NAME [--dry-run] [--force] /path/to/target-repo" >&2
  exit 1
fi

if [[ -z "$TARGET_REPO" ]]; then
  echo "ERROR: --repo OWNER/NAME is required (used to rewrite source-repo identifier in installed files)" >&2
  exit 1
fi

# Validate --repo format
if [[ ! "$TARGET_REPO" =~ ^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "ERROR: --repo must be OWNER/NAME (e.g. acme/my-app), got: $TARGET_REPO" >&2
  exit 1
fi

# Validate --simulate-missing stays inside TARGET. ${VAR:?} at the rm -rf call
# site below only guards against an EMPTY value — it does nothing against a
# value that escapes TARGET via `..` or an absolute path, and this value ends
# up directly in an `rm -rf "$TARGET/$SIMULATE_MISSING"`. Rejecting `..`
# segments and absolute paths here means that call site can only ever delete
# something under TARGET, not something the caller pointed it at outside it.
if [[ -n "$SIMULATE_MISSING" ]]; then
  if [[ "$SIMULATE_MISSING" == /* ]] || [[ "$SIMULATE_MISSING" == *".."* ]]; then
    echo "ERROR: --simulate-missing must be a relative path inside the target, with no '..' segments — got: $SIMULATE_MISSING" >&2
    exit 1
  fi
fi

TARGET="$(realpath "$TARGET")"

# Refuse dangerous targets. Resolve $HOME too, not just the target — a
# symlinked $HOME (NixOS, impermanence, some corp NFS layouts) would
# otherwise never compare equal to the realpath'd target, letting the real
# home directory slip past this guard entirely (security review, PR #1885
# fix round).
HOME_REAL="$(realpath "$HOME" 2>/dev/null || printf '%s' "$HOME")"
if [[ "$TARGET" == "/" ]] || [[ "$TARGET" == "$HOME_REAL" ]]; then
  echo "ERROR: refusing to bootstrap into / or \$HOME — pick a project subdirectory" >&2
  exit 1
fi

# Verify target is a git repo
if ! git -C "$TARGET" rev-parse --git-dir > /dev/null 2>&1; then
  echo "ERROR: $TARGET is not a git repository" >&2
  exit 1
fi

# Platform preflight (D#2263) — refuse here, before this script writes
# anything to $TARGET, if a utility bootstrap depends on later (today: an
# in-place sed) is missing or behaves in a way platform-compat.sh's shims
# can't drive. Without this, a host whose sed can't do what
# rewrite_tree_identifiers needs would fail at that step instead — by
# which point rsync_bootstrap_dir() has already populated $TARGET with a
# fully working-looking repo still pointing at $SOURCE_REPO.
if ! pc_preflight; then
  echo "ERROR: platform preflight failed — see above. $TARGET was not touched." >&2
  exit 1
fi

# .claude/agents/ install mode:
#   Default: skip individual agent files that already exist in the target (project-local overrides preserved).
#   --force:  overwrite all agent files unconditionally.

# Derive the memory destination path from the target's absolute path
# /home/user/my-project → -home-user-my-project
TARGET_SLUG=$(echo "$TARGET" | sed 's|^/||; s|/|-|g')
MEMORY_DEST="$TARGET/.claude/projects/-${TARGET_SLUG}/memory"

do_install() {
  local src="$1"
  local dst_dir="$2"
  local dst="$dst_dir/$(basename "$src")"

  # Skip symlinks (CWE-61 — don't dereference into source bundle)
  if [[ -L "$src" ]]; then
    echo "[skip] not copying symlink: $src"
    return
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would copy: $src → $dst"
    return
  fi

  mkdir -p "$dst_dir"
  cp -P "$src" "$dst"
  # cp propagates the source file's mode bits, including a read-only source
  # (a verify_tree_build tree, a Nix store path, a restrictive CI checkout).
  # Restore owner-write on the copy without touching the executable bit.
  chmod u+w "$dst"
}

# List, but do not install, agent files with an upstream update the target
# already has a (differing) local copy of — collected into AGENT_UPSTREAM_UPDATES
# and reported once at the end instead of a silent per-file skip line
# (D#1890 Spec §4.1 — product-owner's finding: the old per-file
# "[=] agent file already exists, skipping" line gave an adopter re-running
# bootstrap monthly zero visibility into 12 months of agent improvements
# they never received).
AGENT_UPSTREAM_UPDATES=()

do_install_agent() {
  # Like do_install, but skips the file if it already exists in dst_dir (unless FORCE=true).
  # Used for .claude/agents/ to preserve project-local customisations across re-bootstraps.
  local src="$1"
  local dst_dir="$2"
  local dst="$dst_dir/$(basename "$src")"

  if [[ "$FORCE" != "true" ]] && [[ -f "$dst" ]]; then
    if ! cmp -s "$src" "$dst"; then
      AGENT_UPSTREAM_UPDATES+=("$(basename "$src")")
    fi
    return
  fi
  do_install "$src" "$dst_dir"
}

do_install_dir() {
  local src_dir="$1"
  local dst_dir="$2"

  for f in "$src_dir"/*; do
    [[ -f "$f" ]] || continue
    do_install "$f" "$dst_dir"
  done
}

# Rsync a BOOTSTRAP_PATHS directory entry from the live tree into the
# target, inheriting the same excludes the public export uses
# (open-source/lib/rsync-excludes.sh) plus any entry-scoped extras passed
# in $3+ (e.g. the deliberate-variant scripts that must come from
# loop-bootstrap/ instead of live, and scripts/memory-triage/, which is not
# yet safe to derive wholesale — see the comment at its call site).
rsync_bootstrap_dir() {
  local src_dir="$1"
  local dst_dir="$2"
  shift 2
  local extra_excludes=("$@")

  if [[ ! -d "$src_dir" ]]; then
    echo "warn: source dir missing, skipping: $src_dir" >&2
    return
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    local n
    n=$(find "$src_dir" -type f | wc -l)
    echo "[dry-run] would rsync ~${n} file(s) → $dst_dir/"
    return
  fi

  mkdir -p "$dst_dir"
  # --chmod=Fu+w: rsync -a preserves source file modes (same propagation
  # issue as cp -P in do_install above) — force owner-write on every copied
  # file without touching the executable bit or directory modes.
  rsync -a --chmod=Fu+w "${RSYNC_EXCLUDES[@]}" "${extra_excludes[@]}" "$src_dir/" "$dst_dir/"
}

# Apply the source→target repo-identifier rewrite to every text file under
# *dir*. This is the step that closes the "bulk rsync bypasses the rewrite"
# defect D#1890 documents: rsync_bootstrap_dir() above copies whole
# directory trees in one shot, so a per-file rewrite hook (the old
# do_install()'s inline sed calls) never sees most of the payload. Running
# once, post-copy, over every directory bootstrap actually wrote to closes
# that gap regardless of which install path a file came through.
#
# This is still the 4-pattern sed rewrite that predates this PR, not the
# full 9-rule IDENTIFIER-RULES.txt table — unifying onto that table is
# D#1890 PR 2 (security-expert's S3, promoted to a precondition for a
# gate-clean payload, not required for PR 1 to land — see Implementation
# Notes: "Do not try to make PR 1 gate-clean on its own"). PR 1's exit bar
# is a strictly lower unallowlisted-hit count than today's loop-bootstrap/,
# not a clean gate.
#
# The rewrite is content-gated now, not extension-gated (D#2207): `grep -Iq`
# (skip binaries) is the real safety check, so do not re-add an extension
# allowlist here as a "safety" improvement — it only re-opens the gap it closed.
rewrite_tree_identifiers() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  local target_owner="${TARGET_REPO%/*}"
  local target_name="${TARGET_REPO#*/}"
  local source_owner="${SOURCE_REPO%/*}"
  local source_name="${SOURCE_REPO#*/}"
  local f
  while IFS= read -r -d '' f; do
    [[ -L "$f" ]] && continue
    grep -Iq . "$f" 2>/dev/null || continue  # skip binaries
    pc_sed_i "s|${SOURCE_REPO}|${TARGET_REPO}|g" "$f"
    pc_sed_i "s|owner:\"${source_owner}\"|owner:\"${target_owner}\"|g" "$f"
    pc_sed_i "s|name:\"${source_name}\"|name:\"${target_name}\"|g" "$f"
    pc_sed_i "s|owner:\\\\\"${source_owner}\\\\\"|owner:\\\\\"${target_owner}\\\\\"|g" "$f"
    pc_sed_i "s|name:\\\\\"${source_name}\\\\\"|name:\\\\\"${target_name}\\\\\"|g" "$f"
    pc_sed_i "s|owner: \"${source_owner}\"|owner: \"${target_owner}\"|g" "$f"
    pc_sed_i "s|name: \"${source_name}\"|name: \"${target_name}\"|g" "$f"
  done < <(find "$dir" -type f -print0 2>/dev/null)
}

echo "Bootstrapping autonomous loop into: $TARGET"
[[ "$DRY_RUN" == "true" ]] && echo "(dry-run mode — no files written)"

# Resolve BOOTSTRAP_PATHS and RSYNC_EXCLUDES. Two sources, tried in order:
#
#   1. open-source/MANIFEST.md + its lib/*.sh helpers — the single source of
#      truth for what this script installs (D#1890 §1.3), read when this
#      script is running from an engine clone that has open-source/
#      alongside it. Unchanged behavior from before this fallback existed.
#      A strict subset of the public export's PATHS block; checked by
#      open-source/checks/bootstrap_subset.py.
#   2. bootstrap-paths.generated, sitting next to this script — read when
#      open-source/ isn't there (an open-source export, or a plugin
#      installed from one: open-source/ is never exported, see
#      MANIFEST.md's own exclusion list). export.sh bakes the same two
#      arrays into this one data file with the explanatory comments
#      stripped, since MANIFEST.md/rsync-excludes.sh's rationale for what
#      the export withholds is itself internal-only and must not ship —
#      only the resolved path/pattern values do.
BOOTSTRAP_PATHS=()
RSYNC_EXCLUDES=()
if [[ -f "$MANIFEST_FILE" && -f "$MANIFEST_PATHS_LIB" && -f "$RSYNC_EXCLUDES_LIB" ]]; then
  # shellcheck source=open-source/lib/manifest_paths.sh
  source "$MANIFEST_PATHS_LIB"
  # shellcheck source=open-source/lib/rsync-excludes.sh
  source "$RSYNC_EXCLUDES_LIB"
  while IFS= read -r line; do
    BOOTSTRAP_PATHS+=("$line")
  done < <(manifest_paths BOOTSTRAP_PATHS "$MANIFEST_FILE")
elif [[ -f "$GENERATED_PATHS_FILE" ]]; then
  # Format: BOOTSTRAP_PATHS entries, one per line, then a bare "===" line,
  # then RSYNC_EXCLUDES patterns (without the --exclude= prefix), one per
  # line. Pure data, no comments -- see export.sh's generation step.
  IN_EXCLUDES=false
  while IFS= read -r line; do
    if [[ "$line" == "===" ]]; then
      IN_EXCLUDES=true
      continue
    fi
    if [[ "$IN_EXCLUDES" == "true" ]]; then
      RSYNC_EXCLUDES+=("--exclude=$line")
    else
      BOOTSTRAP_PATHS+=("$line")
    fi
  done < "$GENERATED_PATHS_FILE"
fi

if [[ "${#BOOTSTRAP_PATHS[@]}" -eq 0 ]]; then
  echo "ERROR: could not resolve BOOTSTRAP_PATHS from either source:" >&2
  echo "         $MANIFEST_FILE (+ open-source/lib/*.sh) -- engine clone, or" >&2
  echo "         $GENERATED_PATHS_FILE -- open-source export / installed plugin" >&2
  echo "       Neither is present and populated in this tree. Refusing to install an empty payload." >&2
  exit 1
fi

# Symmetric with the BOOTSTRAP_PATHS check above: a GENERATED_PATHS_FILE
# truncated before its "===" marker would leave every line read as a
# BOOTSTRAP_PATHS entry and RSYNC_EXCLUDES silently empty -- the
# defense-in-depth rsync layer (*.env*, node_modules/, .venv/, and the
# rest) disappearing with no error, not a loud one.
if [[ "${#RSYNC_EXCLUDES[@]}" -eq 0 ]]; then
  echo "ERROR: could not resolve RSYNC_EXCLUDES from either source:" >&2
  echo "         $MANIFEST_FILE (+ open-source/lib/*.sh) -- engine clone, or" >&2
  echo "         $GENERATED_PATHS_FILE -- open-source export / installed plugin" >&2
  echo "       Neither is present and populated in this tree. Refusing to rsync with no excludes." >&2
  exit 1
fi

# 1. Memories — NOT YET derived from scripts/memory-triage/ by tier. That
#    migration (mirroring what open-source/export.sh already does) is
#    D#1890 PR 2 scope (§2.6): scripts/memory-triage/ carries tier:project
#    memories (internal GPU-rental/training-initiative notes) that must be
#    pruned before shipping, and that prune pass isn't wired into this
#    script yet. Installing from the old hand-picked loop-bootstrap/memories/
#    list here, unchanged, is intentional — it is why BOOTSTRAP_PATHS above
#    does not list memories/scripts/memory-triage at all in this PR.
echo ""
echo "==> memories → $MEMORY_DEST"
do_install_dir "$SCRIPT_DIR/memories" "$MEMORY_DEST"

# 2. BOOTSTRAP_PATHS-derived paths: backend/, scripts/, hooks/,
#    .claude/agents/*.md, .claude/commands/*.md, requirements.txt.
for entry in "${BOOTSTRAP_PATHS[@]}"; do
  case "$entry" in
    */\*.md)
      glob_dir="${entry%/*.md}"
      src_dir="$REPO_ROOT/$glob_dir"
      dest_dir="$TARGET/$glob_dir"
      echo ""
      echo "==> $glob_dir → $dest_dir"
      if [[ ! -d "$src_dir" ]]; then
        echo "warn: source dir missing, skipping: $glob_dir" >&2
        continue
      fi
      if [[ "$DRY_RUN" == "true" ]]; then
        echo "[dry-run] would copy $glob_dir/*.md → $dest_dir/"
        continue
      fi
      mkdir -p "$dest_dir"
      shopt -s nullglob
      for f in "$src_dir"/*.md; do
        if [[ "$glob_dir" == ".claude/agents" ]]; then
          do_install_agent "$f" "$dest_dir"
        else
          do_install "$f" "$dest_dir"
        fi
      done
      shopt -u nullglob
      ;;
    */)
      echo ""
      echo "==> $entry → $TARGET/$entry"
      case "$entry" in
        scripts/)
          # scripts/ carries things that must NOT come from the live
          # tree wholesale:
          #   - the three deliberate project-agnostic variants (they have
          #     their own install step below, from loop-bootstrap/ — a live
          #     rsync here would silently overwrite them with this repo's
          #     own hardcoded-identity copies, the exact bug D#1889 exists
          #     because of);
          #   - memory-triage/, which carries tier:project memories with no
          #     prune pass wired in yet (see step 1's comment) — shipping
          #     it unfiltered here would leak internal-only notes that
          #     open-source/export.sh already knows to prune. Deferred to
          #     PR 2 alongside the memories/ migration above.
          #   - coldstart-unified.sh, which hard-depends on
          #     loop-bootstrap/bootstrap.sh by relative path. loop-bootstrap/
          #     is deliberately not part of BOOTSTRAP_PATHS above (this
          #     script doesn't install itself), so a bootstrapped target
          #     never gets loop-bootstrap/bootstrap.sh alongside a copied
          #     coldstart-unified.sh — the script would ship broken there
          #     (D#2194). The open-source export ships it fine now that
          #     loop-bootstrap/ is itself export-listed; this exclude is
          #     scoped to this consumer only, not open-source/export.sh's
          #     shared open-source/lib/rsync-excludes.sh list.
          rsync_bootstrap_dir "$REPO_ROOT/$entry" "$TARGET/$entry" \
            --exclude='start-dashboard.sh' \
            --exclude='merge-and-hook.sh' \
            --exclude='start-the-day.sh' \
            --exclude='memory-triage/' \
            --exclude='/coldstart-unified.sh'
          ;;
        *)
          rsync_bootstrap_dir "$REPO_ROOT/$entry" "$TARGET/$entry"
          ;;
      esac
      ;;
    *)
      # Single file entry. requirements.txt is handled separately, further
      # down, from loop-bootstrap/templates/requirements.txt.template — that
      # template is a deliberately curated, adopter-facing SUBSET of this
      # repo's own CI-driven requirements.txt (which pins internal-only
      # dependencies like the FastAPI strangler-fig stack and structlog that
      # an adopter's minimal install doesn't need). Listing requirements.txt
      # in BOOTSTRAP_PATHS documents that it is, in principle, a subset of
      # what the export ships (checked by bootstrap_subset.py); it does not
      # obligate this script to derive its exact content from the live
      # root file the same way backend/ or scripts/ are derived. See the PR
      # description for the full reasoning.
      if [[ "$entry" == "requirements.txt" ]]; then
        continue
      fi
      echo ""
      echo "==> $entry → $TARGET/$entry"
      src_file="$REPO_ROOT/$entry"
      if [[ ! -f "$src_file" ]]; then
        echo "warn: source file missing, skipping: $entry" >&2
        continue
      fi
      if [[ "$DRY_RUN" == "true" ]]; then
        echo "[dry-run] would copy: $entry → $TARGET/$entry"
        continue
      fi
      mkdir -p "$(dirname "$TARGET/$entry")"
      cp -P "$src_file" "$TARGET/$entry"
      chmod u+w "$TARGET/$entry"
      ;;
  esac
done

if [[ "${#AGENT_UPSTREAM_UPDATES[@]}" -gt 0 ]]; then
  echo ""
  echo "${#AGENT_UPSTREAM_UPDATES[@]} agent definition(s) have upstream updates you are not receiving:"
  for a in "${AGENT_UPSTREAM_UPDATES[@]}"; do
    echo "    $a"
  done
  echo "  Review:  diff $TARGET/.claude/agents/<file> $REPO_ROOT/.claude/agents/<file>"
  echo "  Accept:  bash loop-bootstrap/bootstrap.sh --force --repo $TARGET_REPO $TARGET"
  echo "           (--force also overwrites any local edits to those files)"
  echo "  CLAUDE.md: never updated by bootstrap after first install — diff"
  echo "             loop-bootstrap/team-lead-protocol.md yourself after upgrades."
fi

# 3. The deliberate project-agnostic variants — hand-maintained on purpose,
#    not stale copies (see the module docstring above and
#    open-source/bootstrap-classification.md). Installed unconditionally,
#    same as before this PR.
echo ""
echo "==> project-agnostic scripts → $TARGET/scripts"
for f in "$SCRIPT_DIR/scripts/start-dashboard.sh" "$SCRIPT_DIR/scripts/merge-and-hook.sh" "$SCRIPT_DIR/scripts/start-the-day.sh"; do
  [[ -f "$f" ]] || continue
  do_install "$f" "$TARGET/scripts"
  if [[ "$DRY_RUN" != "true" ]]; then
    chmod +x "$TARGET/scripts/$(basename "$f")" 2>/dev/null || true
  fi
done

# 4. Bootstrap-only residue scripts with no live counterpart at all.
echo ""
echo "==> bootstrap-only scripts → $TARGET/scripts"
for f in "$SCRIPT_DIR/scripts/generate-initial-plan.py" "$SCRIPT_DIR/scripts/setup-deps.sh"; do
  [[ -f "$f" ]] || continue
  do_install "$f" "$TARGET/scripts"
  if [[ "$DRY_RUN" != "true" && "$f" == *.sh ]]; then
    chmod +x "$TARGET/scripts/$(basename "$f")" 2>/dev/null || true
  fi
done

# 4b. PLAN-template.md → backend/spawn_templates/ (D#2218).
#     generate-initial-plan.py's load_template() checks
#     backend/spawn_templates/PLAN-template.md as its "installed target"
#     candidate — that comment has existed since the function was written,
#     but nothing ever actually copied the file there. loop-bootstrap/ as a
#     whole is never installed into the target (it's a PATHS carve-out — see
#     open-source/MANIFEST.md's "Carved out relative to PATHS" note), so this
#     template needs its own explicit copy step here, the same as the two
#     residue scripts just above, rather than living inside BOOTSTRAP_PATHS's
#     derived scripts/ tree. Without it, load_template() used to fall back to
#     a minimal inline template missing the placeholder comments render_plan()
#     substitutes into — every Discussion the generator fetched got silently
#     discarded even though the run reported success.
echo ""
echo "==> PLAN-template.md → $TARGET/backend/spawn_templates"
if [[ -f "$SCRIPT_DIR/templates/PLAN-template.md" ]]; then
  do_install "$SCRIPT_DIR/templates/PLAN-template.md" "$TARGET/backend/spawn_templates"
else
  echo "WARNING: $SCRIPT_DIR/templates/PLAN-template.md not found on this checkout — generate-initial-plan.py will hard-fail on the target without it." >&2
fi

# 5. Minimal CLAUDE.md if absent
if [[ ! -f "$TARGET/CLAUDE.md" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would create: $TARGET/CLAUDE.md (minimal stub)"
  else
    cat > "$TARGET/CLAUDE.md" << 'CLAUDE_STUB'
# CLAUDE.md

This repo uses the autonomous development loop.

## Build Commands

```bash
# Add your project-specific build/test commands here
```

## Architecture

_Describe your project structure here._
CLAUDE_STUB
    echo "Created minimal CLAUDE.md in $TARGET"
  fi
else
  echo "CLAUDE.md already exists in $TARGET — skipping"
fi

# 6. Append Team Lead protocol stub to CLAUDE.md (idempotent via marker)
TL_PROTOCOL_SRC="$SCRIPT_DIR/team-lead-protocol.md"
TL_START_MARKER="<!-- LOOP_BOOTSTRAP_TEAM_LEAD_PROTOCOL_START -->"
if [[ -f "$TL_PROTOCOL_SRC" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    if grep -q "$TL_START_MARKER" "$TARGET/CLAUDE.md" 2>/dev/null; then
      echo "[dry-run] team-lead-protocol.md: already appended to CLAUDE.md — skipping"
    else
      echo "[dry-run] would append team-lead-protocol.md to $TARGET/CLAUDE.md"
    fi
  else
    if grep -q "$TL_START_MARKER" "$TARGET/CLAUDE.md" 2>/dev/null; then
      echo "team-lead-protocol.md: already present in CLAUDE.md — skipping"
    else
      echo "" >> "$TARGET/CLAUDE.md"
      # Apply repo rewrite to the content before appending.
      PROTOCOL_CONTENT=$(sed "s|${SOURCE_REPO}|${TARGET_REPO}|g" "$TL_PROTOCOL_SRC")
      printf '%s\n' "$PROTOCOL_CONTENT" >> "$TARGET/CLAUDE.md"
      echo "Appended team-lead-protocol.md to $TARGET/CLAUDE.md"
    fi
  fi
fi

# 7. Slash command scaffolding note: .claude/commands/*.md was already
#    installed by the BOOTSTRAP_PATHS loop above (step 2).

# 8. Create loop-metrics.jsonl placeholder.
#     start-the-day.sh warns "missing" when this file is absent; it must exist
#     even if empty so the freshness check can produce a meaningful age readout
#     after the first real /loop iteration writes a row.
LOOP_METRICS_TARGET="$TARGET/.autonomous-team/loop-metrics.jsonl"
if [[ ! -f "$LOOP_METRICS_TARGET" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would create placeholder: $LOOP_METRICS_TARGET"
  else
    mkdir -p "$(dirname "$LOOP_METRICS_TARGET")"
    touch "$LOOP_METRICS_TARGET"
    echo "Created placeholder: $LOOP_METRICS_TARGET"
  fi
else
  echo "[=] loop-metrics.jsonl already exists: $LOOP_METRICS_TARGET"
fi

# 9. Register SubagentStop hook in $TARGET/.claude/settings.json (project-local).
#     The hook is required for real verdict/duration telemetry in stats.duckdb.
#     Without it, every agent_run row lands with verdict=unknown and duration≈0.6s.
#     Uses the same idempotent JSON-merge pattern as step 11 (PreToolUse sandbox hook).
#
#     D#2232: this used to wire post-agent-hook.sh directly, which is
#     argument-driven (--role/--verdict) and exits 1 on the no-args/stdin-JSON
#     payload Claude Code actually sends for SubagentStop -- so telemetry never
#     ran for any adopter. The correct target is subagent-stop-hook.sh, the
#     stdin-JSON adapter that parses the payload and then calls
#     post-agent-hook.sh with the arguments it requires.
#
#     Because this drifted silently, an already-bootstrapped project may still
#     have the stale post-agent-hook.sh command sitting in its SubagentStop
#     array. The merge below removes any entry that references
#     post-agent-hook.sh (matched by path suffix, so both the
#     $CLAUDE_PROJECT_DIR and absolute-path spellings are caught) before
#     adding the adapter entry, so migrating a broken project converges on
#     exactly one telemetry entry instead of appending a second one next to
#     the stale one. Only hooks referencing that suffix are touched --
#     install-sandbox-hook.sh's own SubagentStop entry
#     (hooks/subagent_stop_dial_audit.py) lives in the same array and is left
#     alone either way.
SETTINGS_FILE_STOP="$TARGET/.claude/settings.json"
STOP_HOOK_CMD='bash "$CLAUDE_PROJECT_DIR/scripts/subagent-stop-hook.sh"'
STOP_HOOK_STALE_SUFFIX='/scripts/post-agent-hook.sh'
STOP_HOOK_ADAPTER_SUFFIX='/scripts/subagent-stop-hook.sh'
if [[ "$DRY_RUN" == "true" ]]; then
  STOP_STATUS=$(python3 -c "
import json, sys

STALE_SUFFIX = '$STOP_HOOK_STALE_SUFFIX'
ADAPTER_SUFFIX = '$STOP_HOOK_ADAPTER_SUFFIX'

def references(cmd, suffix):
    if not isinstance(cmd, str):
        return False
    return cmd.rstrip('\"').rstrip(\"'\").endswith(suffix)

try:
    s = json.load(open('$SETTINGS_FILE_STOP'))
except Exception:
    s = {}

has_stale = False
has_adapter = False
for e in s.get('hooks', {}).get('SubagentStop', []) or []:
    if not isinstance(e, dict):
        continue
    for h in e.get('hooks', []) or []:
        if not isinstance(h, dict):
            continue
        cmd = h.get('command', '')
        if references(cmd, STALE_SUFFIX):
            has_stale = True
        if references(cmd, ADAPTER_SUFFIX):
            has_adapter = True

if has_stale:
    print('MIGRATE')
elif has_adapter:
    print('PRESENT')
else:
    print('ADD')
" 2>/dev/null)
  case "$STOP_STATUS" in
    PRESENT)
      echo "[dry-run] .claude/settings.json: SubagentStop hook already registered — skipping"
      ;;
    MIGRATE)
      echo "[dry-run] would replace stale post-agent-hook.sh SubagentStop entry with:"
      echo "[dry-run]   command: $STOP_HOOK_CMD"
      ;;
    *)
      echo "[dry-run] would register SubagentStop hook in $SETTINGS_FILE_STOP"
      echo "[dry-run]   command: $STOP_HOOK_CMD"
      ;;
  esac
else
  mkdir -p "$TARGET/.claude"
  if [[ ! -f "$SETTINGS_FILE_STOP" ]]; then
    echo '{}' > "$SETTINGS_FILE_STOP"
  fi
  python3 - "$SETTINGS_FILE_STOP" "$STOP_HOOK_CMD" "$STOP_HOOK_STALE_SUFFIX" "$STOP_HOOK_ADAPTER_SUFFIX" <<'STOP_SETTINGS_PY'
import json, os, sys
settings_path  = sys.argv[1]
hook_command   = sys.argv[2]
stale_suffix   = sys.argv[3]
adapter_suffix = sys.argv[4]

with open(settings_path) as f:
    settings = json.load(f)
hooks = settings.setdefault("hooks", {})
subagent_stop = hooks.setdefault("SubagentStop", [])

def references(cmd, suffix):
    if not isinstance(cmd, str):
        return False
    return cmd.rstrip('"').rstrip("'").endswith(suffix)

# Remove any entry wired to the stale post-agent-hook.sh command (the pre-fix
# wiring bug). Only hooks referencing the stale path are touched -- other
# tools' SubagentStop entries (e.g. install-sandbox-hook.sh's
# subagent_stop_dial_audit.py) are left exactly as they are.
removed_stale = False
migrated_subagent_stop = []
for entry in subagent_stop:
    if not isinstance(entry, dict):
        migrated_subagent_stop.append(entry)
        continue
    original_hooks = entry.get("hooks", []) or []
    kept_hooks = [
        h for h in original_hooks
        if not (isinstance(h, dict) and references(h.get("command"), stale_suffix))
    ]
    if len(kept_hooks) != len(original_hooks):
        removed_stale = True
    if kept_hooks or not original_hooks:
        if kept_hooks != original_hooks:
            entry = dict(entry)
            entry["hooks"] = kept_hooks
        migrated_subagent_stop.append(entry)
    # else: entry's only hook(s) were stale -- drop the whole entry
subagent_stop = migrated_subagent_stop
hooks["SubagentStop"] = subagent_stop

def _has_adapter(entry):
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and references(h.get("command"), adapter_suffix):
            return True
    return False

already = any(_has_adapter(e) for e in subagent_stop if isinstance(e, dict))
added = False
if not already:
    subagent_stop.append({
        "matcher": ".*",
        "hooks": [{"type": "command", "command": hook_command}],
    })
    added = True

if removed_stale or added:
    tmp = settings_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)
    if removed_stale and added:
        print(f"Replaced stale post-agent-hook.sh SubagentStop entry with adapter in {settings_path}")
    elif added:
        print(f"Registered SubagentStop hook in {settings_path}")
    else:
        print(f"Removed stale post-agent-hook.sh SubagentStop entry in {settings_path}")
else:
    print(f".claude/settings.json: SubagentStop hook already registered")
STOP_SETTINGS_PY
fi

# 10. hooks/ install note: already installed by the BOOTSTRAP_PATHS loop
#     above (step 2) — derived from the live hooks/ tree rather than the 2
#     files loop-bootstrap/hooks/ used to hand-carry (live hooks/ has grown
#     to 9 files; the snapshot never caught up).

# 11. .claude/settings.json — register PreToolUse sandbox hook (C2)
SETTINGS_FILE="$TARGET/.claude/settings.json"
HOOK_CMD='python3 "$CLAUDE_PROJECT_DIR/hooks/sandbox.py"'
if [[ "$DRY_RUN" == "true" ]]; then
  if python3 -c "
import json, sys
try:
    s = json.load(open('$SETTINGS_FILE'))
    for e in s.get('hooks',{}).get('PreToolUse',[]):
        for h in e.get('hooks',[]):
            if h.get('command','') == '$HOOK_CMD':
                sys.exit(0)
    sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null; then
    echo "[dry-run] .claude/settings.json: sandbox hook already registered — skipping"
  else
    echo "[dry-run] would register sandbox hook in $SETTINGS_FILE"
  fi
else
  mkdir -p "$TARGET/.claude"
  if [[ ! -f "$SETTINGS_FILE" ]]; then
    echo '{}' > "$SETTINGS_FILE"
  fi
  python3 - "$SETTINGS_FILE" "$HOOK_CMD" <<'SETTINGS_PY'
import json, os, sys
settings_path = sys.argv[1]
hook_command  = sys.argv[2]
with open(settings_path) as f:
    settings = json.load(f)
hooks = settings.setdefault("hooks", {})
pre_tool_use = hooks.setdefault("PreToolUse", [])

def _has_hook(entry):
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and h.get("command") == hook_command:
            return True
    return False

already = any(_has_hook(e) for e in pre_tool_use if isinstance(e, dict))
if not already:
    for tool_name in ("Bash", "Edit", "Write"):
        pre_tool_use.append({
            "matcher": tool_name,
            "hooks": [{"type": "command", "command": hook_command}],
        })
    tmp = settings_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)
    print(f"Registered sandbox hook in {settings_path}")
else:
    print(f".claude/settings.json: sandbox hook already registered")
SETTINGS_PY
fi

# 12. scripts/hooks/post-merge.d/ and post-agent.d/ install note: already
#     installed by the BOOTSTRAP_PATHS loop above (step 2), as part of the
#     derived scripts/ tree.

# 13. GitHub labels bootstrap (C3): scripts/bootstrap-github-labels.sh is
#     already installed by the BOOTSTRAP_PATHS loop above (step 2), as part
#     of the derived scripts/ tree.

# Run labels bootstrap if we have a gh token and the repo is resolvable
if [[ "$DRY_RUN" != "true" ]]; then
  RESOLVED_REPO="$TARGET_REPO"
  if [[ -f "$TARGET/.autonomous-team/project.json" ]]; then
    PJ_REPO=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get('repo', ''))
" "$TARGET/.autonomous-team/project.json" 2>/dev/null || echo "")
    if [[ -n "$PJ_REPO" ]]; then
      RESOLVED_REPO="$PJ_REPO"
    fi
  fi
  # RESOLVED_REPO may have just been overridden from project.json, a file
  # this script doesn't control the contents of -- --repo itself was
  # already validated against this same shape above, but an override from
  # the file was not. Run it through the same check rather than handing an
  # unvalidated string to a `gh` write (confused-deputy shape, CWE-20/441).
  if [[ -n "$RESOLVED_REPO" ]] && [[ ! "$RESOLVED_REPO" =~ ^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "ERROR: repo slug resolved from $TARGET/.autonomous-team/project.json is malformed: $RESOLVED_REPO" >&2
    echo "       Expected OWNER/NAME, same format as --repo. Fix project.json's 'repo' field and re-run." >&2
    exit 1
  fi
  if [[ -n "$RESOLVED_REPO" ]] && command -v gh >/dev/null 2>&1 && [[ -f "$TARGET/scripts/bootstrap-github-labels.sh" ]]; then
    echo ""
    echo "==> Creating GitHub labels for $RESOLVED_REPO"
    bash "$TARGET/scripts/bootstrap-github-labels.sh" --repo "$RESOLVED_REPO" 2>/dev/null \
      && echo "Labels created." \
      || echo "Warning: label creation failed (no gh token? labels can be created later with: bash scripts/bootstrap-github-labels.sh)"
  else
    echo ""
    echo "Note: GitHub labels not created (gh not available or repo unclear)."
    echo "      Run manually: cd $TARGET && bash scripts/bootstrap-github-labels.sh"
  fi
fi

# 14. .mcp.json template (I1) — scaffold MCP server config if absent.
#     Does NOT overwrite existing .mcp.json — each project uses different servers.
MCP_TEMPLATE="$SCRIPT_DIR/templates/.mcp.json.template"
MCP_DST="$TARGET/.mcp.json"
if [[ -f "$MCP_TEMPLATE" ]]; then
  if [[ -f "$MCP_DST" ]]; then
    echo "[=] .mcp.json already exists in $TARGET — skipping (project-specific)"
  elif [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would copy: $MCP_TEMPLATE → $MCP_DST"
  else
    cp -P "$MCP_TEMPLATE" "$MCP_DST"
    chmod u+w "$MCP_DST"
    echo "Installed .mcp.json scaffold → $MCP_DST"
    echo "NOTE: Edit $MCP_DST to configure MCP servers for your project."
  fi
else
  echo "WARNING: loop-bootstrap/templates/.mcp.json.template not found — skipping MCP scaffold."
fi

# 15. requirements.txt template + setup-deps.sh (I3).
#     Provides the list of Python packages backend/ needs. Operator runs
#     `bash scripts/setup-deps.sh` to install after bootstrap. Deliberately
#     curated (see the BOOTSTRAP_PATHS loop's comment above) rather than
#     copied verbatim from the live root requirements.txt.
REQ_TEMPLATE="$SCRIPT_DIR/templates/requirements.txt.template"
REQ_DST="$TARGET/requirements.txt"

if [[ -f "$REQ_TEMPLATE" ]]; then
  if [[ -f "$REQ_DST" ]]; then
    echo "[=] requirements.txt already exists in $TARGET — skipping"
  elif [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would copy: $REQ_TEMPLATE → $REQ_DST"
  else
    cp -P "$REQ_TEMPLATE" "$REQ_DST"
    chmod u+w "$REQ_DST"
    echo "Installed requirements.txt → $REQ_DST"
  fi
else
  echo "WARNING: loop-bootstrap/templates/requirements.txt.template not found — skipping."
fi

# 16. Control-plane defaults (I6) — conservative fork defaults for config.json.
#     Written to <target>/.autonomous-team/config.json if absent.
#     backend/control_plane.py reads THIS exact filename (config.json, not
#     control-plane.json — see backend/control_plane.py's _CONFIG_PATH) and
#     merges it over its hardcoded defaults. Getting the filename wrong here
#     meant the file installed but was never actually read: server startup
#     (backend/spawn_guard.py's assert_gate_present()) hard-fails whenever
#     gates.allow_claude_spawn is absent from config.json, which it always
#     was, because nothing else in this pipeline creates config.json either
#     (D#1872 item 19b — reproduced via PR #1880's README walkthrough).
CP_DEFAULTS_TEMPLATE="$SCRIPT_DIR/templates/control-plane-defaults.json.template"
CP_DEFAULTS_DST="$TARGET/.autonomous-team/config.json"
if [[ -f "$CP_DEFAULTS_TEMPLATE" ]]; then
  if [[ -f "$CP_DEFAULTS_DST" ]]; then
    echo "[=] .autonomous-team/config.json already exists — skipping"
  elif [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would copy: $CP_DEFAULTS_TEMPLATE → $CP_DEFAULTS_DST"
  else
    mkdir -p "$TARGET/.autonomous-team"
    cp -P "$CP_DEFAULTS_TEMPLATE" "$CP_DEFAULTS_DST"
    chmod u+w "$CP_DEFAULTS_DST"
    echo "Installed control-plane defaults → $CP_DEFAULTS_DST"
    echo "NOTE: Edit to adjust concurrency caps and feature gates for your project."
  fi
else
  echo "WARNING: loop-bootstrap/templates/control-plane-defaults.json.template not found — skipping."
fi

# 16b. bot_account (D#2219) — scripts/lib/external_intake_gate.py's trust
#      gate hard-fails resolving BOT_ACCOUNT until a "bot_account" field
#      exists in .autonomous-team/config.json, and nothing in this pipeline
#      ever wrote one: the file this step installs above is a static
#      template, and no per-adopter identity was ever merged into it. That
#      blocks the project's very first Discussion until an operator sets it
#      by hand.
#
#      The value is the authenticated account this same run already used to
#      create labels in step 13 above — resolve it the same way
#      scripts/provision-dial-allowlist.sh resolves its operator login
#      (`gh api user --jq .login`), and merge it into config.json only if
#      the key isn't already set, so an operator's own value (or a prior
#      bootstrap's) is never clobbered.
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] would resolve the authenticated gh account and write bot_account into $CP_DEFAULTS_DST if not already set"
elif [[ -f "$CP_DEFAULTS_DST" ]]; then
  BOT_ACCOUNT_LOGIN=""
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    BOT_ACCOUNT_LOGIN="$(gh api user --jq .login 2>/dev/null || true)"
  fi
  if [[ -n "$BOT_ACCOUNT_LOGIN" ]]; then
    python3 - "$CP_DEFAULTS_DST" "$BOT_ACCOUNT_LOGIN" <<'BOT_ACCOUNT_PY'
import json
import sys

path, login = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)

if cfg.get("bot_account"):
    print(f"[=] bot_account already set in {path} — leaving as-is")
else:
    cfg["bot_account"] = login
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Wrote bot_account={login!r} to {path}")
BOT_ACCOUNT_PY
  else
    echo "[!] WARN: could not resolve the authenticated GitHub account (gh absent or not logged in) — bot_account NOT written to $CP_DEFAULTS_DST." >&2
    echo "    external_intake_gate.py will refuse to classify any Discussion until you add it by hand:" >&2
    echo "    add a \"bot_account\": \"<your-automation-account-login>\" field to $CP_DEFAULTS_DST" >&2
  fi
else
  echo "[!] WARN: $CP_DEFAULTS_DST not present — bot_account not written (control-plane-defaults.json.template missing above?)." >&2
fi

# 16c. config.json "repo" field (D#2226) — same idempotent merge idiom as
#      bot_account above, but for the field scripts/lib/repo-resolve.sh and
#      ts-backend/src/config/repo.ts's resolveRepo() treat as their
#      HIGHEST-priority source (ahead of GH_REPO/_REPO env vars, ahead of the
#      hardcoded DEFAULT_REPO fallback). Neither --mode actually wrote this
#      field before this step existed -- step 19 below only ever wrote
#      project.json's "repo", a separate file with separate readers
#      (backend/_repo.py and friends). --mode existing repos that appeared
#      to have config.json's "repo" set were carrying a value someone added
#      by hand, not something either mode's script path produced; a fork
#      relying on the documented precedence order would otherwise silently
#      fall through to DEFAULT_REPO (this project's own slug) the first time
#      GH_REPO/_REPO weren't set, regardless of --mode. Write it here, from
#      the same already-validated $TARGET_REPO used for the sed pass, so both
#      modes get it identically and un-guessed -- never overwriting a value
#      an operator (or a prior bootstrap) already set.
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] would write \"repo\": \"$TARGET_REPO\" into $CP_DEFAULTS_DST if not already set"
elif [[ -f "$CP_DEFAULTS_DST" ]]; then
  python3 - "$CP_DEFAULTS_DST" "$TARGET_REPO" <<'CONFIG_REPO_PY'
import json
import sys

path, repo = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)

if cfg.get("repo"):
    print(f"[=] repo already set in {path} — leaving as-is")
else:
    cfg["repo"] = repo
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Wrote repo={repo!r} to {path}")
CONFIG_REPO_PY
else
  echo "[!] WARN: $CP_DEFAULTS_DST not present — repo field not written (control-plane-defaults.json.template missing above?)." >&2
fi

# 17. Team-log Issue auto-create (I4).
#     Creates the first team-log Issue in the target repo so the loop can post
#     to it immediately after bootstrap. Skipped when:
#       - BOOTSTRAP_SKIP_TEAMLOG=1 env var is set
#       - gh CLI is not available
#       - team-log label doesn't exist yet in the target repo
#       - an open team-log issue already exists
#
#     Depends on the GitHub labels bootstrap step above having run first.
if [[ "${BOOTSTRAP_SKIP_TEAMLOG:-}" == "1" ]]; then
  echo "[=] BOOTSTRAP_SKIP_TEAMLOG=1 — skipping team-log Issue creation"
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] would check for and create team-log Issue in $TARGET_REPO"
elif ! command -v gh >/dev/null 2>&1; then
  echo "[=] gh CLI not available — skipping team-log Issue creation (run manually: bash scripts/rotate-team-log.sh current)"
elif [[ -f "$TARGET/scripts/rotate-team-log.sh" ]]; then
  RESOLVED_TEAMLOG_REPO="$TARGET_REPO"
  if [[ -f "$TARGET/.autonomous-team/project.json" ]]; then
    PJ_REPO2=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get('repo', ''))
" "$TARGET/.autonomous-team/project.json" 2>/dev/null || echo "")
    if [[ -n "$PJ_REPO2" ]]; then
      RESOLVED_TEAMLOG_REPO="$PJ_REPO2"
    fi
  fi
  # Same rule as RESOLVED_REPO above: a value overridden from project.json
  # -- a file this script doesn't control the contents of -- goes through
  # the same OWNER/NAME check --repo itself was already validated against,
  # rather than reaching a `gh` read unvalidated.
  if [[ -n "$RESOLVED_TEAMLOG_REPO" ]] && [[ ! "$RESOLVED_TEAMLOG_REPO" =~ ^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "ERROR: repo slug resolved from $TARGET/.autonomous-team/project.json is malformed: $RESOLVED_TEAMLOG_REPO" >&2
    echo "       Expected OWNER/NAME, same format as --repo. Fix project.json's 'repo' field and re-run." >&2
    exit 1
  fi
  # Check if team-log label exists
  if gh label list --repo "$RESOLVED_TEAMLOG_REPO" --json name --jq '.[].name' 2>/dev/null | grep -q "^team-log$"; then
    # Check if open team-log issue already exists
    EXISTING_LOG=$(gh issue list --repo "$RESOLVED_TEAMLOG_REPO" --label team-log --state open --json number --jq '.[0].number' 2>/dev/null || echo "")
    if [[ -z "$EXISTING_LOG" || "$EXISTING_LOG" == "null" ]]; then
      echo ""
      echo "==> Creating initial team-log Issue in $RESOLVED_TEAMLOG_REPO"
      ROTATE_LOG_OUT=$(bash "$TARGET/scripts/rotate-team-log.sh" current 2>&1) || true
      echo "Team-log Issue: $ROTATE_LOG_OUT"
    else
      echo "[=] Team-log Issue #$EXISTING_LOG already exists — skipping"
    fi
  else
    echo "[=] team-log label not found in $RESOLVED_TEAMLOG_REPO — skipping team-log Issue creation"
    echo "      Run after labels are created: bash scripts/rotate-team-log.sh current"
  fi
else
  echo "[=] scripts/rotate-team-log.sh not installed — skipping team-log Issue creation"
fi

# 18. jsonl-rotation config (D#976) — install default rotation thresholds.
#     scripts/sweep-jsonl.sh reads .autonomous-team/jsonl-rotation.json first,
#     then falls back to templates/jsonl-rotation.json. Ship the template.
JSONL_ROT_SRC="$SCRIPT_DIR/templates-extra/jsonl-rotation.json"
JSONL_ROT_DST="$TARGET/templates/jsonl-rotation.json"
if [[ -f "$JSONL_ROT_SRC" ]]; then
  if [[ -f "$JSONL_ROT_DST" ]]; then
    echo "[=] templates/jsonl-rotation.json already exists — skipping"
  elif [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would copy: $JSONL_ROT_SRC → $JSONL_ROT_DST"
  else
    mkdir -p "$TARGET/templates"
    cp -P "$JSONL_ROT_SRC" "$JSONL_ROT_DST"
    chmod u+w "$JSONL_ROT_DST"
    echo "Installed jsonl-rotation default config → $JSONL_ROT_DST"
  fi
else
  echo "WARNING: loop-bootstrap/templates-extra/jsonl-rotation.json not found — JSONL sweeper will use built-in defaults."
fi

# 19. Minimal .autonomous-team/project.json "repo" field, if absent.
#     backend/_repo.py (D#1870) resolves the repo slug at import time with
#     no hardcoded fallback — AUTONOMOUS_TEAM_REPO env, then
#     <state_dir>/project.json, then repo-root .autonomous-team/project.json,
#     then a loud RuntimeError. Full project.json (dashboard_port, language,
#     hub_files, ...) is scripts/coldstart-project.sh's job, run as a
#     separate step after bootstrap per the "Next steps" printed below — but
#     without at least a "repo" field here, backend.server cannot import
#     until the operator remembers to run that second script, which is
#     exactly the kind of silent gap D#1890's own import-smoke-test (step 20
#     below) exists to catch rather than assume away. coldstart-project.sh
#     merges into this file rather than overwriting it, so writing a minimal
#     one here does not conflict with running it afterward.
PROJECT_JSON_DST="$TARGET/.autonomous-team/project.json"
if [[ -f "$PROJECT_JSON_DST" ]]; then
  echo "[=] .autonomous-team/project.json already exists — skipping (repo field not modified)"
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] would create: $PROJECT_JSON_DST with \"repo\": \"$TARGET_REPO\""
else
  mkdir -p "$TARGET/.autonomous-team"
  python3 - "$PROJECT_JSON_DST" "$TARGET_REPO" <<'PROJECT_JSON_PY'
import json, sys
dst, repo = sys.argv[1], sys.argv[2]
with open(dst, "w") as f:
    json.dump({"repo": repo}, f, indent=2)
    f.write("\n")
PROJECT_JSON_PY
  echo "Created $PROJECT_JSON_DST with \"repo\": \"$TARGET_REPO\" (run scripts/coldstart-project.sh to fill in the rest)"
fi

# 19a. .autonomous-team/engine-install.json baseline stamp (D#2335 PR 1) —
#      records which engine commit this install was bootstrapped/updated
#      from, so scripts/update-check.sh has something to compare against
#      upstream. Nothing wrote this before D#2335: bootstrap.sh had no
#      rev-parse/VERSION/engine_commit write of any kind (grepped and
#      confirmed zero hits across bootstrap.sh, coldstart.sh,
#      coldstart-unified.sh before this PR). Pre-existing installs that
#      never ran this step correctly report "cannot determine" with a
#      named remedy (`--record-baseline`) rather than update-check.sh
#      guessing a baseline it never saw.
#
#      A new, distinct, engine-owned file — deliberately NOT a key inside
#      config.json or project.json, both of which carry local divergence
#      (I6 above) that an update must never touch.
#
#      This step runs on EVERY bootstrap invocation (including --force
#      re-runs), unlike the `[[ -f ... ]] skip` guards above — an update
#      is exactly the moment this stamp is supposed to move forward. This
#      does not reopen the idempotency contract those other steps protect:
#      config.json, project.json and everything else this script writes
#      are unaffected by this step running unconditionally, and
#      tests/test_loop_bootstrap_extended.sh's whole-tree idempotency
#      check excludes this one path (bootstrapped_at is wall-clock, not
#      content) with a comment explaining why, right at that check.
#
#      engine_commit is read from REPO_ROOT (the engine tree currently
#      running this script), not from an env var some caller might not
#      have set. In an export or a plugin tarball with no .git, rev-parse
#      fails and this deliberately records `engine_commit: null` — that is
#      what makes update-check.sh report reason=no_baseline_recorded
#      instead of inventing a fake baseline for a build with no real one.
#
#      "source" is a best-effort install-shape guess for humans reading
#      the stamp; nothing in the Spec's acceptance criteria depends on its
#      exact value, only on the key existing.
#
#      "source_repo" is deliberately NOT derived from the $SOURCE_REPO sed
#      key above — that variable is documented at length (bootstrap.sh:65-
#      91) as a REWRITE key that may legitimately still hold the pre-
#      rename slug for parts of the corpus, not "this engine's current
#      upstream identity". Using it here would make update-check.sh
#      compare against a slug that may not resolve on GitHub at all. A
#      dedicated LOOP_BOOTSTRAP_ENGINE_REPO override (mirroring the
#      LOOP_BOOTSTRAP_SOURCE_REPO pattern) exists for forks; the default
#      matches the canonical engine identity, same as the worked example
#      in the Spec's own Implementation Notes.
ENGINE_INSTALL_JSON_DST="$TARGET/.autonomous-team/engine-install.json"
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] would create/refresh: $ENGINE_INSTALL_JSON_DST"
else
  mkdir -p "$TARGET/.autonomous-team"
  ENGINE_COMMIT_VAL="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
  ENGINE_VERSION_VAL=""
  if [[ -f "$REPO_ROOT/engine/VERSION" ]]; then
    ENGINE_VERSION_VAL="$(tr -d '[:space:]' < "$REPO_ROOT/engine/VERSION")"
  fi
  if [[ -d "$REPO_ROOT/.git" ]]; then
    ENGINE_SOURCE_KIND="clone"
  elif [[ -f "$REPO_ROOT/.claude-plugin/plugin.json" ]]; then
    ENGINE_SOURCE_KIND="plugin"
  else
    ENGINE_SOURCE_KIND="export"
  fi
  ENGINE_CANONICAL_REPO="${LOOP_BOOTSTRAP_ENGINE_REPO:-fulcrumaxe/fulcrumaxe}"
  python3 - "$ENGINE_INSTALL_JSON_DST" "$ENGINE_VERSION_VAL" "$ENGINE_COMMIT_VAL" "$ENGINE_SOURCE_KIND" "$ENGINE_CANONICAL_REPO" <<'ENGINE_INSTALL_PY'
import json, sys
from datetime import datetime, timezone

dst, engine_version, engine_commit, source_kind, source_repo = sys.argv[1:6]
data = {
    "engine_version": engine_version or None,
    "engine_commit": engine_commit or None,
    "source": source_kind,
    "source_repo": source_repo,
    "bootstrapped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(dst, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
ENGINE_INSTALL_PY
  echo "Wrote $ENGINE_INSTALL_JSON_DST (engine_commit=${ENGINE_COMMIT_VAL:-null})"
fi

# 20. Post-copy identifier rewrite (D#1890) — apply the source→target repo
#     rewrite across every directory this script wrote to via
#     rsync_bootstrap_dir() above, closing the "bulk copy bypasses the
#     rewrite" gap. Individually do_install()'d files (agents, commands,
#     the deliberate-variant scripts, residue scripts, templates) are
#     harmless to re-sweep here too — sed is idempotent — so this single
#     pass covers the whole installed tree rather than trying to track
#     which install path touched what.
if [[ "$DRY_RUN" != "true" ]]; then
  echo ""
  echo "==> rewriting ${SOURCE_REPO} → ${TARGET_REPO} across the installed tree"
  rewrite_tree_identifiers "$TARGET/backend"
  rewrite_tree_identifiers "$TARGET/scripts"
  rewrite_tree_identifiers "$TARGET/hooks"
  rewrite_tree_identifiers "$TARGET/.claude/agents"
  rewrite_tree_identifiers "$TARGET/.claude/commands"
fi

# 21. Adopter .gitignore (D#2235) — install a curated, marker-guarded block
#     so a fresh coldstart leaves `git status` showing only files the
#     adopter is meant to commit, instead of 11+ unexplained runtime-state
#     entries. Ships the curated template below, NOT the engine's own
#     .gitignore — that one carries internal-only carve-outs
#     (training-data/, docker/gemma4-trainer/, archive/state-db-residue-*,
#     wiki/*, vast-*.json) that are noise in an adopter repo.
#     Marker-guarded append-only, same shape as step 6's CLAUDE.md append:
#     never truncates an existing .gitignore, never appends a second block.
GITIGNORE_TEMPLATE="$SCRIPT_DIR/templates/.gitignore.template"
GI_START_MARKER="# >>> AUTONOMOUS_TEAM_BOOTSTRAP_GITIGNORE_START >>>"
GITIGNORE_WRITTEN="false"
if [[ ! -f "$GITIGNORE_TEMPLATE" ]]; then
  echo "WARNING: loop-bootstrap/templates/.gitignore.template not found — skipping .gitignore install." >&2
else
  GITIGNORE_DST="$TARGET/.gitignore"
  if grep -q "$GI_START_MARKER" "$GITIGNORE_DST" 2>/dev/null; then
    echo "[=] .gitignore: managed block already present — skipping"
  elif [[ "$DRY_RUN" == "true" ]]; then
    if [[ -f "$GITIGNORE_DST" ]]; then
      echo "[dry-run] would append managed block to $GITIGNORE_DST"
    else
      echo "[dry-run] would create $GITIGNORE_DST with managed block"
    fi
  else
    if [[ -f "$GITIGNORE_DST" ]]; then
      # Defensive: an existing $GITIGNORE_DST could itself have arrived via a
      # mode-propagating copy earlier in the pipeline (or from a read-only
      # source tree the adopter cloned in). Ensure it's writable before append.
      chmod u+w "$GITIGNORE_DST" 2>/dev/null || true
      echo "" >> "$GITIGNORE_DST"
      cat "$GITIGNORE_TEMPLATE" >> "$GITIGNORE_DST"
      echo "Appended managed block to $GITIGNORE_DST"
    else
      cp "$GITIGNORE_TEMPLATE" "$GITIGNORE_DST"
      chmod u+w "$GITIGNORE_DST"
      echo "Installed .gitignore → $GITIGNORE_DST"
    fi
    GITIGNORE_WRITTEN="true"
  fi
fi

# 22. Post-install import-smoke-test (D#1890 Spec §1.6, §1.8) — verify the
#     derived payload actually starts instead of printing "Bootstrap
#     complete" over a broken install, which is what this script did before
#     this PR: it exited 0 and printed "Bootstrap complete" over an install
#     whose backend/server.py could not import (D#1890 body). Fails loudly
#     and non-zero, naming the failed import on stderr, rather than leaving
#     the adopter to discover it themselves on their first real command.
if [[ "$DRY_RUN" != "true" ]]; then
  if [[ -n "$SIMULATE_MISSING" ]]; then
    echo "[test-only] --simulate-missing: removing $TARGET/$SIMULATE_MISSING"
    rm -rf "${TARGET:?}/${SIMULATE_MISSING:?}"
  fi
  echo ""
  echo "==> verifying the installed backend imports"
  # PYTHONDONTWRITEBYTECODE: this check must not itself break the
  # idempotency this script documents at the top ("re-running on an
  # already-bootstrapped repo produces no diff") — without it, each
  # `import backend.server` here writes fresh __pycache__/*.pyc files whose
  # bytes are not stable run-to-run, so a second bootstrap looks like it
  # changed files it didn't.
  if IMPORT_ERR=$(cd "$TARGET" && PYTHONDONTWRITEBYTECODE=1 python3 -c "import backend.server" 2>&1 >/dev/null); then
    echo "OK: backend.server imports cleanly in the installed target."
  else
    echo "ERROR: installed backend failed to import — the install is broken:" >&2
    echo "$IMPORT_ERR" >&2
    echo "Bootstrap did NOT complete successfully." >&2
    exit 1
  fi
fi

echo ""
echo "Bootstrap complete."
if [[ "$GITIGNORE_WRITTEN" == "true" ]]; then
  echo "  .gitignore installed — review the managed block and commit it."
fi
echo ""
echo "Next steps:"
echo "  cd $TARGET"
echo "  bash scripts/coldstart-project.sh $TARGET <project-name>   # sets up state dir"
echo "  # Edit .autonomous-team/project.json to set repo, language, hub_files"
echo "  # If GitHub labels were not created automatically:"
echo "  #   bash scripts/bootstrap-github-labels.sh"
echo "  bash scripts/setup-deps.sh    # install Python dependencies — not run automatically by this bootstrap"
for BOOTSTRAP_DEP_DIR in dashboard ts-backend tui; do
  if [[ -f "$TARGET/$BOOTSTRAP_DEP_DIR/package.json" ]]; then
    if [[ "$BOOTSTRAP_DEP_DIR" == "ts-backend" && -f "$TARGET/$BOOTSTRAP_DEP_DIR/bun.lock" ]]; then
      echo "  (cd $BOOTSTRAP_DEP_DIR && bun install)   # install Node dependencies — not run automatically by this bootstrap"
    else
      echo "  (cd $BOOTSTRAP_DEP_DIR && npm install)   # install Node dependencies — not run automatically by this bootstrap"
    fi
  fi
done
echo "  claude"
echo "  # Then in Claude Code: /start-the-day"
# SubagentStop hook is now auto-registered in $TARGET/.claude/settings.json by step 9.
