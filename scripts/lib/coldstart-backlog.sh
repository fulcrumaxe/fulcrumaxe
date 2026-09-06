#!/usr/bin/env bash
# scripts/lib/coldstart-backlog.sh
#
# The epic-backlog beat of coldstart: look at whatever is (or is not) at the
# backlog directory, say what it found, and scaffold from the committed
# template only in the cases where scaffolding cannot destroy anything.
#
# coldstart.sh used to print a fill-in-the-blank epic format inline, in a
# heredoc. That format described a document with a Goal / Why now / Scope /
# Out of scope shape and no YAML frontmatter at all, while
# scripts/import-epic-tasks.py — the thing that actually consumes the
# backlog — reads frontmatter (epic, task, title, type, status,
# estimated_hours, depends_on, tags, parent_task, supersedes) and derives
# every Discussion title and label from it. An operator who followed the
# on-screen instructions produced files the seeding step could not use. The
# format now lives in one place, scripts/coldstart-templates/epic/, and both
# sides point at it instead of restating it.
#
# Four cases, three behaviours:
#
#   absent      the backlog directory does not exist       -> announce, scaffold
#   empty       it exists and holds no markdown            -> announce, scaffold
#   conforming  it already holds epics the importer reads  -> report, touch nothing
#   foreign     it holds markdown in some other shape      -> report, touch nothing
#
# The foreign case does NOT convert, and there is no flag that makes it
# convert. Converting a backlog means mapping headings whose meaning we do not
# know onto fields the importer requires — which estimate, which acceptance
# criteria, which task depends on which — and then creating GitHub Discussions
# from the result in a repository we do not own. Files could be restored;
# Discussions created in someone else's repo are a manual cleanup, and a
# conversion that guesses wrong is discovered only after it has published. So
# this reports what it found, names the template, and stops. The operator
# converts, or does not, knowing their own format.
#
# Nothing here deletes, moves, or rewrites an existing file. The scaffold
# writes only files that do not already exist, and only inside the backlog
# directory it was given.
#
# No network calls, no gh calls.

set -euo pipefail

_BACKLOG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_BACKLOG_REPO_ROOT="$(cd "$_BACKLOG_LIB_DIR/../.." && pwd)"

COLDSTART_EPIC_TEMPLATE_DIR="${COLDSTART_EPIC_TEMPLATE_DIR:-$_BACKLOG_REPO_ROOT/scripts/coldstart-templates/epic}"

# ---------------------------------------------------------------------------
# Classify — prints exactly one of: absent | empty | conforming | foreign
#
# "conforming" is defined against what the importer actually looks for, not
# against a prettier idea of the format: scripts/import-epic-tasks.py walks
# <backlog>/epic-*/ and reads epic.md as the overview plus every other *.md as
# a task. So a directory counts as conforming if it has at least one
# epic-*/epic.md, or at least one epic-*/<name>.md whose frontmatter carries a
# `task:` field. Anything else with markdown in it is foreign.
# ---------------------------------------------------------------------------
coldstart_backlog_classify() {
  local dir="$1"

  [[ -d "$dir" ]] || { echo "absent"; return 0; }

  local md_count
  md_count="$(find "$dir" -type f -name '*.md' 2>/dev/null | head -n 1 | wc -l)"
  if [[ "$md_count" -eq 0 ]]; then
    echo "empty"
    return 0
  fi

  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if [[ "$(basename "$f")" == "epic.md" ]]; then
      echo "conforming"
      return 0
    fi
    # A task file: frontmatter opens on line 1 and carries a `task:` field.
    # Read only the frontmatter block, so a `task:` mentioned in prose 200
    # lines down cannot vote.
    if [[ "$(head -n 1 "$f")" == "---" ]]; then
      if sed -n '2,/^---[[:space:]]*$/p' "$f" | grep -qE '^task:[[:space:]]*[0-9]'; then
        echo "conforming"
        return 0
      fi
    fi
  done < <(find "$dir" -mindepth 2 -maxdepth 2 -type f -name '*.md' -path '*/epic-*/*' 2>/dev/null)

  echo "foreign"
}

# ---------------------------------------------------------------------------
# Describe what is there — used by the report-only branches. Prints counts
# with their scope named, never a bare number.
# ---------------------------------------------------------------------------
_coldstart_backlog_describe() {
  local dir="$1"
  local dirs files
  dirs="$(find "$dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
  files="$(find "$dir" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
  echo "    $dirs subdirector$([[ "$dirs" == "1" ]] && echo y || echo ies), $files markdown file(s) under $dir"
}

# ---------------------------------------------------------------------------
# Scaffold — announce every path first, then write. Never overwrites.
#
# The example epic is written verbatim from the template, placeholders and
# all, so there is exactly one copy of the format and the file an operator
# edits is the file the template describes.
# ---------------------------------------------------------------------------
coldstart_backlog_scaffold() {
  local dir="$1"
  local tpl="${2:-$COLDSTART_EPIC_TEMPLATE_DIR}"

  if [[ ! -f "$tpl/epic.md" || ! -f "$tpl/NN.md" ]]; then
    echo "[!] WARN: epic template not found at $tpl — skipping backlog scaffold." >&2
    echo "    Expected $tpl/epic.md and $tpl/NN.md." >&2
    return 1
  fi

  local epic_dir="$dir/epic-1-example"

  echo "  About to write, and nothing else:"
  echo "    $epic_dir/epic.md   (from $tpl/epic.md)"
  echo "    $epic_dir/01.md     (from $tpl/NN.md)"
  echo ""

  mkdir -p "$epic_dir"

  local wrote=0 skipped=0
  if [[ -e "$epic_dir/epic.md" ]]; then
    echo "  [skip] $epic_dir/epic.md already exists — left untouched"
    skipped=$((skipped + 1))
  else
    cp "$tpl/epic.md" "$epic_dir/epic.md"
    wrote=$((wrote + 1))
  fi

  if [[ -e "$epic_dir/01.md" ]]; then
    echo "  [skip] $epic_dir/01.md already exists — left untouched"
    skipped=$((skipped + 1))
  else
    cp "$tpl/NN.md" "$epic_dir/01.md"
    wrote=$((wrote + 1))
  fi

  echo "  Wrote $wrote file(s), skipped $skipped already-present file(s)."
  return 0
}

# ---------------------------------------------------------------------------
# The beat coldstart.sh calls. One argument is the backlog directory —
# whatever --backlog resolved to, never a hardcoded "epics".
# ---------------------------------------------------------------------------
coldstart_backlog_step() {
  local dir="$1"
  local repo_path="${2:-}"
  local tpl="${3:-$COLDSTART_EPIC_TEMPLATE_DIR}"

  echo "=== coldstart.sh: epic backlog ==="

  local kind
  kind="$(coldstart_backlog_classify "$dir")"

  case "$kind" in
    absent|empty)
      if [[ "$kind" == "absent" ]]; then
        echo "  No backlog directory at $dir."
      else
        echo "  $dir exists but holds no epic files."
      fi
      # Only point at the template's own docs if the template is actually
      # there — scaffold returns non-zero when it is not, and next-steps text
      # naming a file that does not exist is the failure this whole change is
      # about.
      if coldstart_backlog_scaffold "$dir" "$tpl"; then
        _coldstart_backlog_next_steps "$dir" "$tpl"
      fi
      ;;
    conforming)
      echo "  $dir already holds epics in the format the seeding step reads."
      _coldstart_backlog_describe "$dir"
      echo "  Nothing written. Re-run with --resume to seed Discussions from them."
      ;;
    foreign)
      echo "  $dir holds markdown, but not in the format the seeding step reads."
      _coldstart_backlog_describe "$dir"
      echo ""
      echo "  Nothing was written, moved, or deleted, and nothing will be:"
      echo "  coldstart does not convert an existing backlog. Which heading is"
      echo "  the acceptance criteria, which number is the estimate, and which"
      echo "  task depends on which are judgements about your format, and a"
      echo "  wrong guess would surface as Discussions created in your repo."
      echo ""
      echo "  To seed from these, add the frontmatter the importer needs to each"
      echo "  task file. The format is one file:"
      echo "    $tpl/README.md"
      echo "  Check the result without touching GitHub:"
      echo "    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name> --dry-run"
      echo ""
      echo "  To keep these files and start a separate backlog instead, pass a"
      echo "  different --backlog path — coldstart writes only where you point it."
      ;;
  esac

  # The seeding step (scripts/import-epic-tasks.py) reads <repo-path>/epics,
  # not this directory. Where --backlog points somewhere else, saying so here
  # is the difference between an operator seeing "0 task files found" and
  # understanding why.
  if [[ -n "$repo_path" && "$dir" != "$repo_path/epics" ]]; then
    echo ""
    echo "  Note: --backlog points at $dir, but the seeding step reads"
    echo "  $repo_path/epics. Epics written here will not be seeded from there."
  fi

  echo ""
}

_coldstart_backlog_next_steps() {
  local dir="$1" tpl="$2"
  cat <<EOF

  These are placeholders, not a backlog. Before seeding:
    - rename epic-1-example/ to epic-<N>-<slug> and fill in both files
    - add one numbered file per task (02.md, 03.md, ...)
    - delete the guidance comments

  The format, and what each frontmatter field turns into:
    $tpl/README.md

  Then check what would be created, without touching GitHub:
    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name> --dry-run

  Seeding an unedited placeholder creates a Discussion titled with the
  placeholder text. Edit first.
EOF
}
