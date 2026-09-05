#!/usr/bin/env bash
# scripts/build-public-seed.sh — build the commit that seeds the public repo.
#
# Builds a single commit whose parent is the public repo's current tip and
# whose tree is the seed set: this repo's tracked tree minus the directories
# that do not ship. Prints the commit sha. Does NOT push — publication is a
# one-way, world-readable act and it is the owner's to perform, from the sha
# this prints.
#
# The tree is assembled with plumbing (`ls-tree` -> `update-index
# --index-info` -> `write-tree`), never by deleting files from a checkout.
# Blob shas and file modes are carried across verbatim, so the seed commit's
# content for any surviving path is bit-identical to this repo's, and the
# working tree is never touched. `git rm` never runs, so the Archive Protocol
# is not engaged: nothing is removed from this repo, a different tree is
# built beside it.
#
# THE EXCLUSION LIST DECIDES BY NAMING, NOT BY OMISSION
#
# The seed was first specified as "the tree minus archive/, minus
# .autonomous-team/, minus open-source/". A rule that decides by omission
# cannot be reviewed, because nothing in it names what it is deciding, and a
# directory added at the repo root later ships silently under it. Measured
# when this list was written, that rule published 152 files that must not go
# out; measured against today's tree the delta is 0, because D#2348 phases 1
# and 2 moved every one of those trees to the internal repo. The entries for
# them stay: they guard against a reappearance now rather than a live leak,
# and an entry naming a directory that is gone costs nothing.
#
# Every path below is listed with the authority that excluded it, and each
# count is measured against the tracked tree — re-derive, do not trust it.
#
# Usage:
#   bash scripts/build-public-seed.sh [--sha <engine-sha>] [--parent <public-tip>]
#
# Defaults: --sha HEAD, --parent the public repo's current main.
#
# Exit 0 = commit built, sha on stdout. Exit 1 = a precondition failed.

set -euo pipefail

# Honours AUTONOMOUS_TEAM_REPO_ROOT (scripts/lib/repo-root-resolve.sh's
# convention) so the mutation tests can run a modified COPY of this script
# from a tmpdir against the real repo, instead of writing a mutant into
# scripts/ and hoping the cleanup fires. Falls back to this file's own
# location, which is what every non-test caller gets.
if [[ -n "${AUTONOMOUS_TEAM_REPO_ROOT:-}" ]]; then
  REPO_ROOT="$(cd "$AUTONOMOUS_TEAM_REPO_ROOT" && pwd)"
else
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
PUBLIC_REMOTE="${PUBLIC_REMOTE:-https://github.com/fulcrumaxe/fulcrumaxe.git}"

# ---------------------------------------------------------------------------
# What does not ship. Each entry carries who excluded it, so the next reader
# can diff intent against outcome instead of re-deriving the list.
# ---------------------------------------------------------------------------
EXCLUDED_PREFIXES=(
  # Named in PR-l item 1 itself. Counts measured against the tracked tree at
  # aa9ec34b; archive/ held 1801 files and .autonomous-team/ 73 when this list
  # was first written, before D#2348 phases 1 and 2 moved most of both out.
  "archive/"                 # 416 files — one retained audit corpus, moves in phase 3
  ".autonomous-team/"        #  67 files — live team state
  "open-source/"             #  22 files — the export machinery being retired

  # Owner decision, D#2348 2026-09-04T11:35Z, with dashboard_tui/ reversed
  # to excluded at 11:41Z. Re-stated in scripts/ci/publish-denylist.sh's
  # header, which records that enforcing this set is PR-l's job.
  #
  # Every one of these is 0 files today: phase 1 moved the whole tree to
  # fulcrumaxe-internal. They stay listed so a directory of that name
  # reappearing at the root is excluded by decision, not by nobody noticing.
  "wiki/"                    #   0 files (was 56)
  "dashboard_tui/"           #   0 files (was 55)
  "docker/"                  #   0 files (was  2)
  "systemd/"                 #   0 files (was  2)
  "verification-report/"     #   0 files (was  3)
  "templates/"               #   0 files (was  1)

  # Publish denylist, enforced in CI by scripts/ci/publish-denylist.sh (#2383).
  # These sit under scripts/, which PR-l item 1 shipped wholesale — so the
  # Spec as written produced a push its own denylist gate exists to reject.
  # Also 0 files today, and retained for the same reason as the block above.
  "scripts/training/"        #   0 files (was 18)
  "scripts/serving/"         #   0 files (was  8)
  "scripts/gemma-sandbox/"   #   0 files (was  6)
)

EXCLUDED_FILES=(
  # Untracked since phase 1; kept for the same reason the 0-file prefixes are.
  "pr-body-p5e.txt"          # a PR description someone wrote to a file and committed
)

# .env.example SHIPS. Stated rather than defaulted, because it sits at the
# root and would otherwise be decided by the absence of an entry here.
#
# It carries exactly two credential lines and both are commented out, so it
# is the documented opposite of a secret — a template telling an adopter
# which variables to set. The publish denylist's `*.env*` glob exists to stop
# credentials, and scripts/ci/publish-denylist.sh already carves out this
# exact basename on purpose (an exact-basename match, so `prod.env.example`
# and `.env.example.bak` stay denied). Letting a bare glob decide would have
# ruled, as a side effect of a pattern, that this project ships no example
# environment file at all.

# ---------------------------------------------------------------------------
# One file is ADDED that this repo does not track: loop-bootstrap/'s resolved
# path data.
#
# bootstrap.sh resolves BOOTSTRAP_PATHS from two sources — open-source/'s
# MANIFEST.md (the engine clone's arm) or bootstrap-paths.generated sitting
# next to it (the export's arm, written by export.sh at export time). The
# seed ships loop-bootstrap/ and excludes open-source/, and the generated
# file is untracked here, so without this the public repo would get a
# bootstrap.sh that exits 1 at its first line of real work — a regression on
# what is published there today, introduced by the seed itself.
#
# The file is regenerated from MANIFEST.md at build time rather than copied,
# so it cannot go stale against the manifest it derives from. Verified
# byte-identical to the copy already published at ffbf45f3, so this adds no
# disclosure: it is resolved values only, no prose, which is the whole reason
# the generated-file mechanism exists (see MANIFEST.md:37 — the owner
# rejected shipping the manifest or its libs even trimmed, because
# rsync-excludes.sh names and explains every internal carve-out).
# ---------------------------------------------------------------------------
GENERATED_BOOTSTRAP_REL="loop-bootstrap/bootstrap-paths.generated"

ENGINE_SHA="HEAD"
PARENT_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha)    ENGINE_SHA="${2:?--sha needs a value}"; shift 2 ;;
    --parent) PARENT_SHA="${2:?--parent needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

cd "$REPO_ROOT"
FULL_SHA="$(git rev-parse --verify "${ENGINE_SHA}^{commit}")"

if [[ -z "$PARENT_SHA" ]]; then
  PARENT_SHA="$(git ls-remote "$PUBLIC_REMOTE" refs/heads/main | cut -f1)"
  [[ -n "$PARENT_SHA" ]] || { echo "could not resolve public main" >&2; exit 1; }
fi
# The parent must be an object we have. Fetch it if it is not already here.
git cat-file -e "${PARENT_SHA}^{commit}" 2>/dev/null \
  || git fetch --no-tags "$PUBLIC_REMOTE" "$PARENT_SHA"

# --- assemble the index -----------------------------------------------------
# `ls-tree -r -z` gives NUL-terminated "<mode> <type> <sha>\t<path>" records,
# which is exactly `update-index -z --index-info`'s input format. Filtering
# the stream filters the tree.
#
# -z is not decoration. Without it `ls-tree` C-quotes any path containing a
# non-ASCII byte, a quote or a backslash — the whole record becomes
# `... <sha>\t"caf\303\251/x.md"` — so a prefix comparison sees a leading `"`
# and the exclusion silently misses. There are no such paths today, which is
# what makes it dangerous: it would start missing the day someone adds one.
# This is the same defect that blocked #2383, and this is that fix.
INDEX_INFO="$(mktemp)"
KEPT="$(mktemp)"
DROPPED="$(mktemp)"
TMP_INDEX="$(mktemp)"
GEN_TMP="$(mktemp)"
trap 'rm -f "$INDEX_INFO" "$KEPT" "$DROPPED" "$TMP_INDEX" "$GEN_TMP"' EXIT

while IFS= read -r -d '' record; do
  path="${record#*$'\t'}"
  skip=""
  for p in "${EXCLUDED_PREFIXES[@]}"; do
    [[ "$path" == "$p"* ]] && { skip=1; break; }
  done
  if [[ -z "$skip" ]]; then
    for f in "${EXCLUDED_FILES[@]}"; do
      [[ "$path" == "$f" ]] && { skip=1; break; }
    done
  fi
  if [[ -n "$skip" ]]; then
    printf '%s\n' "$path" >> "$DROPPED"
  else
    printf '%s\0' "$record" >> "$INDEX_INFO"
    printf '%s\n' "$path" >> "$KEPT"
  fi
done < <(git ls-tree -r -z "$FULL_SHA")

[[ -s "$INDEX_INFO" ]] || { echo "seed set came out empty — refusing" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Memory-tier prune, carried from export.sh:269-370.
#
# scripts/memory-triage/ carries a `tier:` field per memory (D#874).
# `tier: project` memories are this project's own operational history — they
# name a personal Hugging Face account, RunPod pod and volume ids, and
# training runs. They have never shipped. There were 13 of them and the
# export was the only thing pruning them, so a seed built from the tracked
# tree alone would have published all 13.
#
# Phase 1 untracked those 13, so the prune removes 0 today and the directory
# is down to 24 tracked files: 22 tier:transferable / tier:hardwire-candidate
# memories, MEMORY.md and apply-tiers.sh. That makes this a guard rather than
# a live transformation, which is the point of the next paragraph.
#
# Carried as the PRUNE, not as a path list, and the distinction is the whole
# point: a list ships a new memory silently the moment someone adds one,
# whereas an unrecognised or missing tier hard-fails here exactly as it does
# in export.sh. Fail-closed is the property being copied, not the filenames.
#
# MEMORY.md is the index over the directory and is filtered the same way —
# whole lines dropped when their link target was pruned, never rewritten.
# Without it the index would have shipped pointing at the 13 pruned files.
# It drops 0 entries today, for the same reason the prune removes 0.
# ---------------------------------------------------------------------------
MEMORY_REL="scripts/memory-triage"
if grep -q "^$MEMORY_REL/" "$KEPT"; then
  PRUNE_OUT="$(python3 - "$FULL_SHA" "$MEMORY_REL" <<'PYEOF'
import re
import subprocess
import sys

full_sha, memory_rel = sys.argv[1], sys.argv[2]
VALID_TIERS = {"transferable", "hardwire-candidate", "project"}

# Read the listing here rather than from stdin: this script arrives on stdin
# as a heredoc, so a pipe into it would be swallowed by the heredoc.
listing = subprocess.run(
    ["git", "ls-tree", "-r", "-z", full_sha, "--", memory_rel],
    capture_output=True, check=True).stdout

entries = {}
for record in listing.split(b"\0"):
    if not record:
        continue
    meta, path = record.decode("utf-8").split("\t", 1)
    entries[path] = meta.split()[2]


def blob(sha):
    return subprocess.run(["git", "cat-file", "blob", sha],
                          capture_output=True, check=True).stdout.decode("utf-8")


def read_tier(text):
    """The `tier:` value from the frontmatter block, or None. Scoped between
    the first two '---' lines so a `tier:` mention in prose can never be
    mistaken for the field. First line wins on duplicates — same behaviour
    as export.sh, recorded rather than changed (D#1873)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("tier:"):
            return line.split(":", 1)[1].strip()
    return None


index_rel = f"{memory_rel}/MEMORY.md"
removed, kept = [], []
for path in sorted(entries):
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".md") or path == index_rel:
        continue
    tier = read_tier(blob(entries[path]))
    if tier not in VALID_TIERS:
        print(f"error: {path} has a missing or unrecognised tier "
              f"(found: {tier!r}); expected one of {sorted(VALID_TIERS)}",
              file=sys.stderr)
        sys.exit(1)
    (removed if tier == "project" else kept).append(path)

survivors = {p.rsplit("/", 1)[-1] for p in kept} | {"MEMORY.md"}

# The index, filtered to entries whose target survived. A line that does not
# parse as an index entry is an error, not something to pass through.
new_index_sha = ""
dropped = 0
if index_rel in entries:
    out = []
    link_re = re.compile(r"\]\(([^)]+)\)")
    for line in blob(entries[index_rel]).splitlines(keepends=True):
        if not line.strip():
            out.append(line)
            continue
        m = link_re.search(line)
        if not m:
            print(f"error: MEMORY.md line does not parse as a memory-index "
                  f"entry: {line!r}", file=sys.stderr)
            sys.exit(1)
        if m.group(1) in survivors:
            out.append(line)
        else:
            dropped += 1
    new_index_sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input="".join(out).encode("utf-8"),
        capture_output=True, check=True).stdout.decode().strip()

print(f"REMOVED\t{len(removed)}")
print(f"KEPT\t{len(kept)}")
print(f"INDEX_DROPPED\t{dropped}")
print(f"INDEX_SHA\t{new_index_sha}")
for p in removed:
    print(f"DROP\t{p}")
PYEOF
)" || { echo "memory-tier prune failed — refusing to build a seed" >&2; exit 1; }

  MEM_REMOVED="$(awk -F'\t' '$1=="REMOVED"{print $2}' <<<"$PRUNE_OUT")"
  MEM_KEPT="$(awk -F'\t' '$1=="KEPT"{print $2}' <<<"$PRUNE_OUT")"
  MEM_INDEX_DROPPED="$(awk -F'\t' '$1=="INDEX_DROPPED"{print $2}' <<<"$PRUNE_OUT")"
  MEM_INDEX_SHA="$(awk -F'\t' '$1=="INDEX_SHA"{print $2}' <<<"$PRUNE_OUT")"

  # Rebuild the index-info stream without the pruned memories, and with the
  # filtered MEMORY.md swapped in for the original blob.
  FILTERED="$(mktemp)"
  while IFS= read -r -d '' record; do
    path="${record#*$'\t'}"
    if awk -F'\t' -v p="$path" '$1=="DROP" && $2==p{found=1} END{exit !found}' <<<"$PRUNE_OUT"; then
      printf '%s\n' "$path" >> "$DROPPED"
      continue
    fi
    if [[ "$path" == "$MEMORY_REL/MEMORY.md" && -n "$MEM_INDEX_SHA" ]]; then
      printf '100644 blob %s\t%s\0' "$MEM_INDEX_SHA" "$path" >> "$FILTERED"
      continue
    fi
    printf '%s\0' "$record" >> "$FILTERED"
  done < "$INDEX_INFO"
  mv "$FILTERED" "$INDEX_INFO"
  grep -vxFf <(awk -F'\t' '$1=="DROP"{print $2}' <<<"$PRUNE_OUT") "$KEPT" > "$KEPT.new" || true
  mv "$KEPT.new" "$KEPT"

  echo "Memory-tier prune: removed ${MEM_REMOVED} tier:project file(s), kept ${MEM_KEPT}; MEMORY.md dropped ${MEM_INDEX_DROPPED} entries." >&2
else
  echo "error: $MEMORY_REL/ is not in the seed set — the tier prune has nothing to protect, which means the exclusion list changed underneath it" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Plugin auto-discovery mirror, carried from export.sh:117-127.
#
# The Claude Code plugin loader finds agents and commands in CONVENTIONAL
# directories at the plugin root — agents/ and commands/ — not under
# .claude/ and not via keys in plugin.json. This repo ships
# .claude-plugin/plugin.json and marketplace.json, so the public repo is
# installable as a plugin, and verify-export.sh treats a missing mirror as a
# FAIL, not a warning.
#
# The duplication is required, not an oversight: a `git clone` user needs
# .claude/agents/, a `claude plugin install` user needs agents/. Dropping it
# reads like removing a redundant copy and is actually breaking plugin
# install.
#
# Mirrored by pointing a second path at the SAME blob sha, so the two trees
# are byte-identical by construction. export.sh rsyncs and then asserts they
# match; this cannot drift in the first place.
# ---------------------------------------------------------------------------
MIRROR_SRC="$(mktemp)"
cp "$INDEX_INFO" "$MIRROR_SRC"          # snapshot: the loop appends to INDEX_INFO
for mirror_name in agents commands; do
  found=0
  while IFS= read -r -d '' record; do
    meta="${record%%$'\t'*}"; path="${record#*$'\t'}"
    case "$path" in
      ".claude/$mirror_name/"*)
        printf '%s\t%s\0' "$meta" "${path#.claude/}" >> "$INDEX_INFO"
        printf '%s\n' "${path#.claude/}" >> "$KEPT"
        found=$((found + 1))
        ;;
    esac
  done < "$MIRROR_SRC"
  if [[ "$found" -eq 0 ]]; then
    rm -f "$MIRROR_SRC"
    echo "error: .claude/$mirror_name/ is not in the seed set — plugin auto-discovery needs $mirror_name/ at the root and there is nothing to mirror" >&2
    exit 1
  fi
  echo "Mirrored .claude/$mirror_name/ -> $mirror_name/ for plugin auto-discovery ($found files)" >&2
done
rm -f "$MIRROR_SRC"

# ---------------------------------------------------------------------------
# engine/manifest.json — filtered to the paths that are actually in the tree.
#
# This one has no counterpart in export.sh, because export.sh never shipped
# engine/. It is a SHA-256 manifest of the files under .claude/, hooks/ and
# scripts/ — 209 of them as of aa9ec34b, all present in the tree.
#
# It shipped with 224 entries, 13 of which named files under
# scripts/training/, scripts/serving/ and scripts/gemma-sandbox/ — the three
# internal-initiative directories the publish denylist exists to withhold.
# Their basenames describe the withheld work (deploy-to-vast.sh,
# vast-bringup.sh, adapter-swap.sh, sandbox-run.sh), which is the disclosure
# MANIFEST.md:37 records the owner rejecting: not the files, but a list
# naming them. The manifest has since been regenerated against a tree those
# directories had already left, so the filter drops 0 entries today.
#
# When engine/ was cleared to ship it was measured as referencing "zero paths
# under any excluded directory". That was true against the exclusion list at
# the time; the three scripts/ subdirectories were added to it hours later,
# and nobody re-derived the claim. Re-derive it here on every build instead
# of asserting it once — which is the whole reason this is a filter and not a
# comment.
#
# Filtering is also what makes the manifest CORRECT on the public repo:
# scripts/engine-sync/manifest.py verify reports MISSING for a listed file
# that is not present, so an unfiltered manifest is a permanently failing
# check as well as a disclosure. When the filter was written, two entries
# beyond the withheld thirteen were simply stale (files deleted since the
# manifest was generated) and dropped out by the same rule; there are none
# today, which is a property of the current manifest, not a guarantee.
# ---------------------------------------------------------------------------
ENGINE_MANIFEST_REL="engine/manifest.json"
if grep -qx "$ENGINE_MANIFEST_REL" "$KEPT"; then
  MANIFEST_SHA="$(awk -v p="$ENGINE_MANIFEST_REL" -F'\t' \
    'BEGIN{RS="\0"} {split($0,a,"\t"); if (a[2]==p) {split(a[1],m," "); print m[3]}}' "$INDEX_INFO")"
  [[ -n "$MANIFEST_SHA" ]] || { echo "error: could not resolve the blob for $ENGINE_MANIFEST_REL" >&2; exit 1; }
  FILTER_OUT="$(python3 - "$KEPT" "$MANIFEST_SHA" <<'PYEOF'
import json
import subprocess
import sys

kept = set(l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip())
# Read the blob here, not from stdin: this script arrives on stdin as a
# heredoc, so a pipe into it is swallowed. Same shape as the tier prune above.
doc = json.loads(subprocess.run(["git", "cat-file", "blob", sys.argv[2]],
                                capture_output=True, check=True).stdout)
files = doc.get("files")
if not isinstance(files, dict) or not files:
    print("error: engine/manifest.json has no usable 'files' object", file=sys.stderr)
    sys.exit(1)

dropped = sorted(p for p in files if p not in kept)
for p in dropped:
    del files[p]
if not files:
    print("error: filtering engine/manifest.json emptied it — refusing", file=sys.stderr)
    sys.exit(1)

body = json.dumps(doc, indent=2, sort_keys=True) + "\n"
sha = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                     input=body.encode("utf-8"),
                     capture_output=True, check=True).stdout.decode().strip()
print(f"SHA\t{sha}")
print(f"DROPPED\t{len(dropped)}")
print(f"KEPT\t{len(files)}")
for p in dropped:
    print(f"PATH\t{p}")
PYEOF
)" || { echo "engine/manifest.json filter failed — refusing to build a seed" >&2; exit 1; }

  NEW_MANIFEST_SHA="$(awk -F'\t' '$1=="SHA"{print $2}' <<<"$FILTER_OUT")"
  MANIFEST_DROPPED="$(awk -F'\t' '$1=="DROPPED"{print $2}' <<<"$FILTER_OUT")"
  MANIFEST_KEPT="$(awk -F'\t' '$1=="KEPT"{print $2}' <<<"$FILTER_OUT")"
  SWAPPED="$(mktemp)"
  while IFS= read -r -d '' record; do
    if [[ "${record#*$'\t'}" == "$ENGINE_MANIFEST_REL" ]]; then
      printf '100644 blob %s\t%s\0' "$NEW_MANIFEST_SHA" "$ENGINE_MANIFEST_REL" >> "$SWAPPED"
    else
      printf '%s\0' "$record" >> "$SWAPPED"
    fi
  done < "$INDEX_INFO"
  mv "$SWAPPED" "$INDEX_INFO"
  echo "engine/manifest.json: dropped ${MANIFEST_DROPPED} entries for paths not in the seed, kept ${MANIFEST_KEPT}." >&2
else
  echo "error: $ENGINE_MANIFEST_REL is not in the seed set — the manifest filter has nothing to protect" >&2
  exit 1
fi

# The generated bootstrap data, hashed into the object store and added by
# path. Built here from MANIFEST.md, not copied from anywhere.
# shellcheck source=/dev/null
source "$REPO_ROOT/open-source/lib/manifest_paths.sh"
# shellcheck source=/dev/null
source "$REPO_ROOT/open-source/lib/rsync-excludes.sh"
# Excludes that have real function in an export-rooted run. The complement
# (open-source/'s own carve-outs, the internal scripts/ subdirs, the runbook)
# is deliberately NOT carried: those name withheld internal paths and match
# nothing in a tree that already excludes them, so shipping them would be
# pure disclosure. Same classification export.sh applies, same reason.
GEN_SHIP=(".autonomous-team/" "archive/" "*.env*" "node_modules/" "dist/"
          "__pycache__/" "*.pyc" ".pytest_cache/" "*.pr" "*.pr_backup" "*.test"
          ".venv/" "venv/" "env/" ".ruff_cache/" ".mypy_cache/" ".coverage"
          ".claude/settings.local.json")
GEN_PATHS=()
while IFS= read -r l; do GEN_PATHS+=("$l"); done \
  < <(manifest_paths BOOTSTRAP_PATHS "$REPO_ROOT/open-source/MANIFEST.md")
[[ "${#GEN_PATHS[@]}" -gt 0 ]] || { echo "no BOOTSTRAP_PATHS in MANIFEST.md — refusing" >&2; exit 1; }
GEN_EXCL=()
for a in "${RSYNC_EXCLUDES[@]}"; do
  pat="${a#--exclude=}"
  for s in "${GEN_SHIP[@]}"; do [[ "$pat" == "$s" ]] && { GEN_EXCL+=("$pat"); break; }; done
done
{ printf '%s\n' "${GEN_PATHS[@]}"; echo "==="; printf '%s\n' "${GEN_EXCL[@]}"; } > "$GEN_TMP"
GEN_BLOB="$(git hash-object -w "$GEN_TMP")"
printf '100644 blob %s\t%s\0' "$GEN_BLOB" "$GENERATED_BOOTSTRAP_REL" >> "$INDEX_INFO"
printf '%s\n' "$GENERATED_BOOTSTRAP_REL" >> "$KEPT"

# A throwaway index, so the caller's real index is untouched. `mktemp`, not
# `mktemp -u`: -u returns a name and deletes it, which is a create-race, and
# it sat outside the trap so an early failure leaked it. read-tree --empty
# overwrites the empty file mktemp leaves, so nothing is lost by creating it.
GIT_INDEX_FILE="$TMP_INDEX" git read-tree --empty
GIT_INDEX_FILE="$TMP_INDEX" git update-index -z --index-info < "$INDEX_INFO"
TREE="$(GIT_INDEX_FILE="$TMP_INDEX" git write-tree)"

echo "seed files: $(wc -l < "$KEPT")" >&2
echo "tree:       $TREE" >&2
echo "parent:     $PARENT_SHA" >&2

# ---------------------------------------------------------------------------
# Verify the tree that was actually built, and refuse to print a sha on a hit.
#
# Not optional and not a flag. The person running this is the last human
# before an irreversible publication, and a check they have to remember is a
# check that gets skipped. `git grep` against the tree object needs no
# checkout, so there is no cheaper path that skips it.
#
# READ THIS BEFORE TRUSTING A GREEN RUN. The pattern scan below is a
# known-string check and nothing more. It cannot tell you the tree is clean,
# only that it holds no string someone already thought to forbid. That is not
# a hypothetical limit: FORBIDDEN_PATTERNS returned zero over a tree that
# contained a personal Hugging Face account handle, a RunPod pod id and a
# volume id, because none of those spellings is in the list — and the leak
# was caught instead by noticing that 0 of 13 tier:project files were public
# while 24 of 24 others were. A structural comparison found what a content
# scan could not.
#
# So the tier assertion below is not a duplicate of the prune above. The
# prune is the fix; this is the check that the fix ran, stated structurally
# rather than by pattern, because the structural form is the one that
# generalises to the next handle nobody has listed.
# ---------------------------------------------------------------------------
VERIFY_FAILED=0

# The pattern set comes from the pre-push gate, not from a second list here.
# `--list-patterns` resolves FORBIDDEN_PATTERNS minus PREPUSH_EXEMPT under
# that block's own two fail-closed rules (a no-tab exemption line is
# rejected; an exemption whose pattern is not verbatim in FORBIDDEN_PATTERNS
# hard-fails). It is the same question — what must never reach a public
# commit — so it gets the same answer from the same file.
#
# The four exempt patterns are the repo's own identity and its pre-rename
# name, both of which this repo publishes deliberately: CLAUDE.md's Repo
# Scope table names the private Discussion plane as a literal, and
# .autonomous-forever-state is a live runtime path. What stays enforced is
# the set no rename fixes — a proprietary product identifier, a retired
# internal codename, and a real person's login.
PATTERNS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && PATTERNS+=("$line")
done < <(bash "$REPO_ROOT/scripts/check-forbidden-identifiers.sh" --list-patterns) \
  || { echo "error: could not resolve the forbidden-pattern list — refusing to verify" >&2; exit 1; }

[[ "${#PATTERNS[@]}" -gt 0 ]] || {
  echo "error: zero enforced patterns resolved — refusing to verify with an empty list" >&2
  exit 1
}

for pattern in "${PATTERNS[@]}"; do
  hits="$(git grep -I -n -E -e "$pattern" "$TREE" -- . 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    echo "VERIFY FAIL: forbidden pattern present in the built tree: $pattern" >&2
    printf '%s\n' "$hits" | head -20 >&2
    VERIFY_FAILED=1
  fi
done

# Structural assertion: no tier:project memory survived into the tree.
PROJECT_LEFT=0
while IFS= read -r -d '' record; do
  path="${record#*$'\t'}"
  sha="$(awk '{print $3}' <<<"${record%%$'\t'*}")"
  case "$path" in
    "$MEMORY_REL"/*.md)
      [[ "$path" == "$MEMORY_REL/MEMORY.md" ]] && continue
      # Capture the tier as a VALUE and compare it. Every `exit` below is a
      # bare exit (status 0) on purpose: `set -o pipefail` is on, so an awk
      # that exited non-zero would poison the pipeline's status and the `if`
      # would read as "no match" for every file — an assertion that cannot
      # fire. That is exactly what the first version of this block did, and
      # tests/test_build_public_seed.sh's C1 mutant is what caught it.
      tier="$(git cat-file blob "$sha" | awk '
            NR == 1 && $0 != "---" { exit }
            NR > 1 && $0 == "---" { exit }
            NR > 1 && sub(/^tier:[ \t]*/, "") { print; exit }')"
      if [[ "$tier" == "project" ]]; then
        echo "VERIFY FAIL: tier:project memory survived into the tree: $path" >&2
        PROJECT_LEFT=$((PROJECT_LEFT + 1))
        VERIFY_FAILED=1
      fi
      ;;
  esac
done < <(git ls-tree -r -z "$TREE")
echo "verify: tier:project memories in the built tree: $PROJECT_LEFT (must be 0)" >&2

if [[ "$VERIFY_FAILED" -ne 0 ]]; then
  echo "" >&2
  echo "Refusing to print a commit sha. Nothing was pushed and nothing is" >&2
  echo "reachable — fix the tree at source and re-run." >&2
  exit 1
fi
echo "verify: pattern scan clean over the built tree" >&2

COMMIT="$(git commit-tree "$TREE" -p "$PARENT_SHA" <<'MSG'
seed the repo with the engine tree

Development moves here. Commits, PRs, CI and code review happen in this
repo from now on; the generate-and-force-push export that produced the
previous contents is retired.

This is the engine's own tree, minus what stays behind: the archive, the
team's live state, the export machinery itself, the wiki, and the
internal-initiative directories. Everything that makes the project
runnable comes with it — the full test suite, pytest.ini, conftest.py,
the Makefile, the flake, and .gitignore, none of which the export ever
shipped.
MSG
)"

printf '%s\n' "$COMMIT"
