#!/usr/bin/env bash
# tests/lib/repo-root-fixture.sh — materialise an isolated fixture "repo root"
# for suites that invoke hooks/*.py as a subprocess (D#2267).
#
# The problem this solves
# ------------------------
# hooks/sandbox.py's own telemetry directory is:
#   _TELEMETRY_DIR = Path(__file__).resolve().parent.parent / ".autonomous-team" / "hook-events"
# — anchored to wherever the invoked *file* physically lives, with no env
# override. (AUTONOMOUS_TEAM_STATE_DIR only redirects hooks/sandbox.py's
# separate <state_dir>/audit.jsonl write, not this one.) hooks/
# claude_execve_fence.py derives its own _FALLBACK_LOG_DIR the same naive way.
#
# A bash suite that invokes the real $REPO_ROOT/hooks/sandbox.py therefore
# always appends to the LIVE .autonomous-team/hook-events/blocks-<date>.jsonl
# — indistinguishable from every other running agent's own sandbox telemetry.
# That is the mechanism behind D#2267's headline flake.
#
# The only way to redirect that write without changing hooks/sandbox.py's
# production code (the Spec's preferred option — "hooks/ is parked", see
# CLAUDE.md) is to invoke a *copy* of hooks/ rooted somewhere else.
# Path.resolve() follows symlinks back to the real file, so a symlinked copy
# does not move _TELEMETRY_DIR — the files must be materialised (cp) at the
# fixture path so __file__ genuinely points there.
#
# The git-init requirement
# -------------------------
# hooks/sandbox_rules.py's tier classification (classify_cwd,
# is_foreign_self_governed) separately derives "the main repo root" from
# hooks/repo_root.py's OWN __file__, and — independently of _TELEMETRY_DIR —
# refuses to grant the "team_lead" tier unless that derivation passed a
# real-git-dir confidence check: "the derived root is only trustworthy when
# the HEAD-file check ... passed ... tier down instead of granting the most
# permissive tier on ambiguous evidence" (hooks/sandbox_rules.classify_cwd).
# A bare `mktemp -d` fixture has no .git at all, so every fixture cwd would
# silently downgrade to "untrusted" and every assert_allowed keyed on the
# Team Lead / worktree tiers would flip to a false failure — a correctness
# bug in the fixture, not in the code under test.
#
# `git init -q` at the fixture root is the cheapest way to satisfy that
# confidence check honestly (a real .git directory with a real HEAD file),
# without adding a bypass to hooks/sandbox_rules.py or hooks/repo_root.py
# to serve a test. No commit is needed — _is_real_git_dir only requires
# `.git` to be a directory containing a HEAD file.
#
# Usage:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/repo-root-fixture.sh"
#   FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || exit 1
#   trap 'rm -rf "$FIXTURE_ROOT"' EXIT
#   HOOK="$FIXTURE_ROOT/hooks/sandbox.py"
#   MAIN_REPO="$FIXTURE_ROOT"
#   WT_CLAUDE="$FIXTURE_ROOT/.claude/worktrees/testid123"
#   BLOCKS_FILE="$FIXTURE_ROOT/.autonomous-team/hook-events/blocks-$(date +%F).jsonl"
#
# repo_root_fixture_make <real_repo_root>
#   Creates a mktemp -d fixture, `git init -q`s it, and copies every
#   hooks/*.py file (the real code under test, byte-for-byte, unmodified)
#   from <real_repo_root>/hooks/ into <fixture>/hooks/. Prints the fixture
#   path on stdout. The caller owns cleanup (rm -rf) — this function does
#   not register a trap, since a suite sourcing this file already has its
#   own trap conventions (see D#2254 on not centralizing traps in a helper).
#
# Deliberately NOT under /tmp or /var/tmp
# -----------------------------------------
# hooks/sandbox_rules._is_ephemeral_tmp_path() treats every path under /tmp
# or /var/tmp as "ephemeral filesystem, not repo state, always safe" and
# exempts it from every write-outside-worktree check. D#2149 already named
# this trap for differential harnesses: fixtures placed under /tmp make
# every verdict identical on both sides, so the harness silently measures
# nothing. It applies here for the same reason, just via a different
# symptom — a fixture rooted under /tmp would make hooks/sandbox.py itself
# ALLOW writes this suite expects to be BLOCKED (an assert_blocked case
# quietly flips to a false PASS-as-allow, i.e. a real test failure, not a
# measurement gap), because the "target outside worktree" it is asserting
# against textually resolves under /tmp (measured while writing this suite:
# every D#1756/D#1792 write-outside-worktree case silently started PASSing
# as allow the moment the fixture briefly lived under /tmp).
#
# Not under <real_repo_root>/.git/ either. In the main checkout .git is a
# real directory and this would work, but a linked worktree's .git is a
# *file* (the whole point of hooks/repo_root.py — see its module docstring),
# so mktemp -d against it fails outright; and even where it is a directory,
# that path is the git-common-dir shared by every worktree of this repo, so
# writing scratch data into the real checkout's .git/ from a worktree-
# isolated agent is exactly the parent-checkout write the sandbox exists to
# stop. The fixture is created as a plain subdirectory of <real_repo_root>
# instead — always writable by whoever is running the suite (their own
# worktree or the main checkout), never under /tmp, and dot-prefixed
# (.repo-root-fixture.*, gitignored below) so a crash that skips the
# caller's cleanup trap doesn't show up as tracked-looking clutter.
repo_root_fixture_make() {
  local real_repo_root="${1:?repo_root_fixture_make: real repo root argument required}"
  local fixture

  fixture="$(mktemp -d "$real_repo_root/.repo-root-fixture.XXXXXX")" || return 1

  if ! git init -q "$fixture" >/dev/null 2>&1; then
    rm -rf "$fixture"
    return 1
  fi

  if ! mkdir -p "$fixture/hooks"; then
    rm -rf "$fixture"
    return 1
  fi

  if ! cp "$real_repo_root"/hooks/*.py "$fixture/hooks/" 2>/dev/null; then
    rm -rf "$fixture"
    return 1
  fi

  printf '%s\n' "$fixture"
}
