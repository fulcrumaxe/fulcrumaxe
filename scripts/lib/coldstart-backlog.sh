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
# the frontmatter field the importer actually treats as load-bearing, and the
# incomplete branch says the quiet part out loud instead of pointing at
# --resume.
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
# A standing warning for whoever edits this file next. Every judgement here
# about "is there work to do" has been wrong at some point — the format the
# operator was told to write, the check that decided a backlog conformed, the
# message it printed, the branch for a finished backlog, and the counter that
# branch depends on. Five, and the test suite was green for every one of them.
# The trap is structural, not incidental: this module restates what
# scripts/import-epic-tasks.py will do, and a restatement drifts silently
# because both sides still run and both still exit 0. D#2451 measured three
# more divergences on top of those five, all with the same shape.
#
# So the standard for a change here is not "the tests pass". It is: run the
# real importer on the same tree and check the two answers match — or, better,
# stop restating and ask it directly.
#
# --- D#2451: what still restates the importer, and what does not anymore ---
#
# The status filter (how many task files the importer would actually act on)
# used to be a bash regex parsing `status:` out of the frontmatter by hand.
# That regex is gone. _coldstart_backlog_importable_count now runs
# scripts/import-epic-tasks.py --dry-run on the tree and reads its own
# "After status filter: N" line — see _coldstart_backlog_importer_status_count
# below for the mechanics and the fail-closed contract on subprocess failure.
# --dry-run needs no network and no `gh` credentials (every gh() call site in
# import-epic-tasks.py sits behind `if not dry_run:`), and it computes that
# line on the identical code path a real run does (the print is above the
# `if not dry_run:` branch), so it is legitimate evidence about the real run
# rather than a mode of its own (D#2149).
#
# What is NOT routed through the importer, and why: whether a task file is
# well-formed enough to call this backlog "conforming" is still decided
# locally, by _coldstart_backlog_missing_fields. That is not the same question
# as the status filter, and the importer has no answer for it — it never
# rejects a file for a missing epic/task/title/type, it just falls back to "?"
# and "untitled" and creates a worse-looking Discussion anyway. So "conforming"
# cannot be "the importer would act on this" (item 3's question); it has to
# stay a local, structural check — but D#2451 found that check demanding the
# wrong things: it required epic, task, title, type AND status, when `status`
# is the only field the importer's own behaviour treats as load-bearing (an
# absent-or-non-matching status is what the status filter silently drops; a
# missing epic/task/title just produces a placeholder title, not a skip). That
# mismatch is fixed by shrinking _COLDSTART_BACKLOG_REQUIRED_FIELDS to
# (status) — see the comment there. The broader field list survives as
# _COLDSTART_BACKLOG_SIGNAL_FIELDS, used only to tell "our layout, incomplete"
# from "someone else's format" in the diagnostic branches — a question the
# importer was never going to answer either, since it has no notion of "does
# this look like an attempt at our shape".
#
# The symlinked-epic-directory misclassification (item 8) is not a restatement
# bug at all — plain `find` does not descend into a symlinked directory, so a
# backlog reachable only through one used to read as "no markdown here" and
# get scaffolded into, next to real content `find` never saw. Routing the
# status filter through the importer does not fix this (the importer follows
# the symlink correctly, but classify's own emptiness/structural finds do not
# run the importer at all): fixed directly, with `-L`, everywhere this module
# lists files.
#
# No network calls, no gh calls.

set -euo pipefail

_BACKLOG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_BACKLOG_REPO_ROOT="$(cd "$_BACKLOG_LIB_DIR/../.." && pwd)"

COLDSTART_EPIC_TEMPLATE_DIR="${COLDSTART_EPIC_TEMPLATE_DIR:-$_BACKLOG_REPO_ROOT/scripts/coldstart-templates/epic}"

# ---------------------------------------------------------------------------
# The one field import-epic-tasks.py actually treats as load-bearing before a
# task file becomes a Discussion. A file missing it is not rejected loudly —
# it is dropped by the status filter, silently, exit 0, with nothing printed
# about the file it skipped. That silence is the failure mode this whole
# module exists to make loud, so a file missing `status` is not conforming no
# matter how good the rest of it looks.
#
# epic/task/title/type are NOT in this list, on purpose (D#2451 item 1). The
# importer never rejects a file for missing them — format_title() falls back
# to "?"/"untitled" and the Discussion is still created if status matches.
# The old five-field list called such a file "incomplete" ("nothing here
# would import"), which is exactly the kind of confident-and-wrong report
# this whole change exists to stop making. If it carries `status`, the
# importer would act on it, so this module now agrees.
_COLDSTART_BACKLOG_REQUIRED_FIELDS=(status)

# The broader field set used ONLY to tell "this is our layout, just
# incomplete" from "this is someone else's format" (_coldstart_backlog_has_
# partial_frontmatter). That is not a question about import eligibility, so
# it does not shrink with _COLDSTART_BACKLOG_REQUIRED_FIELDS above — a file
# carrying `epic:` and `title:` but no `status:` is obviously an attempt at
# our shape, even though it is not (yet) conforming.
_COLDSTART_BACKLOG_SIGNAL_FIELDS=(epic task title type status)

# Prints a file's YAML frontmatter block, or nothing.
#
# Returns nothing when there is no closing `---` fence, which is exactly when
# import-epic-tasks.py's parse_frontmatter gives up too — the two must agree
# about what counts as frontmatter or this check is worth less than nothing.
#
# This one restatement of parse_frontmatter survives on purpose (D#2451 item
# 6): it backs only the diagnostic branches (which fields is this file
# missing, does it look like an attempt at our shape) — the importer has no
# equivalent surface (it never explains why a file was skipped), so there is
# no "ask it instead" available here. It does NOT decide import eligibility;
# _coldstart_backlog_importable_count asks the importer for that instead of
# re-parsing status itself.
_coldstart_backlog_frontmatter() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  [[ "$(head -n 1 "$f" 2>/dev/null)" == "---" ]] || return 0
  tail -n +2 "$f" 2>/dev/null | grep -qE '^---[[:space:]]*$' || return 0
  sed -n '2,/^---[[:space:]]*$/p' "$f" 2>/dev/null
}

# Prints a human phrase naming what is wrong with a task file, or nothing at
# all when the file carries every field in _COLDSTART_BACKLOG_REQUIRED_FIELDS.
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
#
# `-L`: an epic-<N>-<slug> directory can itself be a symlink (D#2451 item 8).
# Plain find does not descend into a symlinked directory while walking, which
# used to make a backlog reachable only through one read as having no task
# files at all — while import-epic-tasks.py's Path.glob()/Path.is_dir() both
# follow the same symlink without issue. Following it here is what makes this
# module's own view agree with the importer's.
_coldstart_backlog_task_files() {
  find -L "$1" -mindepth 2 -maxdepth 2 -type f -name '*.md' -path '*/epic-*/*' \
    ! -name 'epic.md' 2>/dev/null | sort
}

_coldstart_backlog_overviews() {
  find -L "$1" -mindepth 2 -maxdepth 2 -type f -name 'epic.md' -path '*/epic-*/*' 2>/dev/null | sort
}

# True when at least one task file carries a *---fenced* frontmatter block
# with at least one of _COLDSTART_BACKLOG_SIGNAL_FIELDS in it — i.e. someone
# was aiming at this format and missed. No frontmatter anywhere and no
# epic.md means they are using a different format entirely, and the two
# deserve different answers. Deliberately checks the broader signal set, not
# _COLDSTART_BACKLOG_REQUIRED_FIELDS: a file with `epic:`/`title:` but no
# `status:` is obviously an attempt at our shape even though it is not
# conforming, and by the time this function runs, classify has already
# confirmed no task file conforms.
_coldstart_backlog_has_partial_frontmatter() {
  local dir="$1" f fm k
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    fm="$(_coldstart_backlog_frontmatter "$f")"
    [[ -n "$fm" ]] || continue
    for k in "${_COLDSTART_BACKLOG_SIGNAL_FIELDS[@]}"; do
      grep -qE "^${k}:" <<<"$fm" && return 0
    done
  done < <(_coldstart_backlog_task_files "$dir")
  return 1
}

# ---------------------------------------------------------------------------
# Ask the importer, instead of restating it (D#2451 items 3-5).
#
# Runs `import-epic-tasks.py --dry-run` on $1 and prints the integer from its
# own "After status filter: N task(s) to process" line. That line is computed
# on the same code path a real (non-dry-run) run uses — see the header
# comment — so this is not a preview standing in for the real answer, it IS
# the real answer, on this tree, right now.
#
# import-epic-tasks.py hardcodes <repo_path>/epics as the tree it walks. $1
# is whatever --backlog resolved to and need not be named "epics" or sit
# under anything the importer would recognise as a repo root, so this points
# the importer at a throwaway proxy directory whose "epics" entry is a
# symlink to the real one. Path.is_dir()/Path.glob() both follow that
# symlink, which is also why a symlinked EPIC subdirectory (item 8) is read
# correctly through this path.
#
# Fails closed on every failure mode: missing python3, missing timeout(1), a
# non-zero exit, a timeout, or output that does not carry the expected line.
# Each sets _COLDSTART_BACKLOG_REFUSAL_REASON and returns 1 rather than
# printing a guessed number — a wrong "0" here is indistinguishable from a
# real empty backlog, and this module exists specifically to stop producing
# that kind of confident-and-wrong answer.
_COLDSTART_BACKLOG_IMPORTER_TIMEOUT="${COLDSTART_BACKLOG_IMPORTER_TIMEOUT:-30}"
# import-epic-tasks.py requires --repo, but nothing in its --dry-run path
# reads it: get_repo_node_id/list_existing_discussion_titles/ensure_label's
# network call/create_discussion's network call all sit behind `if not
# dry_run:` (or an equivalent early return), so this placeholder is never
# split, queried, or otherwise inspected for a dry run.
_COLDSTART_BACKLOG_DUMMY_REPO="dry-run/dry-run"
_COLDSTART_BACKLOG_REFUSAL_REASON=""
# Set on success, alongside the stdout echo, so a caller that must not lose
# _COLDSTART_BACKLOG_REFUSAL_REASON to a subshell (see the comment on
# _coldstart_backlog_importable_count_into_globals below) can read the count
# back without wrapping this call in $(...) at all.
_COLDSTART_BACKLOG_LAST_COUNT=""

_coldstart_backlog_importer_status_count() {
  local dir="$1"
  _COLDSTART_BACKLOG_REFUSAL_REASON=""
  _COLDSTART_BACKLOG_LAST_COUNT=""

  local abs_dir
  abs_dir="$(cd "$dir" 2>/dev/null && pwd)" || {
    _COLDSTART_BACKLOG_REFUSAL_REASON="backlog directory vanished before it could be asked about: $dir"
    return 1
  }

  if ! command -v python3 >/dev/null 2>&1; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="python3 not found on PATH -- cannot ask the importer"
    return 1
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="timeout(1) not found on PATH -- refusing to run the importer unbounded"
    return 1
  fi

  local importer="$_BACKLOG_REPO_ROOT/scripts/import-epic-tasks.py"
  if [[ ! -f "$importer" ]]; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="importer script not found at $importer"
    return 1
  fi

  local proxy
  proxy="$(mktemp -d 2>/dev/null)" || {
    _COLDSTART_BACKLOG_REFUSAL_REASON="could not create a temp directory to proxy the importer call"
    return 1
  }
  if ! ln -s "$abs_dir" "$proxy/epics" 2>/dev/null; then
    rm -rf "$proxy"
    _COLDSTART_BACKLOG_REFUSAL_REASON="could not create the proxy symlink for $abs_dir"
    return 1
  fi

  local out rc
  # `if out=$(...); then rc=0; else rc=$?; fi`, not a bare assignment followed
  # by `rc=$?` -- this file runs under set -e, and a bare
  # `out="$(cmd_that_can_fail)"` aborts the whole sourcing script the moment
  # cmd exits non-zero, never reaching the rc=$? line below it. That would
  # turn every one of this function's fail-closed paths (D#2451 item 5) into
  # an uncontrolled crash of whatever sourced this file instead of a graceful
  # refusal. Wrapping the assignment in the if-condition is what keeps it
  # from tripping errexit.
  if out="$(timeout "${_COLDSTART_BACKLOG_IMPORTER_TIMEOUT}s" \
        python3 "$importer" "$proxy" --repo "$_COLDSTART_BACKLOG_DUMMY_REPO" --dry-run 2>&1)"; then
    rc=0
  else
    rc=$?
  fi
  rm -rf "$proxy"

  if [[ $rc -eq 124 ]]; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="importer timed out after ${_COLDSTART_BACKLOG_IMPORTER_TIMEOUT}s"
    return 1
  fi
  if [[ $rc -ne 0 ]]; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="importer exited $rc: $(tail -n 1 <<<"$out" | tr -s '[:space:]' ' ')"
    return 1
  fi

  local n
  # `|| true`: same set -e hazard as above. grep exits 1 when the expected
  # line is absent (exactly the "format changed" case this is meant to
  # detect), and with pipefail that makes the whole pipeline -- and so this
  # bare assignment -- "fail". Without the guard, the one failure this
  # function is supposed to catch and report would instead crash past the
  # check below and abort the caller.
  n="$(grep -E '^After status filter: [0-9]+ task' <<<"$out" | head -n 1 \
        | sed -E 's/^After status filter: ([0-9]+) task.*/\1/')" || true
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then
    _COLDSTART_BACKLOG_REFUSAL_REASON="importer output did not carry the expected 'After status filter: N' line -- its format may have changed"
    return 1
  fi

  _COLDSTART_BACKLOG_LAST_COUNT="$n"
  echo "$n"
  return 0
}

# How many task files would survive the importer's DEFAULT status filter
# (not-started, in_progress) — asked, not restated (D#2451 item 3). The name
# and one-argument contract are unchanged from before; what changed is that
# the body no longer parses `status:` itself. A backlog of entirely completed
# tasks is well-formed and imports nothing, which is correct — but saying so
# beats another silent zero.
#
# On failure, prints nothing and returns 1; _COLDSTART_BACKLOG_REFUSAL_REASON
# names the cause. Callers must not treat that as zero.
_coldstart_backlog_importable_count() {
  _coldstart_backlog_importer_status_count "$1"
}

# coldstart_backlog_step's conforming branch cannot call
# _coldstart_backlog_importable_count via `x="$(...)"` and then read
# _COLDSTART_BACKLOG_REFUSAL_REASON on failure -- command substitution forks
# a subshell, so a global set inside it (the refusal reason) never reaches
# the caller once that subshell exits. This wrapper is called WITHOUT
# `$(...)` (its stdout is thrown away; the count comes back via
# _COLDSTART_BACKLOG_LAST_COUNT instead), so both variables it sets stay
# visible to the caller.
#
# Calls _coldstart_backlog_importable_count (not
# _coldstart_backlog_importer_status_count directly) on purpose: that is the
# one public, canonical entry point everything -- this, the tests, and any
# other caller -- goes through, so a regression landed in
# _coldstart_backlog_importable_count's body (the natural place to "fix" a
# bug in it) is visible from every caller, not just direct ones.
_coldstart_backlog_importable_count_into_globals() {
  _coldstart_backlog_importable_count "$1" >/dev/null
}

# ---------------------------------------------------------------------------
# Describe what is there — used by the report-only branches. Prints counts
# with their scope named, never a bare number.
# ---------------------------------------------------------------------------
_coldstart_backlog_describe() {
  local dir="$1"
  local dirs files
  dirs="$(find "$dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
  # -maxdepth 6: generous for any layout worth describing, but bounded --
  # -L already means this follows symlinks, and an unbounded depth on a
  # symlink-following find over a directory this module does not control the
  # contents of (an operator's own backlog dir) is a cycle/escape exposure
  # for no real benefit over a generous bound.
  files="$(find -L "$dir" -maxdepth 6 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
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
# Classify — prints exactly one of:
#   absent | empty | conforming | incomplete | foreign
#
# Conformance is decided by _COLDSTART_BACKLOG_REQUIRED_FIELDS — a directory
# is conforming when at least one task file carries every field in that list.
# That list is (status) (see its own comment for why it shrank from five
# fields): the importer never rejects a file for a missing epic/task/title/
# type, so requiring them here used to report "nothing would import" for
# files the importer would in fact act on.
# ---------------------------------------------------------------------------
coldstart_backlog_classify() {
  local dir="$1"

  [[ -d "$dir" ]] || { echo "absent"; return 0; }

  # -L: see _coldstart_backlog_task_files for why a symlinked epic directory
  # must not read as "no markdown here at all" (D#2451 item 8). -maxdepth 6
  # for the same reason as _coldstart_backlog_describe's: bound the
  # symlink-following walk instead of leaving it unbounded.
  if ! find -L "$dir" -maxdepth 6 -type f -name '*.md' 2>/dev/null | grep -q .; then
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
      # Not `importable="$(_coldstart_backlog_importable_count "$dir")"`: that
      # forks a subshell for the command substitution, and
      # _COLDSTART_BACKLOG_REFUSAL_REASON set inside it would never reach this
      # scope once the subshell exits -- the else branch below would always
      # read empty. Calling the _into_globals form directly keeps both
      # variables it sets in this shell.
      if _coldstart_backlog_importable_count_into_globals "$dir"; then
        importable="$_COLDSTART_BACKLOG_LAST_COUNT"
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
      else
        # D#2451 item 5: fail closed. This branch never scaffolds regardless
        # (only absent/empty do), so a failed subprocess here cannot produce
        # a destructive outcome — but it must still never be misreported as
        # zero importable, so it says exactly why it could not check.
        echo "    Could not ask the importer how many of these carry a matching"
        echo "    status: — $_COLDSTART_BACKLOG_REFUSAL_REASON"
        echo ""
        echo "  Nothing written. Check by hand before assuming either answer:"
        echo "    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name> --dry-run"
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
  #
  # D#2451 item 13: routing the classify/report path above through the
  # importer (via a proxy symlink) does NOT make this warning redundant. The
  # proxy is local to _coldstart_backlog_importer_status_count and is never
  # seen outside it — the actual seeding invocation, scripts/coldstart.sh's
  # run_seed(), calls import-epic-tasks.py with $REPO_PATH and no --backlog
  # equivalent, so it still only ever reads $repo_path/epics. The two
  # disagreeing about where the backlog is remains a real, separate problem,
  # so the warning stays.
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
