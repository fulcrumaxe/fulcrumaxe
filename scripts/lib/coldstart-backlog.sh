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
# Five cases, three behaviours:
#
#   absent      the backlog directory does not exist       -> announce, scaffold
#   empty       it exists and holds no markdown            -> announce, scaffold
#   conforming  it holds task files the importer imports   -> report, touch nothing
#   incomplete  our layout, but nothing that would import  -> report, touch nothing
#   foreign     it holds markdown in some other shape      -> report, touch nothing
#
# `incomplete` is the state a repo written to coldstart's OLD on-screen format
# lands in — a directory of epic-<N>-<slug>/epic.md files with no frontmatter
# anywhere. That population is the whole reason this change exists, and they
# are the likeliest people to re-run coldstart, so getting their branch wrong
# reintroduces the original defect through a different door. An earlier cut of
# this module did exactly that: it accepted the basename `epic.md` as proof of
# conformance, told those operators they were already fine, and sent them to
# --resume, which imports 0 tasks and exits 0. Conformance is now decided by
# the frontmatter fields the importer actually reads, and the incomplete
# branch says the quiet part out loud instead of pointing at --resume.
#
# An epic.md with no task file beside it is not importable on coldstart's own
# path either. import-epic-tasks.py will create an overview Discussion from
# one, but only under --include-empty-epics, and coldstart's seeding step does
# not pass that flag. So "you have epic.md, you are set" is false twice over.
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
# The fields scripts/import-epic-tasks.py needs before a task file can become
# a Discussion. `status` is in this list on purpose: a file without one is not
# rejected, it is dropped by the status filter — silently, exit 0, with
# nothing printed about the file it skipped. That silence is the failure mode
# this whole module exists to make loud, so a file missing `status` is not
# conforming no matter how good the rest of it looks.
_COLDSTART_BACKLOG_REQUIRED_FIELDS=(epic task title type status)

# Prints a file's YAML frontmatter block, or nothing.
#
# Returns nothing when there is no closing `---` fence, which is exactly when
# import-epic-tasks.py's parse_frontmatter gives up too — the two must agree
# about what counts as frontmatter or this check is worth less than nothing.
_coldstart_backlog_frontmatter() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  [[ "$(head -n 1 "$f" 2>/dev/null)" == "---" ]] || return 0
  tail -n +2 "$f" 2>/dev/null | grep -qE '^---[[:space:]]*$' || return 0
  sed -n '2,/^---[[:space:]]*$/p' "$f" 2>/dev/null
}

# Prints a human phrase naming what is wrong with a task file, or nothing at
# all when the file carries every required field.
_coldstart_backlog_missing_fields() {
  local f="$1" fm k
  local missing=()
  fm="$(_coldstart_backlog_frontmatter "$f")"
  if [[ -z "$fm" ]]; then
    echo "no YAML frontmatter"
    return 0
  fi
  for k in "${_COLDSTART_BACKLOG_REQUIRED_FIELDS[@]}"; do
    grep -qE "^${k}:" <<<"$fm" || missing+=("$k")
  done
  [[ ${#missing[@]} -gt 0 ]] && echo "missing ${missing[*]}"
  return 0
}

# The files the importer treats as tasks: *.md under <backlog>/epic-*/, minus
# the epic.md overview.
_coldstart_backlog_task_files() {
  find "$1" -mindepth 2 -maxdepth 2 -type f -name '*.md' -path '*/epic-*/*' \
    ! -name 'epic.md' 2>/dev/null | sort
}

_coldstart_backlog_overviews() {
  find "$1" -mindepth 2 -maxdepth 2 -type f -name 'epic.md' -path '*/epic-*/*' 2>/dev/null | sort
}

# True when at least one task file carries a *---fenced* frontmatter block with
# at least one required field in it. Partial frontmatter means someone was
# aiming at this format and missed; no frontmatter anywhere and no epic.md
# means they are using a different format entirely, and the two deserve
# different answers.
_coldstart_backlog_has_partial_frontmatter() {
  local dir="$1" f fm k
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    fm="$(_coldstart_backlog_frontmatter "$f")"
    [[ -n "$fm" ]] || continue
    for k in "${_COLDSTART_BACKLOG_REQUIRED_FIELDS[@]}"; do
      grep -qE "^${k}:" <<<"$fm" && return 0
    done
  done < <(_coldstart_backlog_task_files "$dir")
  return 1
}

# ---------------------------------------------------------------------------
# Classify — prints exactly one of:
#   absent | empty | conforming | incomplete | foreign
#
# Conformance is decided by the frontmatter fields the importer reads, never
# by a filename. A directory is conforming when at least one task file carries
# all of _COLDSTART_BACKLOG_REQUIRED_FIELDS — that is the whole test, because
# it is the whole of what stands between a file and a Discussion.
# ---------------------------------------------------------------------------
coldstart_backlog_classify() {
  local dir="$1"

  [[ -d "$dir" ]] || { echo "absent"; return 0; }

  if ! find "$dir" -type f -name '*.md' 2>/dev/null | grep -q .; then
    echo "empty"
    return 0
  fi

  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if [[ -z "$(_coldstart_backlog_missing_fields "$f")" ]]; then
      echo "conforming"
      return 0
    fi
  done < <(_coldstart_backlog_task_files "$dir")

  # Nothing here would import. Is this our layout gone stale, or someone
  # else's format altogether?
  if [[ -n "$(_coldstart_backlog_overviews "$dir")" ]] \
     || _coldstart_backlog_has_partial_frontmatter "$dir"; then
    echo "incomplete"
    return 0
  fi

  echo "foreign"
}

# How many conforming task files would survive the importer's DEFAULT status
# filter (not-started, in_progress). A backlog of entirely completed tasks is
# well-formed and imports nothing, which is correct — but saying so beats
# another silent zero.
_coldstart_backlog_importable_count() {
  local dir="$1" f fm st n=0
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    [[ -z "$(_coldstart_backlog_missing_fields "$f")" ]] || continue
    fm="$(_coldstart_backlog_frontmatter "$f")"
    # Same normalisation the importer applies: lowercase, and '_' and ' '
    # both fold to '-', so not_started / not started / not-started all match.
    st="$(sed -n 's/^status:[[:space:]]*\([^#]*\).*/\1/p' <<<"$fm" | head -n 1 \
          | tr -d '"'"'"'' | tr '[:upper:]' '[:lower:]' | tr '_ ' '--' \
          | sed 's/[[:space:]]*$//')"
    case "$st" in
      not-started|in-progress) n=$((n + 1)) ;;
    esac
  done < <(_coldstart_backlog_task_files "$dir")
  echo "$n"
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

# Per-file diagnosis for the incomplete branch: name each file the importer
# would have read and what it was missing. Capped, so a 400-file backlog
# reports a readable sample instead of a wall.
_coldstart_backlog_diagnose() {
  local dir="$1" f why shown=0 total=0
  local overviews
  overviews="$(_coldstart_backlog_overviews "$dir")"

  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    total=$((total + 1))
    if [[ "$shown" -lt 8 ]]; then
      why="$(_coldstart_backlog_missing_fields "$f")"
      echo "    ${f#"$dir"/} — $why"
      shown=$((shown + 1))
    fi
  done < <(_coldstart_backlog_task_files "$dir")

  if [[ "$total" -eq 0 ]]; then
    if [[ -n "$overviews" ]]; then
      local n
      n="$(grep -c . <<<"$overviews")"
      echo "    $n epic.md overview(s) and no task files at all"
    fi
  elif [[ "$total" -gt "$shown" ]]; then
    echo "    … and $((total - shown)) more task file(s) with the same kind of problem"
  fi
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
      local importable
      importable="$(_coldstart_backlog_importable_count "$dir")"
      echo "    $importable task file(s) carry status: not-started or in_progress —"
      echo "    those are the ones --resume would turn into Discussions"
      if [[ "$importable" -eq 0 ]]; then
        echo ""
        echo "  So --resume would create nothing: every task file here is already"
        echo "  completed or superseded. For a finished backlog that is the correct"
        echo "  result, not a fault — but it is the same exit-0-having-done-nothing"
        echo "  you would get from a broken backlog, so it is worth saying. The"
        echo "  status: field is what decides it."
        echo ""
        echo "  Nothing written."
      else
        echo "  Nothing written. Re-run with --resume to seed Discussions from them."
      fi
      ;;
    incomplete)
      echo "  $dir is laid out like an epic backlog, but nothing in it would be imported."
      _coldstart_backlog_describe "$dir"
      _coldstart_backlog_diagnose "$dir"
      echo ""
      echo "  Seeding this as it stands imports 0 tasks and exits 0 — silently,"
      echo "  with no error naming the files it skipped. That is the failure this"
      echo "  check exists to catch, so do not reach for --resume first."
      echo ""
      echo "  What the seeding step reads is a task file: epic-<N>-<slug>/01.md and"
      echo "  up, each opening with YAML frontmatter carrying epic, task, title,"
      echo "  type and status. An epic.md on its own is an overview — the importer"
      echo "  can make a Discussion from one, but only under --include-empty-epics,"
      echo "  and the seeding step does not pass that flag."
      echo ""
      echo "  The format, and what each field turns into:"
      echo "    $tpl/README.md"
      echo "  Check the result before you seed, without touching GitHub:"
      echo "    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name> --dry-run"
      echo ""
      echo "  Nothing was written, moved, or deleted."
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
