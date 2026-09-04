#!/usr/bin/env bash
# scripts/lib/worktree-claims.sh — shared worktree staleness/claim classification (D#1819).
#
# Single source of truth for "does this worktree still claim files in the
# --touchpoints gate", consumed by:
#   - scripts/spawn-agent.sh   (the gate itself)
#   - scripts/sweep-stale-worktrees.sh (informational reporting only — its
#     actual removal predicate is intentionally unchanged, see D#1819 Spec
#     "Explicitly out of scope": widening what gets *deleted* is not part
#     of this fix)
#
# Root cause this replaces: the old gate built `_WT_FILES` from a raw
# `git diff --name-only origin/main` with no check for whether the branch
# had already landed. This repo squash-merges, so a landed branch's
# commits never appear in main's history and the diff never goes empty —
# a merged worktree looked like live in-flight work forever. The old
# pre-filter (commits-behind > threshold) doesn't rescue this either: on
# an idle repo, weeks-old worktrees stay under the commit threshold
# because nothing is landing to put commits between them and main.
#
# Runnable standalone:
#   bash scripts/lib/worktree-claims.sh list
#     -> "<file> WT:<id>" lines, the exact shape the old gate's _WT_FILES had.
#   bash scripts/lib/worktree-claims.sh census
#     -> one line per worktree: id, branch, classification, commits-behind,
#        age-days, dirty-tracked-count.
#   bash scripts/lib/worktree-claims.sh explain <worktree-path>
#     -> classification + reason for one worktree, and (if dirty-tracked)
#        names each still-claimed dirty file.
#
# Classification (in order — first match wins):
#   MERGED    — the worktree's branch is the head ref of a merged PR. Detected
#               via one batched `gh pr list --state merged` call (never per-
#               worktree — 53 worktrees x 1 gh call each is not acceptable in
#               a gate that runs on every spawn). This is the only detection
#               method that survives squash-merge: `git branch --merged` and
#               patch-id/cherry equivalence both fail because squash collapses
#               N commits into 1, so nothing in the worktree's history matches
#               anything in main's history. Worktrees on detached HEAD have no
#               branch to match and fall through to rule 2.
#   ABANDONED — (D#2155, PR-a) last-activity age in hours >
#               policies.team_lead.claim_gate_abandoned_hours (NEW key,
#               default 24) AND no PR (open, closed, OR merged) has EVER been
#               opened with this branch as head ref. Fills a gap MERGED/STALE
#               both miss: a worktree abandoned before it ever produces a PR
#               has no merged-head ref to match (rule 1) and can still be well
#               under the day-scale STALE thresholds (rule 3) — e.g. one day
#               old. "Ever had a PR" is checked via a SEPARATE batched, cached
#               `gh pr list --state all` call — deliberately not a widened
#               --state merged call, see wtc_load_all_pr_heads. BOTH an
#               unanswerable lookup (gh unavailable) AND a lookup that
#               SUCCEEDED but came back empty degrade fail CLOSED here
#               (assume a PR exists, never fire ABANDONED) — see
#               wtc_branch_ever_had_pr for why an empty-but-successful
#               result is not treated as a trustworthy "no PR ever" on this
#               repo. This is the opposite direction from the MERGED gh
#               degrade, because releasing a claim is the risky direction for
#               this rule while withholding MERGED status is the safe one for
#               that rule.
#   STALE     — commits-behind origin/main > policies.team_lead.claim_gate_stale_commits
#               (existing key, default 20, unchanged) OR last-activity age in
#               days > policies.team_lead.claim_gate_stale_days (default 14).
#               last-activity = max(HEAD committer date, worktree directory
#               mtime) — deliberately the conservative direction: it keeps a
#               worktree claiming for longer, never shorter, than either
#               signal alone would.
#   ACTIVE    — everything else.
#
# What each classification claims:
#   ACTIVE                    -> git diff --name-only origin/main...HEAD
#                      (three-dot — see note below) plus uncommitted tracked
#                      changes.
#   MERGED / ABANDONED / STALE -> nothing from the committed diff. BUT: safety
#                      valve — if the worktree has uncommitted TRACKED changes
#                      (git status --porcelain -z, ignoring "??" untracked
#                      records), those files are claimed ONLY if their
#                      working-tree content actually differs from
#                      `origin/main` (D#2090) — a dirty file that is
#                      byte-identical to origin/main merely diverged from
#                      the worktree's own stale HEAD, not from anything live,
#                      and blocking a spawn to protect it has no value. A
#                      WARN is still printed to stderr for every file that
#                      DOES remain claimed, naming the worktree, its
#                      classification/reason/age, and each dirty file. A
#                      MERGED/ABANDONED/STALE worktree holding real divergent
#                      content is never silently invisible — a blocked spawn
#                      is cheap, a silently-skipped worktree with real
#                      unmerged content is not (see D#1820: one such worktree
#                      turned out to hold a 916-line deletion resembling a
#                      botched revert). If `origin/main` cannot be resolved in
#                      that worktree, this filter is skipped entirely and
#                      every dirty tracked file stays claimed — fail closed,
#                      the opposite degrade direction from wtc_commits_behind
#                      (which reads an unresolvable ref as "0 behind"). This
#                      dirty-file protection is unchanged and untouched by
#                      PR-a — ABANDONED joins the same "else" branch MERGED
#                      and STALE already share; expiry releases a CLAIM
#                      (the committed-diff claim), never the worktree
#                      directory itself.
#
# Three-dot vs two-dot: the old gate used two-dot `git diff origin/main`,
# which conflates "files this branch changed" with "files main changed
# since this branch's base" — manufacturing claims on files the worktree
# never touched. `list`/ACTIVE claims use three-dot `origin/main...HEAD`
# instead, scoped to the branch's own changes since it forked.
#
# gh degrade (constraint: no gh call may block the hot path when gh is
# unavailable): if `gh pr list --state merged` fails, MERGED detection is
# skipped entirely for this run — no worktree is classified MERGED. The
# commits-behind and wall-clock STALE checks still run (they need no
# network), so this degrades toward the *pre-fix* gate behaviour, never
# toward treating every worktree as active-and-unclaimed. Likewise, if the
# separate `gh pr list --state all` call (ABANDONED's "ever had a PR"
# lookup, see wtc_load_all_pr_heads) fails, ABANDONED detection is skipped
# entirely — every worktree is treated as "a PR might exist", never as
# abandoned, on an unverifiable answer.
#
# Test overrides (mirrors scripts/lib/ci-status-check.sh's mock-var
# convention, so fixtures never make a real `gh` call):
#   WTC_MERGED_HEADS_OVERRIDE — newline-separated head ref names, used
#                                instead of calling `gh pr list --state merged`.
#   WTC_ALL_HEADS_OVERRIDE    — newline-separated head ref names (any PR
#                                state), used instead of calling
#                                `gh pr list --state all` for ABANDONED's
#                                "ever had a PR" check.
#   WTC_SKIP_GH=1              — force the gh-unavailable degrade path for
#                                BOTH the merged-heads and all-heads calls.
#   WTC_NOW_EPOCH              — override "now" (unix seconds) for age math.

set -uo pipefail

_WTC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_WTC_REPO_ROOT="$(cd "$_WTC_LIB_DIR/../.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$_WTC_LIB_DIR/repo-resolve.sh"

_WTC_MERGED_HEADS_CACHE=""
_WTC_MERGED_HEADS_LOADED=0
_WTC_MERGED_HEADS_AVAILABLE=1

# ── policy lookups ───────────────────────────────────────────────────────────
wtc_stale_commits_threshold() {
  local v
  v=$(python3 "$_WTC_REPO_ROOT/backend/control_plane.py" get policies.team_lead.claim_gate_stale_commits 2>/dev/null | tr -d '"')
  [[ "$v" =~ ^[0-9]+$ ]] || v=20
  printf '%s' "$v"
}

wtc_stale_days_threshold() {
  local v
  v=$(python3 "$_WTC_REPO_ROOT/backend/control_plane.py" get policies.team_lead.claim_gate_stale_days 2>/dev/null | tr -d '"')
  [[ "$v" =~ ^[0-9]+$ ]] || v=14
  printf '%s' "$v"
}

# claim_gate_abandoned_hours (D#2155, PR-a) — no enable flag by design (see
# module header): a threshold with a shipped default, live on every call,
# same shape as claim_gate_stale_days above.
wtc_abandoned_hours_threshold() {
  local v
  v=$(python3 "$_WTC_REPO_ROOT/backend/control_plane.py" get policies.team_lead.claim_gate_abandoned_hours 2>/dev/null | tr -d '"')
  [[ "$v" =~ ^[0-9]+$ ]] || v=24
  printf '%s' "$v"
}

# ── merged head-ref set: one batched gh call, cached per-process ────────────
wtc_load_merged_heads() {
  [[ "$_WTC_MERGED_HEADS_LOADED" -eq 1 ]] && { [[ "$_WTC_MERGED_HEADS_AVAILABLE" -eq 1 ]] && return 0 || return 1; }
  _WTC_MERGED_HEADS_LOADED=1

  if [[ -n "${WTC_MERGED_HEADS_OVERRIDE:-}" ]]; then
    _WTC_MERGED_HEADS_CACHE="$WTC_MERGED_HEADS_OVERRIDE"
    _WTC_MERGED_HEADS_AVAILABLE=1
    return 0
  fi
  if [[ "${WTC_SKIP_GH:-0}" == "1" ]]; then
    _WTC_MERGED_HEADS_CACHE=""
    _WTC_MERGED_HEADS_AVAILABLE=0
    return 1
  fi

  local repo out rc=0
  repo="$(_resolve_repo)"
  out=$(gh pr list --repo "$repo" --state merged --limit 300 --json headRefName --jq '.[].headRefName' 2>/dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    _WTC_MERGED_HEADS_CACHE=""
    _WTC_MERGED_HEADS_AVAILABLE=0
    return 1
  fi
  _WTC_MERGED_HEADS_CACHE="$out"
  _WTC_MERGED_HEADS_AVAILABLE=1
  return 0
}

wtc_is_merged_branch() {
  local branch="$1"
  [[ -z "$branch" ]] && return 1
  wtc_load_merged_heads || return 1
  [[ -z "$_WTC_MERGED_HEADS_CACHE" ]] && return 1
  printf '%s\n' "$_WTC_MERGED_HEADS_CACHE" | grep -qxF "$branch"
}

_WTC_ALL_PR_HEADS_CACHE=""
_WTC_ALL_PR_HEADS_LOADED=0
_WTC_ALL_PR_HEADS_AVAILABLE=1

# ── all-PR head-ref set (any state): one SEPARATE batched gh call ──────────
# Answers "has a PR — open, closed, OR merged — EVER been opened for this
# branch", for the ABANDONED classification only. This is deliberately a
# second call with its own cache, NOT a widening of wtc_load_merged_heads'
# `--state merged --limit 300` to `--state all`: on a repo already at
# PR #2169, that would push genuinely-merged PRs past the --limit 300 window
# and silently starve MERGED detection, regressing D#1819. At most one
# additional gh subprocess per process, cached exactly like
# wtc_load_merged_heads (never per-worktree).
wtc_load_all_pr_heads() {
  [[ "$_WTC_ALL_PR_HEADS_LOADED" -eq 1 ]] && { [[ "$_WTC_ALL_PR_HEADS_AVAILABLE" -eq 1 ]] && return 0 || return 1; }
  _WTC_ALL_PR_HEADS_LOADED=1

  if [[ -n "${WTC_ALL_HEADS_OVERRIDE:-}" ]]; then
    _WTC_ALL_PR_HEADS_CACHE="$WTC_ALL_HEADS_OVERRIDE"
    _WTC_ALL_PR_HEADS_AVAILABLE=1
    return 0
  fi
  if [[ "${WTC_SKIP_GH:-0}" == "1" ]]; then
    _WTC_ALL_PR_HEADS_CACHE=""
    _WTC_ALL_PR_HEADS_AVAILABLE=0
    return 1
  fi

  local repo out rc=0
  repo="$(_resolve_repo)"
  out=$(gh pr list --repo "$repo" --state all --limit 300 --json headRefName --jq '.[].headRefName' 2>/dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    _WTC_ALL_PR_HEADS_CACHE=""
    _WTC_ALL_PR_HEADS_AVAILABLE=0
    return 1
  fi
  _WTC_ALL_PR_HEADS_CACHE="$out"
  _WTC_ALL_PR_HEADS_AVAILABLE=1
  return 0
}

# Returns 0 ("yes, a PR exists for this branch") when a PR of ANY state has
# ever used this branch as its head ref, OR when the answer is anything less
# than a positive, trustworthy "no" — fail CLOSED for ABANDONED purposes: an
# unverifiable or ambiguous answer must never release a claim, so treat it as
# "a PR exists" rather than "no PR ever". Concretely that covers TWO distinct
# cases, not just one:
#   - the lookup itself could not be answered at all (gh unavailable /
#     WTC_SKIP_GH) — wtc_load_all_pr_heads returns non-zero.
#   - the lookup SUCCEEDED but came back with an empty cache. On this repo an
#     empty result is not a credible "zero PRs ever" signal (it is already
#     past PR #2100+, so --limit 300 saturates every real call) — an empty
#     cache after a reported success is closer to "something about this
#     answer isn't trustworthy" than to genuine emptiness, and only the
#     grep-miss case below (non-empty cache, branch just isn't in it) is a
#     positive, trustworthy "no". A prior version of this function returned
#     1 (not merged) here, matching wtc_is_merged_branch's degrade — WRONG
#     for this function specifically: that flips the meaning to "no PR ever"
#     and fires ABANDONED on the one case that most needs failing closed.
# This is the opposite fail direction from wtc_is_merged_branch (which fails
# toward "not merged" on gh-unavailable, including an empty-but-successful
# cache) because the two rules have opposite risky directions — withholding
# MERGED status is the safe default there, silently firing ABANDONED here is
# not.
wtc_branch_ever_had_pr() {
  local branch="$1"
  [[ -z "$branch" ]] && return 0
  wtc_load_all_pr_heads || return 0
  [[ -z "$_WTC_ALL_PR_HEADS_CACHE" ]] && return 0   # fail closed: assume a PR exists on an empty-but-successful answer
  printf '%s\n' "$_WTC_ALL_PR_HEADS_CACHE" | grep -qxF "$branch"
}

# ── per-worktree signals ─────────────────────────────────────────────────────
wtc_commits_behind() {
  local wt_path="$1"
  local n
  n=$(git -C "$wt_path" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  printf '%s' "$n"
}

_wtc_now_epoch() {
  if [[ -n "${WTC_NOW_EPOCH:-}" ]]; then
    printf '%s' "$WTC_NOW_EPOCH"
  else
    date +%s
  fi
}

# last-activity age in whole SECONDS = max(HEAD committer date, dir mtime),
# measured from now. Deliberately conservative: whichever signal is MORE
# RECENT wins, so the worktree looks younger (keeps claiming longer) rather
# than older. Shared by wtc_age_days and wtc_age_hours (D#2155, PR-a) so
# both units come from exactly one `git log` + one `stat` call rather than
# each re-deriving last-activity independently.
_wtc_last_activity_age_seconds() {
  local wt_path="$1"
  local now committer_ts dir_mtime last_activity age_seconds
  now="$(_wtc_now_epoch)"
  committer_ts=$(git -C "$wt_path" log -1 --format=%ct 2>/dev/null || echo 0)
  [[ "$committer_ts" =~ ^[0-9]+$ ]] || committer_ts=0
  dir_mtime=$(stat -c %Y "$wt_path" 2>/dev/null || stat -f %m "$wt_path" 2>/dev/null || echo "$now")
  [[ "$dir_mtime" =~ ^[0-9]+$ ]] || dir_mtime="$now"
  last_activity=$(( committer_ts > dir_mtime ? committer_ts : dir_mtime ))
  age_seconds=$(( now - last_activity ))
  [[ "$age_seconds" -lt 0 ]] && age_seconds=0
  printf '%s' "$age_seconds"
}

# last-activity age in whole days — see _wtc_last_activity_age_seconds.
wtc_age_days() {
  local wt_path="$1"
  local age_seconds
  age_seconds=$(_wtc_last_activity_age_seconds "$wt_path")
  printf '%s' $(( age_seconds / 86400 ))
}

# last-activity age in whole hours (D#2155, PR-a) — the ABANDONED
# classification's threshold (claim_gate_abandoned_hours, default 24) is
# hour-scale, well below wtc_age_days' day-scale resolution.
wtc_age_hours() {
  local wt_path="$1"
  local age_seconds
  age_seconds=$(_wtc_last_activity_age_seconds "$wt_path")
  printf '%s' $(( age_seconds / 3600 ))
}

# Dirty TRACKED files only — ignores "??" untracked lines, matches the
# convention already used by scripts/sweep-stale-worktrees.sh.
#
# Reads `git status --porcelain -z` rather than the default line form.
# Why (D#1951): porcelain v1 is whitespace-delimited, so it C-quotes any
# path containing a space or a non-ASCII byte —
#   ` M "Meta Aesthetics/README.md"` and ` M "caf\303\251.txt"`
# — while wtc_match_claim compares against the raw, unquoted touchpoint
# path. A quoted claim line can never match a real touchpoint, so a genuine
# conflict on such a path silently failed to block a spawn. `-z` emits raw
# NUL-terminated records with no quoting at all, which is the spelling the
# matcher actually compares against. `core.quotePath=false` is NOT a
# substitute: measured on git 2.55.0 it unquotes the non-ASCII case but
# porcelain still quotes spaces unconditionally.
#
# The `-z` rename/copy record shape is NOT the ` -> ` arrow form rearranged.
# Measured for a rename of plain.txt -> "renamed plain.txt":
#   "R  renamed plain.txt\0" "plain.txt\0"
# The NEW path shares the record with the status code; the OLD path is the
# FOLLOWING record and carries no status prefix. So the [RC] branch has to
# consume that paired record explicitly via getline — if it falls through to
# the next iteration it gets parsed as though its first two bytes were a
# status code, which silently mangles it (measured: a bare old-path record
# `plain.txt` parses as code "pl", payload "in.txt"; a bare old path under
# `Meta Aesthetics/...` parses as code "Me" — which MATCHES /[MADRC]/ — and
# emits the junk payload `ta Aesthetics/...`).
#
# Both the old and new paths of a rename are legitimately claimed by this
# worktree, so each is emitted as its OWN line — never one line containing
# " -> ". D#1914: a caller that takes the first whitespace-delimited field
# of a claim line (rather than stripping the trailing ref token) would
# otherwise silently stop matching the new path of a rename. Under `-z`
# there is no arrow to split on at all, so the D#1914 review fix (gating the
# split on R/C status codes so a plain M-status path that merely CONTAINS
# " -> " is not sliced into two junk paths) is preserved structurally rather
# than by a guard — see WC-15.
#
# The `[MADRC]` status-code filter is load-bearing and stays: `-z` still
# emits `?? untracked-file` records, and this filter is the only thing
# dropping them. Widening this set to untracked files is the D#1911 trap.
#
# Output stays NEWLINE-delimited, one bare path per line — the claim list
# this feeds is line-oriented (`printf '%s WT:%s\n'`), and every consumer
# (including the census dirty-count and the tests) reads it as lines. A path
# containing a literal newline cannot be represented in that format; it is
# skipped with a WARN rather than emitted as two fragments, which would
# manufacture claims on paths nobody touched. That newline limit is
# pre-existing and out of scope here — this just declines to make it worse.
wtc_dirty_tracked_files() {
  local wt_path="$1"
  git -C "$wt_path" status --porcelain -z 2>/dev/null | awk '
    BEGIN { RS = "\0"; ORS = "\n" }
    function emit(p) {
      if (p == "") return
      if (index(p, "\n") > 0) {
        print "WARN: skipping claim for a path containing a newline (not representable in the line-oriented claim list)" > "/dev/stderr"
        return
      }
      print p
    }
    {
      code = substr($0, 1, 2)
      if (code !~ /[MADRC]/) next
      payload = substr($0, 4)
      if (code ~ /[RC]/) {
        emit(payload)                     # new path — shares this record
        if ((getline old_path) > 0) {     # old path — the NEXT record, bare
          emit(old_path)
        }
      } else {
        emit(payload)
      }
    }'
}

# ── origin/main content-divergence set (D#2090) ─────────────────────────────
# Populates the associative array named by $2 with one key per path whose
# WORKING TREE content differs from origin/main, via a single
#   git diff --name-only -z --no-renames origin/main --
# invocation (two-dot form: this compares the WORKING TREE against
# origin/main, not HEAD against origin/main — see the module header). One
# call answers "does this worktree hold content that differs from
# origin/main" for the whole dirty set at once; a per-file hash-object /
# cat-file loop was measured and rejected — one worktree in the D#2090
# filing carried 206 dirty tracked files.
#
# --no-renames is load-bearing (fix-cycle 1): with default rename detection,
# a staged/uncommitted rename collapses to ONE record naming only the new
# path, so the old path silently drops out of this set even though it still
# differs from origin/main (which holds the file at the old path). Renames
# into archive/ are routine here — the Archive Protocol mandates `git mv`
# over `git rm` — so this is not an edge case. See WC-G.
#
# Returns 0 and populates the array when origin/main resolves. Returns
# non-zero and leaves the array EMPTY when it does not (detached remote,
# degraded clone, or a fixture that removed the ref) — callers MUST treat a
# non-zero return as "cannot compare", never as "nothing differs" (fail
# closed, D#2090 constraint — the opposite degrade direction from
# wtc_commits_behind, which reads an unresolvable ref as "0 behind").
#
# Output goes through a temp file rather than process substitution or
# `$(...)`: process substitution loses the git command's own exit status
# (only the reading command's status survives it), and `$(...)` strips NUL
# bytes from -z output (the same D#1951 trap wtc_dirty_tracked_files already
# documents) — a plain redirect is the only shape that preserves both the
# NUL-delimited paths and the real exit code from one invocation.
wtc_differs_from_main() {
  local wt_path="$1"
  local -n _wtc_out_set="$2"
  _wtc_out_set=()

  local tmp rc
  tmp="$(mktemp)"
  # --no-renames: default rename detection collapses a staged/uncommitted
  # rename into a single "new path only" record, silently dropping the OLD
  # path from this set even though it genuinely differs from origin/main
  # (which still holds the file there). Renames into archive/ are the house
  # style here (Archive Protocol mandates `git mv`), so this is routine, not
  # exotic — see WC-G. Still one invocation; the flag changes what a single
  # call reports, not how many calls happen.
  git -C "$wt_path" diff --name-only -z --no-renames origin/main -- > "$tmp" 2>/dev/null
  rc=$?
  if [[ "$rc" -eq 0 ]]; then
    local d
    while IFS= read -r -d '' d; do
      [[ -z "$d" ]] && continue
      if [[ "$d" == *$'\n'* ]]; then
        echo "WARN: skipping origin/main comparison for a path containing a newline (not representable in the line-oriented claim list)" >&2
        continue
      fi
      _wtc_out_set["$d"]=1
    done < "$tmp"
  fi
  rm -f "$tmp"
  return "$rc"
}

# ── claim-line matcher ───────────────────────────────────────────────────────
# Reads claim lines ("<path> <REF>") on stdin; prints the first line whose
# path field equals $1, where the path field is the line with its trailing
# " <REF>" token (WT:<id> or PR#<n>) stripped off. Prints nothing and exits 0
# when there is no match.
#
# D#1914: the path must be recovered by stripping the FINAL space-delimited
# token, not by taking $1 (awk's first field). Two independent reasons, one
# per half of the claim list this reads:
#   - The `git diff --name-only -z` half (PR files, ACTIVE-worktree diffs) is
#     unquoted, so a tracked path containing a space — e.g.
#     archive/clawcode-2026-08-17/.claw/design/Meta Aesthetics/README.md —
#     breaks `$1` outright: it would resolve to just "archive/clawcode-2026-08-17/.claw/design/Meta".
#   - The `git status --porcelain -z` half (wtc_dirty_tracked_files) is
#     rename/copy-aware: a rename claim line's path is its NEW half, already
#     split out as its own line above. `$1` there breaks for a different
#     reason — nothing to do with spaces — because before that split existed,
#     a rename's raw "old -> new" payload put the OLD path in $1.
# Preserves the old `head -1` semantics: when a path is claimed by two
# worktrees, only the first matching line is printed.
wtc_match_claim() {
  local tp="$1"
  awk -v tp="$tp" '{ p = $0; sub(/ [^ ]*$/, "", p); if (p == tp) { print; exit } }'
}

# ── single-slot dirty-files memo (D#2158) ────────────────────────────────────
# wtc_dirty_tracked_files runs `git status --porcelain -z` for one worktree.
# wtc_classify (for WTC_DIRTY_COUNT) and wtc_claimed_files (for the claim
# list itself) each called it once per worktree, so the scan paid for the
# same `git status` twice per entry — 416 of 877 git subprocesses measured
# under a PATH shim, see D#2158. This wrapper is called directly at each
# call site (never through `$( )`) so the assignment lands in the CALLER's
# shell frame — a plain global written inside wtc_dirty_tracked_files itself
# would be invisible: `$( )` runs its command in a subshell, and a subshell's
# variables die with it the moment the substitution closes.
#
# A single slot keyed on path is enough and is deliberately NOT an
# associative array: wtc_cmd_list/census/explain all walk one worktree at a
# time, so the slot naturally self-invalidates on the next path. Its
# lifetime is exactly one process — nothing here is written to disk or
# survives past this scan (D#2158 Constraint: no persisted state, no cache
# file, no classification memo — this memoises one derived-input function,
# not a classification).
WTC_DIRTY_MEMO_PATH=""
WTC_DIRTY_MEMO_VALUE=""
_wtc_dirty_memo() {
  local wt_path="$1"
  if [[ "$WTC_DIRTY_MEMO_PATH" != "$wt_path" ]]; then
    WTC_DIRTY_MEMO_PATH="$wt_path"
    WTC_DIRTY_MEMO_VALUE="$(wtc_dirty_tracked_files "$wt_path")"
  fi
}

# ── classification ───────────────────────────────────────────────────────────
# Sets globals WTC_CLASS, WTC_REASON, WTC_BEHIND, WTC_AGE_DAYS, WTC_DIRTY_COUNT.
wtc_classify() {
  local wt_path="$1" branch="$2"
  local commits_threshold days_threshold abandoned_hours_threshold
  local age_seconds behind age age_hours dirty_count

  commits_threshold=$(wtc_stale_commits_threshold)
  days_threshold=$(wtc_stale_days_threshold)
  abandoned_hours_threshold=$(wtc_abandoned_hours_threshold)
  behind=$(wtc_commits_behind "$wt_path")
  age_seconds=$(_wtc_last_activity_age_seconds "$wt_path")
  age=$(( age_seconds / 86400 ))
  age_hours=$(( age_seconds / 3600 ))
  _wtc_dirty_memo "$wt_path"
  dirty_count=$(printf '%s' "$WTC_DIRTY_MEMO_VALUE" | grep -c . || true)

  WTC_BEHIND="$behind"
  WTC_AGE_DAYS="$age"
  WTC_DIRTY_COUNT="$dirty_count"

  if wtc_is_merged_branch "$branch"; then
    WTC_CLASS="MERGED"
    WTC_REASON="branch '$branch' is the head ref of a merged PR"
    return 0
  fi

  # ABANDONED (D#2155, PR-a) — ahead of STALE: a worktree can be well under
  # both STALE thresholds (day-scale) yet still be abandoned hour-scale, if
  # it never produced a PR for MERGED to match against. wtc_branch_ever_had_pr
  # fails closed (assumes "yes" on an unverifiable gh answer), so this arm
  # only ever fires when we positively confirmed no PR of any state exists.
  if [[ "$age_hours" -gt "$abandoned_hours_threshold" ]] && ! wtc_branch_ever_had_pr "$branch"; then
    WTC_CLASS="ABANDONED"
    WTC_REASON="last activity ${age_hours}h ago (threshold ${abandoned_hours_threshold}h) and branch '$branch' never had a PR opened"
    return 0
  fi

  if [[ "$behind" -gt "$commits_threshold" ]]; then
    WTC_CLASS="STALE"
    WTC_REASON="${behind} commits behind origin/main (threshold ${commits_threshold})"
    return 0
  fi
  if [[ "$age" -gt "$days_threshold" ]]; then
    WTC_CLASS="STALE"
    WTC_REASON="last activity ${age} days ago (threshold ${days_threshold})"
    return 0
  fi

  WTC_CLASS="ACTIVE"
  WTC_REASON="${behind} commits behind (<= ${commits_threshold}), last activity ${age}d ago (<= ${days_threshold}d)"
  return 0
}

# ── MERGED/ABANDONED/STALE dirty-file filter (shared: wtc_claimed_files + explain) ──
# Function name kept as-is (predates ABANDONED) — applies identically to all
# three non-ACTIVE classes.
# Given a worktree path and its raw dirty-tracked set (wtc_dirty_tracked_files
# output, may be empty), sets:
#   WTC_STILL_CLAIMED   — array of paths that remain claimed (D#2090: only
#                          those whose content differs from origin/main, or
#                          every dirty path if origin/main didn't resolve)
#   WTC_ORIGIN_MAIN_OK   — 0 if origin/main resolved and the filter applied,
#                          non-zero if it didn't (fail-closed: everything
#                          dirty stayed claimed)
# One wtc_differs_from_main call (one git invocation) regardless of how many
# dirty files there are — never a per-file loop.
wtc_filter_merged_stale_dirty() {
  local wt_path="$1" dirty="$2"
  WTC_STILL_CLAIMED=()
  WTC_ORIGIN_MAIN_OK=1

  [[ -z "$dirty" ]] && return 0

  local -A differs_set=()
  wtc_differs_from_main "$wt_path" differs_set
  WTC_ORIGIN_MAIN_OK=$?

  local f
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    # origin/main resolved AND this path's content matches it exactly ->
    # drop the claim. Any other case (unresolvable ref, or a real content
    # difference) keeps the file claimed — fail closed.
    if [[ "$WTC_ORIGIN_MAIN_OK" -eq 0 ]] && [[ -z "${differs_set[$f]:-}" ]]; then
      continue
    fi
    WTC_STILL_CLAIMED+=("$f")
  done <<< "$dirty"
  return 0
}

# ── claimed files for one worktree ───────────────────────────────────────────
# Emits "<file> WT:<id>" lines on stdout. Prints WARN to stderr for the
# MERGED/ABANDONED/STALE-with-dirty-files safety-valve case.
wtc_claimed_files() {
  local wt_path="$1" branch="$2" wt_id="$3"
  wtc_classify "$wt_path" "$branch"

  if [[ "$WTC_CLASS" == "ACTIVE" ]]; then
    # -z, and read straight from a process substitution rather than through
    # a `changed=$(...)` capture. Two reasons, both D#1951:
    #   - `git diff --name-only` C-quotes non-ASCII paths just like porcelain
    #     does (measured: `"caf\303\251.txt"`). That is the half the original
    #     defect report missed — with BOTH producers quoting, a conflict on a
    #     non-ASCII path never blocked, rather than sometimes. `-z` is the
    #     only lever measured to unquote both.
    #   - `$(...)` command substitution STRIPS NUL bytes (bash warns "ignored
    #     null byte in input"), so the old capture-then-`<<<` pattern cannot
    #     carry `-z` output — every path would collapse into a single line.
    local f
    while IFS= read -r -d '' f; do
      [[ -z "$f" ]] && continue
      if [[ "$f" == *$'\n'* ]]; then
        echo "WARN: worktree $wt_id has a changed path containing a newline — not representable in the claim list, skipped" >&2
        continue
      fi
      printf '%s WT:%s\n' "$f" "$wt_id"
    done < <(git -C "$wt_path" diff --name-only -z origin/main...HEAD 2>/dev/null)
    local dirty
    _wtc_dirty_memo "$wt_path"
    dirty="$WTC_DIRTY_MEMO_VALUE"
    if [[ -n "$dirty" ]]; then
      while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        printf '%s WT:%s\n' "$f" "$wt_id"
      done <<< "$dirty"
    fi
    return 0
  fi

  # MERGED / ABANDONED / STALE: nothing from the committed diff. Safety
  # valve: dirty tracked files claim only if they differ from origin/main
  # (D#2090). Note the worktree DIRECTORY is never touched here in any of
  # the three cases — only the committed-diff claim is dropped.
  local dirty
  _wtc_dirty_memo "$wt_path"
  dirty="$WTC_DIRTY_MEMO_VALUE"
  wtc_filter_merged_stale_dirty "$wt_path" "$dirty"

  if [[ ${#WTC_STILL_CLAIMED[@]} -gt 0 ]]; then
    if [[ "$WTC_ORIGIN_MAIN_OK" -eq 0 ]]; then
      echo "WARN: worktree $wt_id is classified $WTC_CLASS ($WTC_REASON, last activity ${WTC_AGE_DAYS}d ago) and holds content that differs from origin/main — these files remain claimed:" >&2
    else
      echo "WARN: worktree $wt_id is classified $WTC_CLASS ($WTC_REASON, last activity ${WTC_AGE_DAYS}d ago) — origin/main unresolvable, filter skipped, these files remain claimed:" >&2
    fi
    local f
    for f in "${WTC_STILL_CLAIMED[@]}"; do
      echo "WARN:   $f" >&2
      printf '%s WT:%s\n' "$f" "$wt_id"
    done
  fi
  return 0
}

# ── worktree enumeration ─────────────────────────────────────────────────────
# Emits "<path>\t<branch>" lines (branch empty for detached HEAD), skipping
# the primary/main worktree — `git worktree list --porcelain` always lists
# it first, regardless of which worktree this script is invoked from, so we
# skip entry index 0 rather than string-comparing against wherever this
# script happens to live (which would only be correct when invoked from the
# main checkout, not from a linked worktree like this one during development).
wtc_list_worktrees() {
  local wt_path="" wt_branch="" index=-1
  local line
  while IFS= read -r line; do
    if [[ "$line" =~ ^worktree\ (.+)$ ]]; then
      if [[ "$index" -ge 1 ]]; then
        printf '%s\t%s\n' "$wt_path" "$wt_branch"
      fi
      index=$(( index + 1 ))
      wt_path="${BASH_REMATCH[1]}"
      wt_branch=""
    elif [[ "$line" =~ ^branch\ refs/heads/(.+)$ ]]; then
      wt_branch="${BASH_REMATCH[1]}"
    fi
  done < <(git -C "$_WTC_REPO_ROOT" worktree list --porcelain 2>/dev/null)
  if [[ "$index" -ge 1 ]]; then
    printf '%s\t%s\n' "$wt_path" "$wt_branch"
  fi
}

# ── CLI subcommands ───────────────────────────────────────────────────────────
wtc_cmd_list() {
  local wt_path wt_branch wt_id
  while IFS=$'\t' read -r wt_path wt_branch; do
    [[ -z "$wt_path" ]] && continue
    [[ ! -d "$wt_path" ]] && continue
    wt_id="$(basename "$wt_path")"
    wtc_claimed_files "$wt_path" "$wt_branch" "$wt_id"
  done < <(wtc_list_worktrees)
}

wtc_cmd_census() {
  local wt_path wt_branch wt_id
  while IFS=$'\t' read -r wt_path wt_branch; do
    [[ -z "$wt_path" ]] && continue
    [[ ! -d "$wt_path" ]] && continue
    wt_id="$(basename "$wt_path")"
    wtc_classify "$wt_path" "$wt_branch"
    printf '%s %s %s behind=%s age_days=%s dirty=%s\n' \
      "$wt_id" "${wt_branch:-<detached>}" "$WTC_CLASS" "$WTC_BEHIND" "$WTC_AGE_DAYS" "$WTC_DIRTY_COUNT"
  done < <(wtc_list_worktrees)
}

wtc_cmd_explain() {
  local target="$1"
  [[ -z "$target" ]] && { echo "usage: worktree-claims.sh explain <worktree-path>" >&2; return 1; }
  target="$(cd "$target" 2>/dev/null && pwd || echo "$target")"

  local wt_path wt_branch found=0
  while IFS=$'\t' read -r wt_path wt_branch; do
    [[ "$wt_path" == "$target" ]] || continue
    found=1
    local wt_id
    wt_id="$(basename "$wt_path")"
    wtc_classify "$wt_path" "$wt_branch"
    echo "worktree: $wt_id"
    echo "path:     $wt_path"
    echo "branch:   ${wt_branch:-<detached HEAD>}"
    echo "class:    $WTC_CLASS"
    echo "reason:   $WTC_REASON"
    echo "behind:   $WTC_BEHIND commits"
    echo "age:      $WTC_AGE_DAYS days"
    local dirty
    _wtc_dirty_memo "$wt_path"
    dirty="$WTC_DIRTY_MEMO_VALUE"
    if [[ "$WTC_CLASS" == "ACTIVE" ]]; then
      if [[ -n "$dirty" ]]; then
        echo "dirty tracked files (still claimed regardless of class):"
        while IFS= read -r f; do
          [[ -z "$f" ]] && continue
          echo "  - $f"
        done <<< "$dirty"
      else
        echo "dirty tracked files: none"
      fi
    else
      # MERGED/ABANDONED/STALE: only files that actually differ from
      # origin/main are claimed (D#2090) — same filter wtc_claimed_files
      # applies.
      wtc_filter_merged_stale_dirty "$wt_path" "$dirty"
      if [[ ${#WTC_STILL_CLAIMED[@]} -gt 0 ]]; then
        if [[ "$WTC_ORIGIN_MAIN_OK" -eq 0 ]]; then
          echo "dirty tracked files that differ from origin/main (still claimed):"
        else
          echo "dirty tracked files (origin/main unresolvable, filter skipped — all remain claimed):"
        fi
        local f
        for f in "${WTC_STILL_CLAIMED[@]}"; do
          echo "  - $f"
        done
      else
        echo "dirty tracked files: none (all match origin/main)"
      fi
    fi
  done < <(wtc_list_worktrees)

  if [[ "$found" -eq 0 ]]; then
    echo "no worktree registered at path: $target" >&2
    return 1
  fi
  return 0
}

# ── standalone entry point ───────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    list) wtc_cmd_list ;;
    census) wtc_cmd_census ;;
    explain) shift; wtc_cmd_explain "${1:-}" ;;
    *)
      echo "usage: worktree-claims.sh {list|census|explain <path>}" >&2
      exit 1
      ;;
  esac
fi
