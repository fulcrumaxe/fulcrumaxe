#!/usr/bin/env bash
# scripts/lib/subshell-mutation-scan.sh — flag x="$(f)" (or x=`f`) where f is
# a shell function that mutates a variable/array the caller expects to see.
#
# Lives in scripts/lib/, not scripts/ci/, on purpose: scripts/ci/run-guards.sh
# auto-discovers and unconditionally runs every top-level file in scripts/ci/
# (see that file's own header), which is the right default for a guard with
# no pre-existing findings but is exactly wrong for a scanner whose first
# real-tree run found 14 call sites, 13 of them genuine and pre-existing.
# Wiring a scanner with known findings straight into that unconditional set
# turns the required check permanently red until every finding is fixed — the
# over-blocking failure CLAUDE.md warns guardrails against. The fix is the
# same ratchet shape scripts/ci/ruff-ratchet.py already uses for `ruff`: a
# thin, baseline-comparing guard (scripts/ci/subshell-mutation-ratchet.py)
# is what run-guards.sh discovers, and it invokes THIS file as the actual
# scanner — the same relationship `ruff-ratchet.py` has to the external `ruff`
# binary, except this scanner is a file in this repo rather than an installed
# tool, so it has to sit somewhere run-guards.sh's maxdepth-1 directory
# listing does not reach. This file's own scanning logic, output format, and
# exit codes (0/1/2, documented below) are unchanged from when this lived at
# scripts/ci/subshell-mutation-guard.sh — only the path moved (D#2512 fix
# round). tests/test_subshell_mutation_guard.sh exercises it directly here.
#
# Command substitution always forks a subshell to run its right-hand side.
# Only f's stdout survives back into the caller — any variable or array f
# assigns, however it assigns it (a bare VAR=, an `export VAR=`, or a
# VAR+=(...) append), is set inside that subshell and is gone the instant
# the subshell exits. This exact shape has been hand-fixed three separate
# times in this repo (D#2512):
#
#   1. blackboard_scratch_state_dir() (tests/lib/blackboard-fixture.sh) —
#      documented in CLAUDE.md: it "must be called directly, not via command
#      substitution... the export it does would run in a subshell and be
#      lost the instant that subshell exits."
#   2. scripts/lib/coldstart-backlog.sh — a global refusal-reason variable
#      set with a bare VAR= assignment inside a function, lost at every
#      x="$(...)" call site until fixed in PR #55.
#   3. mkfixture() in tests/test_coldstart_backlog_template.sh — a
#      FIXTURES+=("$d") array append that never survived any of its 19
#      D="$(mkfixture)" call sites, a leak that went unnoticed until measured.
#
# A static check would have caught all three before they shipped. See the
# Discussion for the full history; this file does not restate it.
#
# What this checks
# -----------------
# Two passes over every *.sh file under the target directory:
#
#   1. Find function definitions (`name() { ... }` or `function name { ... }`,
#      opening brace on the header line). Within a function body, a
#      statement is a mutation if it is a `NAME+=(` array append, an
#      `export NAME=...`, or a bare `NAME=...` assignment that is not itself
#      a per-command environment prefix (`VAR=val cmd`, see below) — and
#      NAME was not declared `local` earlier in that same function body. A
#      function with at least one such statement is "mutating"; the first
#      mutating statement found is kept as its evidence (file, line, var,
#      reason). A statement inside an explicit `( ... )` subshell grouping
#      (see below) never counts, and a function name defined more than once
#      anywhere in the tree is dropped entirely (see below) — both before
#      pass 2 ever looks at a call site.
#   2. Scan every statement (a line, or a `;`-separated segment of a line —
#      quote- and paren-aware, so a `;` inside a string or a `$(...)` does
#      not split — with a leading `local` stripped first since a local
#      *target* doesn't change whose mutation is lost) for a call site
#      shaped `VAR="$(NAME ...)"` or `` VAR=`NAME ...` `` where NAME is a
#      mutating function from pass 1. Each match is a finding.
#
# Scope, deliberately narrow (D#2512 Spec constraint — start narrow, widen
# only if it proves useful without noise). Measured on this tree at
# fulcrumaxe/fulcrumaxe main@023726e8: an early version with none of the
# refinements below flagged 437 call sites across 411 *.sh files, almost all
# of it noise from the four shapes documented here; the refined version
# flags 14, and a manual sample of those is genuine matches of the shape
# this guard defines itself to catch (see the PR body for the count and the
# one identified remaining false positive):
#   - Only `NAME+=(` and a non-`local` `NAME=` inside a function body count
#     as a mutation. `((NAME++))`, `let NAME++`, and comparison operators
#     (`==`, `!=`, `<=`, `>=`) are not `NAME=` and are never treated as one —
#     which is also how a pure log-counter increment stays unflagged, without
#     this guard trying to track whether the counter is ever read back.
#   - A bare `NAME=value` is a mutation only when nothing else follows the
#     value on that statement. `PATH="$MOCK_BIN:$PATH" bash "$JOB_SCRIPT"`
#     (or the same thing backslash-continued across several lines, one
#     `VAR=value \` per line) is bash's per-command environment prefix — it
#     never touches the calling shell's own PATH — and is not a mutation.
#     `export NAME=value` is exempt from this check: `export` is a real
#     command, so trailing content after one export is another real export,
#     not a command about to run.
#   - A statement that is exactly `(` opens, and one that is exactly `)`
#     closes, an explicit subshell grouping. A mutation written inside one
#     is already contained regardless of how the enclosing function is
#     invoked, so it is not this guard's concern. Deliberately narrow: a
#     self-contained one-liner like `(cd foo && bar)` (open and close on the
#     same statement) is not touched at all, so it is still scanned as
#     ordinary top-level content.
#   - A direct call (`f`, no assignment) is never a call-site finding —
#     nothing is being captured, so nothing is lost.
#   - Comment lines (`#...`) are never scanned as code, in either pass — a
#     guard whose own header comments (like the ones above, which quote the
#     exact flagged shapes) would trip it is unusable.
#   - A heredoc body (`<<EOF` / `<<'EOF'` / `<<-EOF` through its terminator
#     line) is never scanned as code — it commonly embeds Python/JSON/YAML,
#     whose own `{`/`}`/`;`/`=` would otherwise corrupt both brace-depth
#     tracking and statement scanning. The opening line is still scanned
#     normally, since real code can precede the `<<...` token on it.
#   - A function name defined more than once anywhere in the tree (a
#     same-named `new_fixture`-style helper redefined per test file, common
#     in this repo) is dropped from consideration entirely, in both
#     directions: pass 1 never treats it as mutating and pass 2 never flags
#     a call to it. Attributing one file's call site to a different file's
#     unrelated same-named function would be worse than missing it.
#   - Statement splitting and the value-token scan are quote- and
#     paren-aware for a *single physical line*, but quote state is not
#     carried across lines. A single- or double-quoted string that spans
#     multiple lines (for example a multi-line `bash -c '...'` script
#     embedded inline, not as a heredoc) has its literal contents read as
#     real statements of the enclosing function. Known, not fixed — see the
#     PR body for the one instance this surfaced.
#
# Usage: bash scripts/lib/subshell-mutation-scan.sh [target-dir]  (default: repo root)
#
# Exit 0 = no call site loses a mutation (scope line reports what was scanned).
# Exit 1 = one or more findings, each printed as a "FAIL: ..." line naming the
#          call site's file and line, the mutating function, and where/how it
#          mutates.
# Exit 2 = usage/argument error, or zero *.sh files found under target-dir —
#          a guard that scanned nothing must not report success.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "error: target dir not found: $TARGET_DIR" >&2
  exit 2
fi
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

mapfile -t FILES < <(find "$TARGET_DIR" -type f -name '*.sh' \
  -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/archive/*' \
  | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "error: no *.sh files found under $TARGET_DIR" >&2
  exit 2
fi

# name -> "file<TAB>line<TAB>varname<TAB>reason" for the first mutation found
declare -A MUTATING=()
declare -A LOCALVARS=()
# name -> how many distinct function definitions with this name pass 1 saw,
# across the whole tree. A name defined more than once (e.g. a same-named
# `new_fixture` helper redefined per test file, 9 times in this tree) cannot
# be resolved to "the" definition a given call site means, so any such name
# is dropped from MUTATING below rather than risk attributing one file's
# call site to a different file's unrelated same-named function.
declare -A DEF_COUNT=()

# Sets _SMG_OUT rather than printing + being captured via $(...): this runs
# once per statement across every file, and $(...) forks a subshell on every
# call — measured as the dominant cost of an earlier version of this script
# (a full-tree run went from several minutes to well under one after
# switching every hot helper in this file to the set-a-global convention).
_smg_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  _SMG_OUT="$s"
}

# Split a line into ';'-separated statements, but never split on a ';' that
# is inside a single- or double-quoted string or inside a $(...) / (...)
# span. Without this, a one-liner like
#   count=$(echo "$out" | python3 -c "import json,sys; prs=json.load(sys.stdin)")
# has its embedded Python ";" read as a bash statement separator, and
# "prs=json.load(sys.stdin)" then reads as a bash assignment to a global
# named prs — a false mutation on code that was never bash. Measured against
# this tree: without quote/paren awareness this guard reported 437 findings
# on 411 files; the dominant cause was exactly this shape.
#
# Two fast paths cover the common cases without the character-by-character
# scan: no ';' at all needs no split, and ';' with no quote and no paren
# anywhere in the line is unambiguous to split plainly. Everything else pays
# for one pass over the line's characters.
_smg_split_statements() {
  local text="$1"
  if [[ "$text" != *';'* ]]; then
    printf '%s\n' "$text"
    return
  fi
  if [[ "$text" != *'"'* && "$text" != *"'"* && "$text" != *'('* ]]; then
    local -a parts
    IFS=';' read -ra parts <<< "$text"
    printf '%s\n' "${parts[@]}"
    return
  fi

  local -a out=()
  local buf="" in_squote=0 in_dquote=0 pdepth=0
  local i len ch prevch=""
  len=${#text}
  for (( i = 0; i < len; i++ )); do
    ch="${text:i:1}"
    if [[ $in_squote -eq 1 ]]; then
      buf+="$ch"
      [[ "$ch" == "'" ]] && in_squote=0
      prevch="$ch"
      continue
    fi
    if [[ $in_dquote -eq 1 ]]; then
      buf+="$ch"
      if [[ "$ch" == '"' && "$prevch" != '\' ]]; then
        in_dquote=0
      fi
      prevch="$ch"
      continue
    fi
    case "$ch" in
      "'") in_squote=1; buf+="$ch" ;;
      '"') in_dquote=1; buf+="$ch" ;;
      '(') pdepth=$((pdepth + 1)); buf+="$ch" ;;
      ')') (( pdepth > 0 )) && pdepth=$((pdepth - 1)); buf+="$ch" ;;
      ';')
        if [[ $pdepth -eq 0 ]]; then
          out+=("$buf"); buf=""
        else
          buf+="$ch"
        fi
        ;;
      *) buf+="$ch" ;;
    esac
    prevch="$ch"
  done
  out+=("$buf")
  printf '%s\n' "${out[@]}"
}

# True (prints 1) if TEXT — everything after a matched `NAME=` — has a
# second, unquoted word after its value token: `VAR="x" cmd args` or a
# backslash-continued `VAR="x" \` (the continuation backslash is itself a
# non-whitespace leftover). That shape is bash's per-command environment
# prefix (`VAR=val cmd`, one or several, line-continued or not) — the
# assignment lives only in the forked command's environment and is never a
# mutation of the calling shell's own state, so it must not be treated as
# one. Prints 0 when the value token is the whole thing (a real assignment).
# This is why the dial-summary job's
#   PATH="$MOCK_BIN:$PATH" \
#   REPO_ROOT="$TMPDIR_TEST" \
#       bash "$JOB_SCRIPT" ...
# does not flag PATH or REPO_ROOT as mutations: each physical line's own
# value token is followed by a trailing backslash, which this function
# already treats as "something else follows" without needing to join
# backslash-continued lines into one logical statement first.
_smg_has_trailing_word() {
  local text="$1"
  local len=${#text} i=0 ch prevch=""
  local in_squote=0 in_dquote=0 pdepth=0
  local end=-1
  for (( i = 0; i < len; i++ )); do
    ch="${text:i:1}"
    if [[ $in_squote -eq 1 ]]; then
      [[ "$ch" == "'" ]] && in_squote=0
      prevch="$ch"; continue
    fi
    if [[ $in_dquote -eq 1 ]]; then
      if [[ "$ch" == '"' && "$prevch" != '\' ]]; then in_dquote=0; fi
      prevch="$ch"; continue
    fi
    if [[ $pdepth -gt 0 ]]; then
      case "$ch" in
        "'") in_squote=1 ;;
        '"') in_dquote=1 ;;
        '(') pdepth=$((pdepth + 1)) ;;
        ')') pdepth=$((pdepth - 1)) ;;
      esac
      prevch="$ch"; continue
    fi
    case "$ch" in
      "'") in_squote=1; prevch="$ch"; continue ;;
      '"') in_dquote=1; prevch="$ch"; continue ;;
      '(') pdepth=$((pdepth + 1)); prevch="$ch"; continue ;;
      ' ') end=$i; break ;;
      $'\t') end=$i; break ;;
    esac
    prevch="$ch"
  done
  # Sets _SMG_OUT to "0" or "1" (see the file-level comment on _smg_trim for
  # why this is a global-set, not a $(...)-captured return).
  if [[ $end -eq -1 ]]; then
    _SMG_OUT="0"
    return
  fi
  _smg_trim "${text:end}"
  if [[ -z "$_SMG_OUT" ]]; then
    _SMG_OUT="0"
  else
    _SMG_OUT="1"
  fi
}

# Sets _SMG_HEREDOC_DELIM and _SMG_HEREDOC_DASH (1 or 0) if LINE opens a
# heredoc (`<<EOF`, `<<'EOF'`, `<<-EOF`, ...); clears _SMG_HEREDOC_DELIM to
# "" otherwise. `$((1<<2))` and other bit-shifts don't match: the character
# right after `<<`/`<<-` has to start an identifier, which a digit never
# does. Global-set for the same reason as _smg_trim above.
_smg_detect_heredoc_start() {
  local line="$1"
  if [[ "$line" =~ \<\<-?[[:space:]]*[\"\']?([A-Za-z_][A-Za-z0-9_]*) ]]; then
    _SMG_HEREDOC_DELIM="${BASH_REMATCH[1]}"
    _SMG_HEREDOC_DASH=0
    [[ "$line" == *"<<-"* ]] && _SMG_HEREDOC_DASH=1
  else
    _SMG_HEREDOC_DELIM=""
  fi
}

# ---------------------------------------------------------------------------
# Pass 1 — collect mutating functions.
# ---------------------------------------------------------------------------
for file in "${FILES[@]}"; do
  in_func=0
  depth=0
  func_name=""
  mutated=0
  mut_line=0
  mut_var=""
  mut_reason=""
  LOCALVARS=()
  lineno=0
  in_heredoc=0
  heredoc_delim=""
  heredoc_dash=0

  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))

    # Heredoc bodies are never bash statements -- a heredoc commonly embeds
    # Python/JSON/YAML/SQL, whose own `{`/`}`/`;`/`=` would otherwise corrupt
    # both brace-depth tracking and statement scanning (that is exactly how
    # this guard's own test fixtures, written as heredocs in this file,
    # would misread as real findings against themselves). Skip everything
    # between a heredoc's opening line and its terminator line, inclusive of
    # the terminator; the opening line itself is still scanned normally,
    # since it can carry real code before the `<<...` token.
    if [[ $in_heredoc -eq 1 ]]; then
      check_line="$line"
      if [[ $heredoc_dash -eq 1 && "$line" =~ ^$'\t'*(.*)$ ]]; then
        check_line="${BASH_REMATCH[1]}"
      fi
      if [[ "$check_line" == "$heredoc_delim" ]]; then
        in_heredoc=0
      fi
      continue
    fi
    _smg_detect_heredoc_start "$line"
    if [[ -n "$_SMG_HEREDOC_DELIM" ]]; then
      heredoc_delim="$_SMG_HEREDOC_DELIM"
      heredoc_dash="$_SMG_HEREDOC_DASH"
      in_heredoc=1
    fi

    if [[ $in_func -eq 0 ]]; then
      if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*\(\)[[:space:]]*\{ ]]; then
        func_name="${BASH_REMATCH[1]}"
      elif [[ "$line" =~ ^[[:space:]]*function[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*\{ ]]; then
        func_name="${BASH_REMATCH[1]}"
      else
        continue
      fi
      DEF_COUNT["$func_name"]=$(( ${DEF_COUNT["$func_name"]:-0} + 1 ))
      in_func=1
      mutated=0
      mut_line=0
      mut_var=""
      mut_reason=""
      LOCALVARS=()
      pgroup_depth=0
      # Everything after the opening brace, on this same line, is body text
      # (covers single-line functions like `f() { local d; d=...; }`, whose
      # own closing brace lives in that same body_text and must be counted
      # below like any other line — the header's opening brace is the only
      # brace NOT in body_text, which is why depth starts at 1 rather than 0).
      body_text="${line#*\{}"
      depth=1
    else
      body_text="$line"
    fi
    opens="${body_text//[^\{]/}"
    closes="${body_text//[^\}]/}"
    depth=$((depth + ${#opens} - ${#closes}))

    mapfile -t STATEMENTS < <(_smg_split_statements "$body_text")
    for raw_stmt in "${STATEMENTS[@]}"; do
      _smg_trim "$raw_stmt"
      stmt="$_SMG_OUT"
      [[ -z "$stmt" ]] && continue
      [[ "$stmt" == \#* ]] && continue

      # A statement that is exactly `(` or `)` opens/closes an explicit
      # subshell grouping ( commands ) — a common convention in this tree
      # for isolating a block's side effects from the rest of the function
      # (both `_run_status()` in tests/test_ci_status_check.sh and
      # `run_verify_only()` in tests/test_post_agent_hook_pr_verify.sh wrap
      # their whole body this way, with a comment saying so). A mutation
      # written inside that grouping is already contained regardless of how
      # the function itself gets called, so it is not this guard's concern
      # — only a mutation reachable at the function's own top level is.
      # Deliberately narrow: only a BARE `(`/`)` statement counts, so a
      # self-contained one-liner like `(cd foo && bar)` (open and close on
      # the same statement) is not touched at all, and `((x++))` never
      # matches either (it starts with two parens, not one).
      if [[ "$stmt" == "(" ]]; then
        pgroup_depth=$((pgroup_depth + 1))
        continue
      fi
      if [[ "$stmt" == ")" ]]; then
        (( pgroup_depth > 0 )) && pgroup_depth=$((pgroup_depth - 1))
        continue
      fi
      if [[ $pgroup_depth -gt 0 ]]; then
        continue
      fi

      if [[ "$stmt" =~ ^local[[:space:]]+(.+)$ ]]; then
        rest="${BASH_REMATCH[1]}"
        for tok in $rest; do
          name="${tok%%=*}"
          [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && LOCALVARS["$name"]=1
        done
        continue
      fi

      if [[ "$stmt" =~ ^([A-Za-z_][A-Za-z0-9_]*)\+=\( ]]; then
        name="${BASH_REMATCH[1]}"
        if [[ -z "${LOCALVARS[$name]:-}" && $mutated -eq 0 ]]; then
          mutated=1; mut_line=$lineno; mut_var="$name"
          mut_reason="array append '${name}+=(...)' without local"
        fi
        continue
      fi

      if [[ "$stmt" =~ ^export[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)= ]]; then
        name="${BASH_REMATCH[1]}"
        if [[ -z "${LOCALVARS[$name]:-}" && $mutated -eq 0 ]]; then
          mutated=1; mut_line=$lineno; mut_var="$name"
          mut_reason="export assignment 'export ${name}=' without local"
        fi
        continue
      fi

      if [[ "$stmt" =~ ^(declare|readonly|typeset)[[:space:]] ]]; then
        continue
      fi

      if [[ "$stmt" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        name="${BASH_REMATCH[1]}"
        value_and_rest="${BASH_REMATCH[2]}"
        if [[ -z "${LOCALVARS[$name]:-}" && $mutated -eq 0 ]]; then
          _smg_has_trailing_word "$value_and_rest"
          if [[ "$_SMG_OUT" == "0" ]]; then
            mutated=1; mut_line=$lineno; mut_var="$name"
            mut_reason="bare assignment '${name}=' without local"
          fi
        fi
        continue
      fi
    done

    if [[ $depth -le 0 ]]; then
      if [[ $mutated -eq 1 ]]; then
        MUTATING["$func_name"]="${file}"$'\t'"${mut_line}"$'\t'"${mut_var}"$'\t'"${mut_reason}"
      fi
      in_func=0
    fi
  done < "$file"
done

# Ambiguous names (more than one definition anywhere in the tree) are
# dropped before pass 2 even looks at call sites — see DEF_COUNT's comment
# above.
for name in "${!MUTATING[@]}"; do
  if [[ "${DEF_COUNT[$name]:-0}" -gt 1 ]]; then
    unset "MUTATING[$name]"
  fi
done

# ---------------------------------------------------------------------------
# Pass 2 — scan for call sites that capture a mutating function via $(...) or
# backticks.
# ---------------------------------------------------------------------------
FINDINGS=0

for file in "${FILES[@]}"; do
  lineno=0
  in_heredoc=0
  heredoc_delim=""
  heredoc_dash=0

  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))

    # Same heredoc-skip as pass 1 -- see that loop's comment. A call site
    # can never be inside a heredoc body (it would be Python/JSON/etc. text,
    # not bash), and skipping keeps this pass's own findings limited to real
    # code the same way it does there.
    if [[ $in_heredoc -eq 1 ]]; then
      check_line="$line"
      if [[ $heredoc_dash -eq 1 && "$line" =~ ^$'\t'*(.*)$ ]]; then
        check_line="${BASH_REMATCH[1]}"
      fi
      if [[ "$check_line" == "$heredoc_delim" ]]; then
        in_heredoc=0
      fi
      continue
    fi
    _smg_detect_heredoc_start "$line"
    if [[ -n "$_SMG_HEREDOC_DELIM" ]]; then
      heredoc_delim="$_SMG_HEREDOC_DELIM"
      heredoc_dash="$_SMG_HEREDOC_DASH"
      in_heredoc=1
    fi

    mapfile -t STATEMENTS < <(_smg_split_statements "$line")
    for raw_stmt in "${STATEMENTS[@]}"; do
      _smg_trim "$raw_stmt"
      stmt="$_SMG_OUT"
      [[ -z "$stmt" ]] && continue
      [[ "$stmt" == \#* ]] && continue

      # A leading `local` on the statement doesn't change whose mutation is
      # lost, so strip it: `local x="$(f)"` is still a real call site.
      if [[ "$stmt" =~ ^local[[:space:]]+(.*)$ ]]; then
        stmt="${BASH_REMATCH[1]}"
      fi

      # No \b here: bash's [[ =~ ]] (glibc ERE) does not treat \b as a word
      # boundary, so the identifier class alone (which already stops at the
      # first non-word character) is what bounds the match.
      call_name=""
      if [[ "$stmt" =~ ^[A-Za-z_][A-Za-z0-9_]*=\"?\$\(([A-Za-z_][A-Za-z0-9_]*) ]]; then
        call_name="${BASH_REMATCH[1]}"
      elif [[ "$stmt" =~ ^[A-Za-z_][A-Za-z0-9_]*=\"?\`([A-Za-z_][A-Za-z0-9_]*) ]]; then
        call_name="${BASH_REMATCH[1]}"
      fi

      [[ -z "$call_name" ]] && continue
      [[ -z "${MUTATING[$call_name]:-}" ]] && continue

      IFS=$'\t' read -r def_file def_line def_var def_reason <<< "${MUTATING[$call_name]}"
      def_rel="${def_file#"$TARGET_DIR"/}"
      call_rel="${file#"$TARGET_DIR"/}"
      echo "FAIL: ${call_rel}:${lineno}: \`${stmt}\` captures \$(${call_name} ...) in a subshell, but ${call_name} (${def_rel}:${def_line}) mutates '${def_var}' — ${def_reason} — that mutation is lost when the subshell exits"
      FINDINGS=$((FINDINGS + 1))
    done
  done < "$file"
done

echo
echo "subshell-mutation-guard: scanned ${#FILES[@]} *.sh file(s) under $TARGET_DIR, ${#MUTATING[@]} mutating function(s), ${FINDINGS} call-site finding(s)"

if [[ $FINDINGS -gt 0 ]]; then
  exit 1
fi
exit 0
