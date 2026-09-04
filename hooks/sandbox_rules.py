"""hooks/sandbox_rules.py

Pure-function sandbox rules for the sub-agent PreToolUse hook.

All logic lives here so it can be unit-tested without subprocess or I/O.
sandbox.py is the thin I/O shell that imports these.

Worktree root patterns recognised (``<main-repo-root>`` is derived at import by
``hooks/repo_root.py``, never hardcoded):
  <main-repo-root>/.claude/worktrees/<id>/
  /tmp/wt-<id>/

Team Lead identity: resolved CWD == main repo root (or under it) but NOT
under any worktree root.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from hooks.repo_root import (
    derive_main_repo_root,
    is_main_repo_root_confident,
    resolve_main_repo_root,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Derived from this file's location, corrected for the worktree case (see
# hooks/repo_root.py). A literal here is what left this repo unprotected: when
# the literal does not exist on the host, every worktree of this repo matches
# the foreign-team predicate below (is_foreign_self_governed) and the hook
# defers to it instead of sandboxing it — exit 0 for every Bash call, every
# Agent() spawn, and every merge, before any tier logic runs.
MAIN_REPO_ROOT = resolve_main_repo_root()

# The env-independent floor (see hooks/repo_root.py). SANDBOX_MAIN_REPO_ROOT
# cannot lift it: worktree/foreign-team checks below always also test against
# this value, so an override can tier *synthetic* fixture paths but can never
# promote a real worktree of this repo to team_lead or hide it as foreign.
_DERIVED_MAIN_REPO_ROOT = derive_main_repo_root()

# Ordered list of glob-style prefix patterns for worktree roots.
# We match by checking if the resolved CWD starts with any of these prefixes.
_WORKTREE_PREFIXES = [
    str(MAIN_REPO_ROOT / ".claude" / "worktrees") + "/",
    str(_DERIVED_MAIN_REPO_ROOT / ".claude" / "worktrees") + "/",
    "/tmp/wt-",
]

# Git subcommands that are read-only and always allowed, regardless of effective CWD.
_GIT_READONLY_VERBS: frozenset[str] = frozenset(
    [
        "fetch",
        "log",
        "status",
        "diff",
        "show",
        "rev-parse",
        "ls-files",
        "cat-file",
        "for-each-ref",
        "config",  # with --get / read-only usage
        "remote",
    ]
)

# Git subcommands that are ALWAYS blocked from a worktree context, regardless of
# effective CWD.  These are branch-state manipulation commands that can flip
# HEAD or delete refs in the parent repository even when run within the worktree.
#
# D#2058: membership alone used to be the whole test, so `git branch`, `git
# branch --list`, and `git reset --help` — none of which touch any ref —
# blocked identically to `git branch -D foo`. Only `branch` and `worktree`
# have a read-only spelling other than `--help`; `checkout`, `switch`,
# `reset`, `clean`, `restore` do not (see _is_git_readonly_invocation below,
# which is what classify_bash now consults per-invocation before blocking on
# membership in this set — the set itself is unchanged).
_GIT_ALWAYS_BLOCKED_VERBS: frozenset[str] = frozenset(
    [
        "checkout",
        "switch",
        "branch",
        "reset",
        "clean",
        "worktree",
        "restore",
    ]
)

# `git branch` flags that only read/list branch state — no ref is created,
# deleted, moved, or retargeted by any of these (D#2058). Deliberately an
# allowlist, not a denylist of known-write flags: an unrecognised flag must
# fail closed (Spec criterion 6), and enumerating every future write flag
# instead of every current read flag is the wrong side to be wrong on for a
# verb that creates/destroys refs.
_GIT_BRANCH_READONLY_FLAGS: frozenset[str] = frozenset(
    [
        "--list",
        "-l",
        "--show-current",
        "-a",
        "--all",
        "-v",
        "--verbose",
        "--merged",
    ]
)


def _is_git_readonly_invocation(verb: str, args: list[str]) -> bool:
    """Return True if this specific `git <verb> <args>` invocation is a
    read-only spelling that should escape `_GIT_ALWAYS_BLOCKED_VERBS` (D#2058).

    `--help`/`-h` never executes the verb, for ANY of the seven always-blocked
    verbs — a documentation lookup, not a write, regardless of which verb it
    follows. `branch` and `worktree` additionally have read-only spellings of
    their own (see the module docstring comment above _GIT_ALWAYS_BLOCKED_VERBS
    for why `checkout`/`switch`/`reset`/`clean`/`restore` do not get one here):

      - `branch`: safe only when EVERY arg is a known read-only flag — any
        other arg (a branch name to create, `-D`/`-d`/`-m`/`-M`, an
        unrecognised flag) fails closed to "not read-only" (Spec criterion 6).
      - `worktree`: safe only when its first arg is the `list` subcommand —
        `add`/`remove`/`prune`/`move`/an unrecognised subcommand all fail
        closed the same way.

    This is a per-INVOCATION check (verb + that invocation's own args), not a
    per-command one — `git log;git reset --hard origin/main` still blocks on
    the `reset` invocation even though `log` (not always-blocked at all)
    precedes it, because the caller (classify_bash) evaluates every
    always-blocked-verb invocation in the command, not just one.
    """
    if any(a in ("--help", "-h") for a in args):
        return True
    if verb == "branch":
        return all(a in _GIT_BRANCH_READONLY_FLAGS for a in args)
    if verb == "worktree":
        return bool(args) and args[0] == "list"
    return False

# Git subcommands that are write operations but are permitted within the worktree
# (executor workflow: commit changes and push the branch).
# They are still blocked if effective CWD escapes the worktree root.
_GIT_WRITE_VERBS: frozenset[str] = frozenset(
    [
        "merge",
        "rebase",
        "cherry-pick",
        "push",
        "tag",
        "commit",
        "am",
        "apply",
        "stash",
        "bisect",
    ]
)

# Shell tokens that indicate output redirection to a path.
#
# Only match the redirect operator when it is at start-of-string or preceded
# by whitespace.  This excludes file-descriptor redirects like `2>/dev/null`,
# `2>&1`, `&>/dev/null`, and `1>>file` where the operator is preceded by a
# digit or `&`.  Those are shell fd-routing constructs, not path writes.
#
# Specifically, a match requires:
#   - Positive lookbehind: start-of-string OR whitespace character before `>`
#   - Followed by optional whitespace and an absolute path
#
# This intentionally does NOT match:
#   2>/dev/null   — digit before `>`
#   2>&1          — digit before `>` (and `&1` is not an abs path)
#   &>/dev/null   — `&` before `>`
#   1>>/dev/null  — digit before `>`
#
_REDIRECT_PATTERN = re.compile(
    r"""(?:(?<=\s)|(?<=^))(?:>>|>)\s*(/[^\s;|&]+)""",
    re.VERBOSE | re.MULTILINE,
)

# D#1898 round 2 (B1, security review of PR #1901): `~`/`$HOME`-prefixed
# redirect targets are matched via a SEPARATE, TOKENISED scan
# (_home_prefixed_redirect_targets, below _expand_home_prefix), not by
# widening the raw-regex pattern above. The raw regex runs over the unparsed
# command string with no quote or heredoc awareness — round 1 widened its
# alternation to also match `~`/`$HOME`, and that turned any MENTION of a
# home path into a false "write": `gh pr comment --body 'echo x >
# ~/.bashrc'`, `echo 'writes go to > ~/.bashrc'`, and a heredoc that writes a
# test file containing a quoted `"echo x > ~/.claude/settings.json"` string
# all blocked, even though none of them write anywhere. Tokenising with
# shlex (same approach resolve_effective_cwd already uses) fixes the quoted-
# argument case: shlex folds a quoted string into one opaque token, so a `>`
# living inside quotes never becomes a standalone redirect-operator token.
#
# The `/`-prefixed regex above is left exactly as it was before D#1898 round
# 1 — it has the same quote-blindness in principle, but it works today and
# rewriting it here would widen the blast radius of this fix for no gain
# (see B1 in the round-2 security review of PR #1901).

# Kernel virtual devices that are never real file writes.
# Redirects to these paths are always safe and must never be blocked.
_KERNEL_DEVICE_PREFIXES: tuple[str, ...] = (
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/fd/",
    "/dev/stdin",
)

# Commands that write to a destination path as their last argument.
_PATH_WRITE_COMMANDS = frozenset(["tee", "cp", "mv", "install", "rsync"])


def _expand_home_prefix(path: str) -> str:
    """Expand a leading ``~``, ``~/``, ``$HOME``, or ``${HOME}`` in *path* to
    the real home directory (D#1898).

    Targets the common ACCIDENTAL spellings of "my home directory" a sub-agent
    might reach for out of habit — not a general shell-expansion engine.
    Deliberately does NOT handle, and this is an acknowledged, documented gap
    rather than an oversight:

      - bare ``~user`` (someone else's home dir) — correct resolution needs a
        passwd lookup this pure-string module has no business doing, and it
        is not a spelling anyone reaches for by accident.
      - ``~`` used mid-argument (e.g. ``/foo/~/bar``) — real shells only
        expand ``~`` in leading position anyway, so this mirrors actual
        semantics rather than under-covering it.
      - ``$HOME``/``${HOME}`` used mid-argument (e.g. ``/foo/$HOME/bar``) —
        unlike ``~``, a real shell DOES expand ``$HOME`` anywhere in a word,
        so this genuinely under-covers that spelling rather than mirroring
        shell semantics. Kept unhandled anyway: no live repro has ever used
        mid-argument `$HOME`, and this module's static-string approach can't
        tell a real `$HOME` reference from a `$HOME` that's part of a longer
        identifier without doing real word-splitting. Called out separately
        from the `~` case above so this note doesn't assert a property the
        code doesn't deliver (D#1898 round 2 security review, PR #1901).
      - variable indirection (``X=~; cmd $X``), ``pushd``/``popd``, subshells,
        or ``env HOME=... cmd`` — see the module note above
        _classify_unenumerated_write for why a string-matching classifier
        can't chase this space, and D#1898 for the explicit decision not to
        (the owner's guidance: catch the common accidental spellings, don't
        chase exhaustiveness that a static classifier can't deliver anyway).
    """
    home = os.path.expanduser("~")
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    if path.startswith("${HOME}"):
        return home + path[len("${HOME}"):]
    if path == "$HOME" or path.startswith("$HOME/"):
        return home + path[len("$HOME"):]
    return path


# Pre-compiled pattern for git rm detection — kept for classify_git_rm word-boundary guard
# and the untokenisable fallback path ONLY.  Do NOT use as the fast-path gate in
# is_real_git_rm_invocation: its lookbehind and `\s+rm` adjacency requirement both create
# bypass vectors:
#   • `;git rm foo`     — `;` before `git` fails the lookbehind → pattern misses it
#   • `git -C p rm foo` — flags between `git` and `rm` fail `\s+rm` → pattern misses it
# is_real_git_rm_invocation uses _GIT_BROAD_PATTERN and normalises separators first.
_GIT_RM_PATTERN = re.compile(r"(?<![^\s])\bgit\s+rm\b")

# Broad pre-filter: any word-boundary `git` token anywhere in the command string.
# is_real_git_rm_invocation uses this (not _GIT_RM_PATTERN) as its fast-path gate so that
# separator normalisation always runs before the authoritative token-walker check.
_GIT_BROAD_PATTERN = re.compile(r"\bgit\b")

# ---------------------------------------------------------------------------
# Dial registry protection — filenames that sub-agents must never write to.
# Only the Team Lead path (not in a worktree) may mutate these files.
# ---------------------------------------------------------------------------

# External-intake approval baseline store (D#1672) — a self-approval
# privilege surface (security-expert SEC-5): whoever can write this file
# can forge "the content a human reviewed", so it gets the same
# Team-Lead-only protection as the dial registry and the audit log.
_EXTERNAL_INTAKE_BASELINES_FILENAME = "external-intake-baselines.json"

# Exact filename suffixes that are protected.  Checked against the resolved
# absolute path of any Edit/Write call, and (as of SEC-6 / D#1672 round 4,
# broadened in SEC-8 / D#1672 round 5) against EVERY operand of a Bash call
# — absolute, relative, or `~`-prefixed — see
# _all_path_operands() / _protected_basename_operand() and classify_bash()
# step 1d, not just output-redirect/copy-destination targets. That extension
# is what makes deletion verbs (`rm`, `unlink`, `mv <marker> <dst>`'s source
# argument) blocked rather than only writes, regardless of how the path to
# the protected file is spelled.
_DIAL_PROTECTED_SUFFIXES: tuple[str, ...] = (
    "dial-registry.json",
    "dial-directive-allowlist.json",
    "audit.jsonl",
    # SEC-1 fix (D#1883 security review round 2): provision-dial-allowlist.sh
    # writes dial-directive-allowlist.json from inside its own process, which
    # is invisible to this operand scan — so blocking the FILE alone still
    # let a worktree sub-agent through a documented side door: hit the
    # allowlist deny, get told (by the old refusal message) to run this
    # script, and the bare invocation (default STATE_DIR, no explicit
    # AUTONOMOUS_TEAM_STATE_DIR) sailed straight past the path-token scan
    # below and self-authorized against the live state dir. Blocking the
    # script's own basename here — the same sibling-tuple mechanism that
    # already protects the file it writes — closes the direct-invocation
    # spelling. A human operator (or Team Lead, outside any worktree cwd)
    # can still run it; only a worktree sub-agent's Bash operand is blocked.
    "provision-dial-allowlist.sh",
    _EXTERNAL_INTAKE_BASELINES_FILENAME,
    # SEC-4 fix (D#1672 round 3): the store's own init-marker sentinel is the
    # ONLY thing that makes deletion of the store fail closed (see
    # scripts/lib/intake_baseline.py::read_baselines() / _marker_path()). If
    # the marker itself were left unprotected, a worktree sub-agent that
    # cannot touch the store could still delete the marker, then delete the
    # store, reproducing the original SEC-2 bypass verbatim (a lower-
    # privileged actor than the one this protection was written for). The
    # basename is derived from the filename above — the exact same
    # f".{name}.initialized" expression intake_baseline.py::_marker_path()
    # uses — rather than hand-typed a second time, so the two cannot
    # silently drift apart.
    #
    # SEC-6 fix (D#1672 round 4, Kai round-3 review): round 3 only added this
    # marker to the list that classify_path_write() and the write-command
    # target scan check — `rm`, `unlink`, and `mv <marker> <dst>`'s source
    # argument aren't writes to a "target" in that sense, so they sailed
    # straight through and reproduced the SEC-2 bypass with one `rm -f`. Fixed
    # structurally (classify_bash() step 1d), not by adding `rm` to a verb
    # list.
    #
    # SEC-8 fix (D#1672 round 5, Kai round-4 review): the round-4 operand scan
    # generalised over verbs but not over path *spellings* — it required
    # `os.path.isabs(tok)` and an exact basename `==`, so `rm ~/<state>/<marker>`
    # (shlex never expands `~`, so isabs is False), `cd <state> && rm <marker>`
    # (relative token never reached the check), and `rm ../../../<state>/<marker>`
    # (dotdot-relative) all reproduced the identical SEC-2 bypass. Fixed by
    # dropping the isabs gate so every token reaches the basename check,
    # regardless of whether it looks absolute (see _protected_basename_operand()).
    #
    # SEC-11 fix (D#1672 round 6, Kai round-5 review): round 5 also made the
    # basename check glob-match, which over-blocked ordinary commands
    # containing a bare `*`, `*.json`, or `*.jsonl` token (`rm -rf *`,
    # `git add *`, ...) since `*` matches every protected name. Reverted to
    # exact match; glob and brace-expansion spellings move into the residual
    # gap below.
    #
    # Honest limit, still accurate after SEC-11: a lexical token scan cannot see
    # a path assembled inside `python3 -c "..."`, `find -delete`, a shell
    # variable (`$VAR`), or a glob/brace-expansion spelling of the protected
    # basename (`*.initialized`, `{d,}`) — those require the attacker to
    # already know the control exists and deliberately route around it, unlike
    # the literal spellings above, which are just how the command is
    # ordinarily typed. That residual gap is accepted and documented, not
    # silently claimed as closed: the durable fix is moving "store was
    # initialized" out of the filesystem into the append-only audit trail or
    # state.db, which this round does not attempt.
    f".{_EXTERNAL_INTAKE_BASELINES_FILENAME}.initialized",
)

# Pre-compiled pattern for gh merge detection.
_GH_MERGE_PATTERN = re.compile(
    r"""
    (?:
        gh\s+pr\s+merge        # gh pr merge <n>
      | gh\s+api\s+.*          # gh api ... (checked further below)
    )
    """,
    re.VERBOSE,
)

_GRAPHQL_MERGE_PATTERN = re.compile(r"mergePullRequest", re.IGNORECASE)
_REST_MERGE_PATTERN = re.compile(
    r"gh\s+api\s+.*-X\s+PUT\s+.*\/pulls\/\d+\/merge|"
    r"gh\s+api\s+.*repos\/.*\/pulls\/\d+\/merge",
)

# ---------------------------------------------------------------------------
# claude-spawn deny-list (D#439)
# ---------------------------------------------------------------------------

# Normalise away interior shell quotes/backslashes so cl"au"de → claude.
# We only strip quotes that appear *inside* a token (not the delimiters
# that shlex already removed); applying this to the raw string before
# tokenisation catches variants like cl"au"de -p "...".
_QUOTE_STRIP_RE = re.compile(r"""["'\\]""")

# Forbidden path fragments that should never be EXECUTED by a subagent's Bash
# command. These cover the exact paths from the 2026-05-10 runaway incident.
#
# D#2058: check_claude_spawn no longer tests these against the raw command
# string (that matched a MENTION anywhere — `cat run-loop-iteration.sh`,
# `gh pr edit --body '...backend/trigger.py'` — not just an execution). The
# fragments themselves are unchanged; only where they're tested moved to
# specific tokens (see _check_forbidden_fragment_positions below).
_FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    "_start_loop_run",
    "loop-trigger",
    "run-loop-iteration.sh",
    "backend/trigger.py",
    "spawn-agent.sh",
)

# Interpreter / sourcing forms whose non-flag operands are executed, not just
# read (D#2058). `python3 backend/trigger.py` runs the file named in its own
# operand; `cat backend/trigger.py` only reads it — the distinguishing factor
# is whether argv[0] is one of these.
_INTERPRETER_TOKENS: frozenset[str] = frozenset(
    ["bash", "sh", "zsh", "python", "python3", "source", ".", "env", "exec"]
)

# Interpreters whose `-c` flag hands the REST of the command to the
# interpreter as ONE opaque inline-script string (`bash -c '<script>'`,
# `python3 -c "<script>"`). By the time check_claude_spawn's quote-stripped
# `tokens` reach _check_forbidden_fragment_positions, that string's quoting is
# already gone (quote-stripping is required upstream to catch
# cl"au"de-style obfuscation) — so its shredded pieces would read as bare
# top-level operands, and a `-c` payload that only MENTIONS a fragment as
# a string literal (`python3 -c "print('see run-loop-iteration.sh')"`,
# `bash -c 'echo see run-loop-iteration.sh'`) would wrongly match. For
# bash/sh, the dedicated recursion in check_claude_spawn (step 3) is the
# authority for what such a payload actually executes, re-parsing the
# ORIGINAL unstripped string; python/python3 -c payloads have no equivalent
# recursive check for this rule (their own os.exec*/subprocess-claude
# bypass gap is handled separately, step 7) — skipping them here is a
# narrow, deliberate under-block, the trade CLAUDE.md calls acceptable.
_DASH_C_SHELLS: frozenset[str] = frozenset(["bash", "sh", "zsh", "python", "python3"])

# Shell tokens that act as pipeline/list separators — command starts after these.
_SHELL_SEPARATORS: frozenset[str] = frozenset(["&&", "||", ";", "|", "&"])

# Shell separators extended with `(` / `)` / `{` / `}` for the git-rm token-walker so
# that subshell expressions like `(git rm f)` and brace groups like `{ git rm f; }` are
# caught.  Not added to _SHELL_SEPARATORS (shared constant) to avoid unintended
# side-effects on other callers.
_GIT_RM_WALK_SEPARATORS: frozenset[str] = _SHELL_SEPARATORS | frozenset(["(", ")", "{", "}"])

# Shell separators extended with `(` / `)` / `` ` `` for the git-verb walker
# (_extract_git_verb / _extract_all_git_verbs) so that subshell / command-substitution
# forms like `(git worktree remove x)`, `true;$(git checkout main)`, and
# `` `git checkout main` `` create a command position the same way `;` does (D#1729 F1).
# `{`/`}` are deliberately excluded here (unlike _GIT_RM_WALK_SEPARATORS) — brace groups
# aren't part of the verified repro set for this walker and adding them is out of scope
# for this fix.
_GIT_VERB_WALK_SEPARATORS: frozenset[str] = _SHELL_SEPARATORS | frozenset(["(", ")", "`"])

# Global git options that consume a SEPARATE next token as their value (i.e. `-c
# <value>`, not glued `-c<value>`). The verb walker must skip both the option token
# and its value token, or the value token gets misread as the verb — e.g. without
# `-c` in this set, `git -c user.name=x checkout main` reads "user.name=x" as the
# verb, "checkout" is never inspected, and the always-blocked check never fires
# (D#1729 round-3 F4). `-C`/`--git-dir`/`--work-tree`/`--namespace` were already
# handled pre-round-3; `-c`/`--exec-path`/`--config-env`/`--super-prefix`/
# `--attr-source` are git's other value-taking global options and have the exact
# same displacement effect.
_GIT_VALUE_TAKING_GLOBAL_OPTS: frozenset[str] = frozenset(
    [
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
        "--super-prefix",
        "--attr-source",
    ]
)

# Subset of _GIT_VALUE_TAKING_GLOBAL_OPTS whose value is a directory path that
# changes WHERE this specific git invocation actually operates (D#1746). `-c`,
# `--namespace`, `--exec-path`, `--config-env`, `--super-prefix`, and
# `--attr-source` all consume a value token too, but none of them relocate the
# invocation the way `-C`/`--git-dir`/`--work-tree` do — treating them as CWD
# overrides would be wrong, and NOT skipping their value would misread the
# value as the verb (the original F4 bug). Both walkers below need this
# distinction: skip-the-value applies to all of _GIT_VALUE_TAKING_GLOBAL_OPTS,
# override-the-CWD applies only to this narrower set.
_GIT_CWD_TAKING_OPTS: frozenset[str] = frozenset(["-C", "--git-dir", "--work-tree"])


def _split_glued_git_option(tok: str) -> tuple[str, Optional[str]]:
    """Split a git long option's glued `--opt=value` form into (name, value).

    Git accepts EVERY value-taking global long option in this glued form —
    `--git-dir=<path>` is standard, documented git CLI syntax, not an edge
    case, and it is the MORE common spelling in scripts (`--work-tree
    <path>` needs the value as a separate, quotable argument; `=` glues it
    inline). round 2 (post-review) found the walker below matched
    `_GIT_CWD_TAKING_OPTS` by exact token equality, so `--git-dir=<path>`
    and `--work-tree=<path>` fell through as ordinary unrecognised flags —
    the CWD override was silently skipped and a write invocation using only
    the glued spelling read as staying inside the worktree when it hadn't.

    Modelled ONCE, generally, rather than special-cased per option name: any
    `--`-prefixed token containing `=` splits into (name-before-first-`=`,
    value-after-first-`=`); anything else returns (tok, None) so the
    existing exact-match membership checks are unaffected by tokens that
    were never in this glued shape to begin with — including bare `-C`/`-c`,
    which are short options and do not take this form. This is what lets a
    future value-taking global option (or any of the existing non-CWD ones,
    `--namespace=x` etc.) get the same glued-form coverage for free, instead
    of needing its own special case the next time this comes up.
    """
    if tok.startswith("--") and "=" in tok:
        name, _, value = tok.partition("=")
        return name, value
    return tok, None

# A real git subcommand is a bare word: letters, digits, hyphens, starting with a
# letter (checkout, worktree, reset, log, config-env-adjacent verbs like `lfs`, etc).
# Used as a guard before appending a candidate token to the verbs list — anything
# that doesn't look like a verb (a `-c` config pair like `user.name=x`, a bare
# backslash-newline continuation artifact, punctuation) is skipped rather than
# misread as the verb, closing the same displacement class as
# _GIT_VALUE_TAKING_GLOBAL_OPTS from the other direction: an *unrecognised* option
# or most stray tokens can't shift the walker onto a false verb (D#1729 round-3
# F4/F5). This guard does NOT cover every displacement vector, though — a
# shell-redirection-operator fragment like `2>&1` still slips past it and can
# displace the real verb (D#1748, F7). `in_git` is left True when a candidate is
# rejected, so the walker keeps looking for the real verb later in the same
# invocation instead of giving up.
_GIT_VERB_SHAPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")

# Environment-setting prefixes that precede the actual command.
# e.g. `env FOO=bar claude -p ...` — we skip over env-var assignments.
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Command-prefix wrappers that run a REAL command as their own operand
# without executing anything themselves (D#2058 fix-cycle 1, security
# review). `timeout 60 bash run-loop-iteration.sh`, `nohup bash
# run-loop-iteration.sh &`, and the bounded-run form this repo's own agent
# briefs mandate (`timeout --kill-after=5s 600 pytest ...`) all fell
# through _check_forbidden_fragment_positions' two shapes because
# _command_positions treated the WRAPPER token itself as argv[0] — the real
# command it wraps never became its own position, so neither shape ever saw
# it. Deliberately NOT added to _INTERPRETER_TOKENS: that set's operands are
# scanned directly for a forbidden fragment, which would make `timeout 60
# cat run-loop-iteration.sh` (a read, not a run) block — exactly the
# over-block this rule exists to remove. Skipping the wrapper here just
# lets the walker keep looking for the command it wraps, which is then
# judged normally by both shapes.
_COMMAND_WRAPPER_TOKENS: frozenset[str] = frozenset(
    ["timeout", "nohup", "nice", "stdbuf", "setsid", "time", "ionice", "xargs"]
)

# A bare numeric operand — GNU `timeout`'s duration (`60`, `300`, or with a
# single-letter suffix: `5s`, `10m`, `2h`, `1d`). Only consulted while
# already skipping PAST a wrapper token (see _command_positions) — a real
# command that happens to start with a digit is not something this rule
# needs to worry about.
_WRAPPER_NUMERIC_OPERAND_RE = re.compile(r"^[0-9]+[smhd]?$")

# Bypass-gap patterns added in D#439 security review.
#
# D#2099: these are now used only as a cheap CANDIDATE filter — "does this
# substitution mention claude anywhere" — not as the final verdict. Every
# agent's scratchpad path contains the literal substring "claude-1000", so a
# perfectly ordinary read like `$(cat /tmp/.../claude-1000/x/f.json)` used to
# match and get refused as a spawn attempt (80 logged events, 0 genuine — see
# D#2099 measurement). The candidates these regexes find are now handed to
# _substitution_is_claude_spawn, which tokenises the substitution's inner text
# and asks whether `claude` is actually in COMMAND POSITION there, reusing
# _command_positions/_is_claude_token exactly as PR #2095 did for the
# forbidden-fragment check. Genuine spawn shapes ($(claude ...), `claude ...`,
# wrapped/env-prefixed/absolute-path variants of the same) still deny.
#
# Matches dollar-paren command substitution containing "claude": $(claude ...) or $(... claude ...)
_DOLLAR_PAREN_CLAUDE_RE = re.compile(r"\$\([^)]*claude[^)]*\)")
# Matches backtick command substitution containing "claude": `claude ...` or `... claude ...`
_BACKTICK_CLAUDE_RE = re.compile(r"`[^`]*claude[^`]*`")
# Matches python3 -c / python -c payloads that exec claude via os.exec* or subprocess
_PYTHON_EXEC_CLAUDE_RE = re.compile(
    r"(?:os\.execvp|os\.execv|subprocess\.run|subprocess\.Popen)\s*\(\s*[\[\(]?[^)]*claude",
    re.DOTALL,
)


def _is_claude_token(tok: str) -> bool:
    """Return True if *tok* is a claude executable reference (after quote-strip).

    Matches:
      claude             (bare name)
      /path/to/claude    (absolute path ending in /claude)
      ./claude           (relative path)

    Does NOT match:
      CLAUDE.md          (upper-case, file extension)
      claude-code        (longer hyphenated name — not a spawn)
    """
    # Strip interior quotes/backslashes to catch cl"au"de / cl'au'de
    normalised = _QUOTE_STRIP_RE.sub("", tok)
    # Must end with /claude or equal "claude" exactly.
    # We accept only "claude" with no suffix, or flags-suffixed forms handled
    # separately in the caller.
    return normalised == "claude" or normalised.endswith("/claude")


def _is_git_token(tok: str) -> bool:
    """Return True if *tok* is a git executable reference: exactly "git" or ending in "/git".

    Matches:
      git              (bare name)
      /usr/bin/git     (absolute path ending in /git)
      ./git            (relative path)
      /path/to/git     (any absolute path ending in /git)

    Does NOT match:
      mygit            (does not end with /git; different binary)
      git-rm           (hyphenated form; handled as documented non-target)
    """
    return tok == "git" or tok.endswith("/git")


def _command_positions(
    tokens: list[str],
    *,
    extra_separators: frozenset[str] = frozenset(),
    skip_command_builtin: bool = False,
) -> list[int]:
    """Return the indices in *tokens* where a new command starts.

    A new command starts at index 0 and after any shell separator (;, &&, ||, |, &).
    Env-var assignments (FOO=bar) are skipped when looking for the executable position.

    D#2058 fix-cycle 1: command-prefix wrappers (_COMMAND_WRAPPER_TOKENS —
    `timeout`, `nohup`, `nice`, `stdbuf`, `setsid`, `time`, `ionice`,
    `xargs`) are ALSO skipped, along with their own flags and (for
    `timeout`) a bare numeric duration operand, so the position landed on is
    the command the wrapper runs, not the wrapper itself. Every consumer of
    this function (the forbidden-fragment position check, the claude-token
    check, and the `command claude` check) benefits — a wrapped command was
    previously invisible to all three.

    D#2225 round 2: `extra_separators` and `skip_command_builtin` are opt-in
    generalisations so a NEW caller can get the subshell/brace-group
    coverage `is_real_git_rm_invocation` proved out, and the `command`
    builtin skip `check_claude_spawn` proved out, WITHOUT forking a second
    copy of this walker that could drift from this one. Both default to
    off, so every existing caller (which calls this with tokens only) is
    byte-for-byte unaffected.
    """
    separators = _SHELL_SEPARATORS | extra_separators if extra_separators else _SHELL_SEPARATORS
    positions: list[int] = []
    next_is_cmd = True
    after_wrapper = False
    for i, tok in enumerate(tokens):
        if tok in separators:
            next_is_cmd = True
            after_wrapper = False
            continue
        if next_is_cmd:
            # Skip env-var assignments to find the actual command token
            if _ENV_PREFIX_RE.match(tok) and "=" in tok and not tok.startswith("-"):
                # Keep next_is_cmd True to find the actual executable
                continue
            # Skip `env` command (used as `env FOO=bar prog`)
            if tok == "env":
                continue
            # Skip the `command` builtin (bypasses alias/function interception;
            # the real executable is the NEXT token) — opt-in, see docstring.
            if skip_command_builtin and tok == "command":
                continue
            # Skip a command-prefix wrapper — the real command is further along.
            if tok in _COMMAND_WRAPPER_TOKENS:
                after_wrapper = True
                continue
            # While still working through a wrapper's own flags/operands
            # (`--kill-after=5s`, `-n 10`, a bare `60`), keep looking rather
            # than mistaking one of them for the command it wraps.
            if after_wrapper and (
                tok.startswith("-") or _WRAPPER_NUMERIC_OPERAND_RE.match(tok)
            ):
                continue
            after_wrapper = False
            positions.append(i)
            next_is_cmd = False
    return positions


def _contains_forbidden_fragment(token: str) -> Optional[str]:
    """Return the first `_FORBIDDEN_FRAGMENTS` entry that is a substring of
    *token*, or None.

    Matches on a single TOKEN, never on the raw command string — that is
    what makes the caller's check positional rather than the blanket
    substring-over-the-whole-string test this replaces (D#2058). Substring
    (not exact-equality) on the token is intentional: two of the five
    fragments (`_start_loop_run`, `loop-trigger`) are deliberately partial —
    they match `backend/_start_loop_run.py` and `scripts/loop-trigger.sh`
    without their extensions, unchanged from the original fragment set.
    """
    for fragment in _FORBIDDEN_FRAGMENTS:
        if fragment in token:
            return fragment
    return None


def _check_forbidden_fragment_positions(tokens: list[str]) -> Optional[str]:
    """Return the forbidden fragment actually being EXECUTED by *tokens*, or
    None if every occurrence is a read or a mention (D#2058).

    A fragment is executed in exactly two shapes:
      (a) it IS argv[0] of some command in the pipeline — `./run-loop-iteration.sh`
      (b) argv[0] is an interpreter/sourcing form (_INTERPRETER_TOKENS) and the
          fragment is one of THAT invocation's own non-flag operands —
          `python3 backend/trigger.py`

    A fragment appearing as an operand of an ordinary (non-interpreter)
    command — `cat run-loop-iteration.sh`, `grep ... backend/trigger.py`,
    `git log -- run-loop-iteration.sh` — or inside a quoted string argument
    that survived quote-stripping as plain words (`gh pr edit --body '...
    backend/trigger.py'`, `echo '...spawn-agent.sh...'`) is a read or a
    mention, not an execution, and must NOT match either shape.
    """
    positions = _command_positions(tokens)
    for idx, pos in enumerate(positions):
        argv0 = tokens[pos]
        end = positions[idx + 1] if idx + 1 < len(positions) else len(tokens)

        # Shape (a): the fragment IS the thing being run.
        match = _contains_forbidden_fragment(argv0)
        if match is not None:
            return match

        # Shape (b): argv0 hands a fragment to an interpreter/sourcing form
        # as one of its own operands.
        if argv0 in _INTERPRETER_TOKENS:
            segment = tokens[pos + 1 : end]
            if argv0 in _DASH_C_SHELLS and "-c" in segment:
                # Opaque payload — check_claude_spawn step 3's recursion
                # (over the ORIGINAL unstripped string) is authoritative.
                continue
            for operand in segment:
                if operand.startswith("-"):
                    continue
                match = _contains_forbidden_fragment(operand)
                if match is not None:
                    return match
    return None


def _substitution_is_claude_spawn(inner: str) -> bool:
    """Return True if *inner* — the text captured inside a `$(...)` or
    `` `...` `` command substitution — actually EXECUTES claude, rather than
    merely mentioning the string somewhere (D#2099).

    Applies the same tokenise-then-_command_positions technique used for the
    forbidden-fragment check (D#2058, extended by PR #2095): find where a new
    command starts inside the substitution (skipping env prefixes and
    wrapper tokens like `timeout`/`nohup`, exactly as the outer command is
    walked), and test THOSE positions with `_is_claude_token`. A path
    argument like `$HOME/.claude/projects/...` is never a command position,
    so it can no longer trip this rule.
    """
    normalised = _QUOTE_STRIP_RE.sub("", inner)
    normalised = re.sub(r"&&", " && ", normalised)
    normalised = re.sub(r"\|\|", " || ", normalised)
    normalised = re.sub(r";", " ; ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()
    try:
        inner_tokens = shlex.split(normalised)
    except ValueError:
        inner_tokens = normalised.split()
    for pos in _command_positions(inner_tokens):
        if _is_claude_token(inner_tokens[pos]):
            return True
    return False


def check_claude_spawn(argv: list[str], cmd_str: str) -> Decision:
    """Return Decision(allow=False, ...) if *cmd_str* attempts to spawn a claude
    process or invoke a forbidden loop-trigger path.

    Strategy:
    1. Check for forbidden path fragments, but only where they are actually
       EXECUTED — argv[0] of a pipeline stage, or a non-flag operand of an
       interpreter/sourcing form (D#2058) — not anywhere in the raw string.
    2. Tokenise and check if `claude` appears as the *executable* token in any
       pipeline stage — this avoids false positives for `grep claude CLAUDE.md`.
    3. Recurse into bash -c / sh -c payloads to catch wrapping obfuscation.

    Parameters
    ----------
    argv:
        Token list from shlex.split(cmd_str), or [] if tokenisation failed.
        Not used directly but kept in signature for future callers that
        pre-tokenise.
    cmd_str:
        Raw shell command string as received from the Bash tool call.
    """
    # Tokenise up front, applying quote-strip normalisation to the raw string
    # first so that cl"au"de -p "..." becomes a single token "claude". Also
    # ensure shell metacharacters are space-surrounded so shlex.split
    # produces them as separate tokens (e.g. "hi;" → "hi ;"). Shared by both
    # the fragment check (1) and the claude-token check (2) below.
    normalised = _QUOTE_STRIP_RE.sub("", cmd_str)
    # Ensure shell separators are space-surrounded so shlex.split produces
    # them as discrete tokens (handles `echo hi;claude` → `echo hi ; claude`).
    normalised = re.sub(r"&&", " && ", normalised)
    normalised = re.sub(r"\|\|", " || ", normalised)
    normalised = re.sub(r";", " ; ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()
    try:
        tokens = shlex.split(normalised)
    except ValueError:
        # Untokenisable (e.g. unclosed quotes after stripping) — fall back to
        # simpler split; prefer safety over false positive.
        tokens = normalised.split()

    # 1. Forbidden path fragments — position-aware, not a raw substring test.
    matched_fragment = _check_forbidden_fragment_positions(tokens)
    if matched_fragment is not None:
        return Decision(
            allow=False,
            reason=f"claude_spawn_forbidden: matched pattern '{matched_fragment}'",
        )

    # 2. Check each command position for a claude executable token
    for pos in _command_positions(tokens):
        tok = tokens[pos]
        if _is_claude_token(tok):
            return Decision(
                allow=False,
                reason="claude_spawn_forbidden: matched pattern 'claude'",
            )
        # `exec claude …` — exec is the command, claude is the argument
        if tok == "exec" and pos + 1 < len(tokens) and _is_claude_token(tokens[pos + 1]):
            return Decision(
                allow=False,
                reason="claude_spawn_forbidden: matched pattern 'exec claude'",
            )

    # 3. Recurse into bash -c / sh -c payloads (original tokens, before quote-strip,
    #    to preserve inner quoting for recursive shlex.split).
    try:
        orig_tokens = shlex.split(cmd_str)
    except ValueError:
        orig_tokens = []

    for i, tok in enumerate(orig_tokens):
        if tok in ("bash", "sh") and i + 1 < len(orig_tokens) and orig_tokens[i + 1] == "-c":
            if i + 2 < len(orig_tokens):
                inner = orig_tokens[i + 2]
                inner_result = check_claude_spawn([], inner)
                if not inner_result.allow:
                    return inner_result

    # 4. `command claude ...` -- bash builtin bypasses alias/function interception
    for pos in _command_positions(tokens):
        tok = tokens[pos]
        if tok == "command" and pos + 1 < len(tokens) and _is_claude_token(tokens[pos + 1]):
            return Decision(
                allow=False,
                reason="claude_spawn_forbidden: matched pattern 'command claude'",
            )

    # 5. Dollar-paren command substitution: $(claude -p ...) or $(...claude...)
    #    Match raw cmd_str before any normalisation so $(...) survives. The
    #    regex only finds CANDIDATES (substitutions mentioning "claude"
    #    anywhere); _substitution_is_claude_spawn decides whether claude is
    #    actually in command position inside that substitution (D#2099) —
    #    a path argument like $(cat /tmp/.../claude-1000/x) no longer denies.
    for match in _DOLLAR_PAREN_CLAUDE_RE.finditer(cmd_str):
        inner = match.group(0)[2:-1]  # strip leading "$(" and trailing ")"
        if _substitution_is_claude_spawn(inner):
            return Decision(
                allow=False,
                reason="claude_spawn_forbidden: matched pattern '$(claude ...)'",
            )

    # 6. Backtick command substitution: `claude -p ...` (same D#2099 narrowing).
    for match in _BACKTICK_CLAUDE_RE.finditer(cmd_str):
        inner = match.group(0)[1:-1]  # strip leading/trailing backtick
        if _substitution_is_claude_spawn(inner):
            return Decision(
                allow=False,
                reason="claude_spawn_forbidden: matched pattern '`claude ...`'",
            )

    # 7. Foreign-language exec: python3 -c / python -c payloads that call claude via
    #    os.execvp, os.execv, subprocess.run, or subprocess.Popen with "claude" in args.
    for i, tok in enumerate(orig_tokens):
        if tok in ("python3", "python") and i + 1 < len(orig_tokens) and orig_tokens[i + 1] == "-c":
            if i + 2 < len(orig_tokens):
                payload = orig_tokens[i + 2]
                if _PYTHON_EXEC_CLAUDE_RE.search(payload):
                    return Decision(
                        allow=False,
                        reason="claude_spawn_forbidden: matched pattern 'python -c os.exec*/subprocess claude'",
                    )

    return Decision(allow=True, reason="")


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    allow: bool
    reason: str  # one of the canonical reason strings, or empty for allow


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def classify_cwd(path: str) -> str:
    """Classify an agent's working directory into one of three tiers.

    Returns:
      "team_lead"  — path is under MAIN_REPO_ROOT and NOT under any worktree prefix.
                     Only this tier may initiate spawns, gh-api mutations, and registry writes.
      "worktree"   — path is under a known worktree prefix (executor context).
      "untrusted"  — path is anywhere else (e.g. /tmp/random, /home/other).
                     All mutations are blocked.

    Trailing slashes and embedded `..` components are normalised via Path.resolve()
    before classification so that paths like
      <main-repo-root>/  (trailing slash)
      <main-repo-root>/.claude/worktrees/abc/../abc  (dot-dot)
    are handled correctly.
    """
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = path

    # Check worktree first (more specific than team_lead)
    for prefix in _WORKTREE_PREFIXES:
        if resolved.startswith(prefix):
            return "worktree"

    # Check team_lead: must be under MAIN_REPO_ROOT (and not a worktree, already excluded)
    main_root = str(MAIN_REPO_ROOT.resolve())
    if resolved == main_root or resolved.startswith(main_root + "/"):
        # The derived root is only trustworthy when the HEAD-file check behind
        # it passed (see hooks/repo_root.py) — that check does not prove the
        # evidence is genuine. An unconfident derivation inside a worktree
        # collapses candidate to the worktree's own path,
        # which would otherwise match here and promote it to team_lead — tier
        # down instead of granting the most permissive tier on ambiguous
        # evidence.
        if not is_main_repo_root_confident():
            return "untrusted"
        return "team_lead"

    return "untrusted"


def is_foreign_self_governed(path: str) -> bool:
    """Return True if *path* belongs to a *different* self-governed autonomous team.

    The af sandbox hook is registered globally (``~/.claude/settings.json``), so it
    fires inside sibling team sessions too — e.g. the ``learningappforkids`` team that
    runs from ``<home>/lafk-demo`` with its own state dir and its own
    ``/start-the-day``. Those teams govern themselves: af must DEFER to their own
    session rather than impose its worktree/team-lead tiering, which would otherwise
    classify them ``"untrusted"`` (see classify_cwd) and block every spawn and mutation.

    A directory is "foreign self-governed" when it is a git working tree (or under one)
    that is OUTSIDE ``MAIN_REPO_ROOT`` and carries its own ``.autonomous-team/`` marker
    directory at the git root. The ``.autonomous-team/`` marker is what distinguishes a
    peer autonomous team from an arbitrary scratch dir (which stays ``"untrusted"``).

    Pure filesystem walk — no subprocess (keeps this module subprocess-free).
    """
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False

    # Env-independent floor: a path under the real, filesystem-derived root is
    # never foreign, regardless of what SANDBOX_MAIN_REPO_ROOT currently says.
    derived_root = _DERIVED_MAIN_REPO_ROOT.resolve()
    if resolved == derived_root or str(resolved).startswith(str(derived_root) + os.sep):
        return False

    main_root = MAIN_REPO_ROOT.resolve()
    # Our own repo (and its worktrees, which live under it) is never foreign.
    if resolved == main_root or str(resolved).startswith(str(main_root) + os.sep):
        return False

    # Walk cwd → filesystem root looking for a git working tree that owns an
    # .autonomous-team/ governance marker.
    for ancestor in (resolved, *resolved.parents):
        if ancestor == main_root:
            return False
        if (ancestor / ".git").exists() and (ancestor / ".autonomous-team").is_dir():
            return True
    return False


def is_team_lead(path: str) -> bool:
    """Return True if *path* classifies as the Team Lead context."""
    return classify_cwd(path) == "team_lead"


def is_worktree(cwd: str) -> Optional[str]:
    """Return the worktree id if *cwd* is inside a known worktree root.

    Resolves symlinks before checking.  Returns None for Team Lead / main repo.

    Delegates to classify_cwd() for the tier check; returns the worktree ID
    string (for backwards-compatibility with callers that use the truthy value).
    """
    try:
        resolved = str(Path(cwd).resolve())
    except Exception:
        resolved = cwd

    if classify_cwd(cwd) != "worktree":
        return None

    for prefix in _WORKTREE_PREFIXES:
        if resolved.startswith(prefix):
            # Extract the id: the path component immediately after the prefix dir separator.
            rest = resolved[len(prefix):]
            worktree_id = rest.split("/")[0] if rest else "unknown"
            return worktree_id or "unknown"
    return None


def _tokenize_shell_command(command: str) -> list[str]:
    """Tokenise *command* into shell-aware tokens — the ONE segmentation layer
    every git-parsing function in this module consumes (D#1746/D#1748).

    Built on `_tokenize_punctuation_aware` (shlex `punctuation_chars` mode,
    already used by the `~`/`$HOME` redirect scan below), which natively:
      - splits `;`, `&&`, `||`, `|`, `&`, `(`, `)` into their own tokens even
        when glued to an adjacent word with no surrounding whitespace, and
      - keeps COMPOUND redirect operators grouped as their own token(s)
        instead of shredding them into single characters. This is what fixes
        D#1748 (F7): the old approach padded `&` with spaces via `re.sub`
        BEFORE tokenising, which turned `2>&1` into standalone `2>`, `&`, `1`
        tokens — and `&` is a shell separator, so the walker's `in_git` state
        reset mid-invocation and the real verb one token later was never
        seen. `shlex`'s punctuation tokeniser instead yields `2`, `>&`, `1`
        for that same input: none of those three is a shell separator, and
        none matches `_GIT_VERB_SHAPE_RE` (a bare word starting with a
        letter), so the walker skips all three without resetting state and
        still finds the real verb right after them.
    Backtick command substitution is padded to its own token FIRST — shlex's
    punctuation_chars set is `();<>|&`, which does not include backtick, so
    `` `git ...` `` would otherwise stay glued to `git` as a single token.
    Quoted content is unaffected by any of this: shlex still folds a quoted
    region into one token regardless of what shell metacharacters live
    inside it, so `git commit -m "fix;bug"` still tokenises `"fix;bug"` as
    one token, not a separator.

    Never raises: falls back to a punctuation-padded, plain whitespace split
    if the punctuation tokeniser itself can't handle the input (unbalanced
    quoting) — degraded coverage on that edge, not an exception that would
    otherwise let an unparseable command fall through to a caller with no
    tokens to inspect at all.
    """
    padded = re.sub(r"`", " ` ", command)
    try:
        return _tokenize_punctuation_aware(padded)
    except ValueError:
        normalised = re.sub(r"[;&|()]", lambda m: f" {m.group()} ", padded)
        return normalised.split()


def _walk_git_invocations(
    tokens: list[str], base_cwd: str
) -> tuple[list[tuple[str, str, list[str]]], str]:
    """Walk *tokens* once, returning every git invocation's (verb, effective_cwd,
    args) triple plus the command's final cwd after all `cd`s (D#1746, D#2058).

    This is the second half of the shared layer: `_tokenize_shell_command`
    turns the raw string into tokens, this function is the ONLY place that
    walks them looking for `cd`s and git invocations. `resolve_effective_cwd`
    (leading-result callers) and `classify_bash` (needs every invocation,
    each paired with ITS OWN cwd) both call this directly; neither keeps a
    private token walk.

    Per-invocation pairing (Spec property 4) is the point of returning a
    list instead of one CWD for the whole command: `git -C <main-repo> log
    && git commit -m wip` must NOT block — the first invocation's cwd
    escapes the worktree but its verb (`log`) is read-only, the second
    invocation's verb (`commit`) is a write but its own cwd never left the
    worktree, and no single invocation is BOTH escaping-cwd and a write verb.
    A single whole-command cwd (what this replaces) got this wrong by
    accident: the pre-fix `resolve_effective_cwd` stopped at the FIRST git
    invocation it saw and returned that invocation's cwd unconditionally,
    so `git -C <elsewhere> log && git commit -m wip` reported `<elsewhere>`
    as "the" effective cwd even though the actual write (`commit`) never
    left the worktree — an over-block, not the escape this fix targets.

    `cd` state accumulates across the WHOLE command in token order — a git
    invocation's own `-C`/`--git-dir`/`--work-tree` only overrides the cwd
    for THAT invocation locally (it does not `cd` the shell), so it must not
    reset the running `effective` cwd that later `cd`s and invocations build
    on. A shell separator ends the CURRENT invocation's verb search (mirrors
    `_GIT_VERB_WALK_SEPARATORS`'s pre-existing role) but never rewinds `cd`
    state.

    Returns (invocations, final_cwd). `final_cwd` is the cwd after every
    `cd` in the command has been applied, independent of any git invocation
    — this is what `resolve_effective_cwd` falls back to when the command
    contains no git invocation at all (unchanged from the pre-fix behaviour
    for that case).

    D#2058: each invocation's `args` — every token between the verb and the
    next separator/redirect — lets `classify_bash` tell `git branch --list`
    (read-only) apart from `git branch -D foo` (write) without a second
    token walk. A read-only-spelling escape needs the verb's OWN flags, not
    just its name, and _GIT_ALWAYS_BLOCKED_VERBS ignores flags entirely.
    """
    effective = Path(base_cwd)
    invocations: list[tuple[str, str, list[str]]] = []
    in_git = False
    invocation_cwd: Optional[Path] = None
    skip_next = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if skip_next:
            skip_next = False
            i += 1
            continue
        if tok in _GIT_VERB_WALK_SEPARATORS:
            in_git = False
            invocation_cwd = None
            i += 1
            continue
        if not in_git:
            if tok == "cd":
                if i + 1 < n and not tokens[i + 1].startswith("-"):
                    # D#1898: `cd ~` / `cd $HOME` / `cd ${HOME}` must resolve to
                    # the real home dir, not a literal relative segment "~".
                    target = _expand_home_prefix(tokens[i + 1])
                    try:
                        candidate = Path(target)
                        effective = candidate if candidate.is_absolute() else effective / candidate
                    except Exception:
                        pass
                    i += 2
                    continue
                i += 1
                continue
            if _is_git_token(tok):
                in_git = True
                invocation_cwd = effective
                i += 1
                continue
            i += 1
            continue
        # in_git: looking for THIS invocation's subcommand (verb), tracking
        # any path-relocating global option along the way. Handles both
        # `--opt value` (two tokens) and the glued `--opt=value` form (one
        # token, split via _split_glued_git_option) uniformly — see that
        # function's docstring for why the glued form needs its own split
        # rather than an exact-match miss.
        opt_name, glued_value = _split_glued_git_option(tok)
        if opt_name in _GIT_CWD_TAKING_OPTS:
            target = glued_value if glued_value is not None else (
                tokens[i + 1] if i + 1 < n else None
            )
            if target is not None:
                try:
                    candidate = Path(target)
                    base = invocation_cwd if invocation_cwd is not None else effective
                    invocation_cwd = candidate if candidate.is_absolute() else base / candidate
                except Exception:
                    pass
            i += 1 if glued_value is not None else 2
            continue
        if opt_name in _GIT_VALUE_TAKING_GLOBAL_OPTS:
            # Value-taking but not cwd-relocating (-c, --namespace, ...) — skip
            # the option's value so it can't be misread as the verb (F4). The
            # glued form has no separate value token to skip.
            i += 1 if glued_value is not None else 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if not _GIT_VERB_SHAPE_RE.match(tok):
            # Not a real verb shape (a stray redirect fragment, a bare `\n`
            # from backslash-newline continuation, other punctuation) — skip
            # it but stay "in git" so the real verb further along is still
            # found (D#1729 F5, D#1748 F7).
            i += 1
            continue
        cwd_for_invocation = invocation_cwd if invocation_cwd is not None else effective
        try:
            resolved_cwd = str(cwd_for_invocation.resolve())
        except Exception:
            resolved_cwd = str(cwd_for_invocation)
        # Collect this invocation's own argument tokens — everything up to
        # the next separator or redirect operator — WITHOUT consuming them
        # from the main walk (`i` only advances past the verb itself below).
        # This is a lookahead, not a second pass over the token stream.
        args: list[str] = []
        j = i + 1
        while j < n:
            arg_tok = tokens[j]
            if arg_tok in _GIT_VERB_WALK_SEPARATORS or arg_tok.startswith((">", "<")):
                break
            args.append(arg_tok)
            j += 1
        invocations.append((tok.lower(), resolved_cwd, args))
        in_git = False
        invocation_cwd = None
        i += 1

    try:
        final_cwd = str(effective.resolve())
    except Exception:
        final_cwd = str(effective)
    return invocations, final_cwd


def resolve_effective_cwd(command: str, base_cwd: str) -> str:
    """Return the effective working directory after resolving `cd` prefixes and `git -C`.

    Handles patterns like:
      cd /some/path && git checkout main
      cd /a && cd /b && git status
      git -C /some/path checkout main

    Thin wrapper over `_walk_git_invocations` (D#1746) for callers that only
    want a single leading result: the first git invocation's own cwd if the
    command contains one, else the cwd after every `cd` has been applied.
    `classify_bash` does NOT use this for its own write-verb check — it
    calls `_walk_git_invocations` directly so it can pair EVERY invocation
    with its own cwd instead of collapsing the whole command to one (see
    that function's docstring for why the collapse was itself a bug).
    """
    tokens = _tokenize_shell_command(command)
    invocations, final_cwd = _walk_git_invocations(tokens, base_cwd)
    if invocations:
        return invocations[0][1]
    return final_cwd


def _extract_git_verb(command: str) -> Optional[str]:
    """Return the first git subcommand in *command*, or None if not a git command.

    Thin wrapper over _extract_all_git_verbs() — kept as its own entry point because
    some callers (and tests) only care about the leading verb.  classify_bash() does
    NOT use this function; it uses _extract_all_git_verbs() so a read-only verb can't
    shield a later write/always-blocked verb in the same command (D#1729 F3).
    """
    verbs = _extract_all_git_verbs(command)
    return verbs[0] if verbs else None


def _extract_all_git_verbs(command: str) -> list[str]:
    """Return every git subcommand found in *command*, in the order encountered.

    Thin wrapper over the shared `_tokenize_shell_command` / `_walk_git_invocations`
    layer (D#1746) — the base cwd passed to the walker is irrelevant here (this
    function only wants the verbs, not their cwds) so a placeholder is used.

    Walks the ENTIRE command rather than returning on the first match — a command can
    contain more than one git invocation (`git log;git reset --hard origin/main`), and
    every one of them needs to be visible to the caller so a read-only verb earlier in
    the command can't shield a write/always-blocked verb later in it (D#1729 F3).
    """
    tokens = _tokenize_shell_command(command)
    invocations, _final_cwd = _walk_git_invocations(tokens, base_cwd=".")
    return [verb for verb, _cwd, _args in invocations]


def _git_rm_args_have_cached(tokens: list[str], start: int) -> bool:
    """Return True if a bare `--cached` token appears among the arguments of
    the `git rm` invocation whose tokens begin at *start* (the index right
    after the `rm` subcommand token, or right after the git/rm pair).

    Scans forward from *start* and stops at the next shell separator
    (`_GIT_RM_WALK_SEPARATORS`) or a bare `--` — option parsing ends at `--`,
    so a `--cached` after it names a real pathspec (`git rm -- --cached`),
    not the flag, and a `--cached` in a later pipeline stage
    (`git rm a.py; git commit --cached`) never belongs to this invocation.

    Requires the EXACT token `--cached` — not `--cached=weird` (the flag
    takes no value; git itself rejects that spelling) and not an
    abbreviation like `--ca` (matching prefixes is how a matcher silently
    re-broadens, D#2058-adjacent).
    """
    for tok in tokens[start:]:
        if tok in _GIT_RM_WALK_SEPARATORS or tok == "--":
            break
        if tok == "--cached":
            return True
    return False


def is_real_git_rm_invocation(command: str, *, exempt_cached: bool = False) -> bool:
    """Return True only if *command* contains a real `git rm` invocation.

    Uses a belt-and-suspenders approach: block if EITHER the token-walker fires
    OR the broad adjacent-token scan fires.

    exempt_cached (keyword-only, default False — every existing caller is
    byte-for-byte unaffected):
        When True, a `git rm` invocation whose own arguments include a bare
        `--cached` token is NOT treated as a real git-rm invocation. `git rm
        --cached <path>` only drops the index entry — it cannot remove a
        working-tree file, unlike a destructive `git rm`. Opt-in because one
        consumer (backend/corpus_drift/claims/archive_protocol.py) counts
        `git rm --cached` on a tracked project file as a violation on
        purpose and must keep the current (non-exempt) behaviour.

    Token-walker (step 4):
        `git` appears as an executable at a command position and is followed by
        `rm` as its subcommand (possibly with global git options in between, e.g.
        `-C <path>`, `--no-pager`, `--git-dir=<x>`, `-c k=v`).
        Correctly handles brace groups — `{` and `}` are now in the separator set.

    Broad adjacent-token scan (step 5):
        After shlex tokenisation, any occurrence of a git token (bare `git` or a token
        ending with `/git`) immediately followed by the exact token `rm` is flagged,
        regardless of command position.  This catches `xargs git rm`, `{ git rm f; }`,
        `xargs /usr/bin/git rm`, and any other `... git rm ...` form where the git
        executable is not at the command-position that the walker would detect.
        Quoted strings are safe: `echo "git rm f"` tokenises to [echo, "git rm f"] so
        `git` and `rm` never appear as adjacent separate tokens.

    False-positive guards (both scanners):
      - `git rmsomething` — subcommand token is not exactly `rm`; walker and scan both miss it
      - `echo "git rm f"` — shlex folds the quoted string into one token; scan misses it
      - `printf 'git rm'` — same: quoted arg is one token
      - `gh issue comment --body "do not git rm files"` — the --body value is one token

    Residual gaps (documented non-targets — do not chase):
      - `git-rm` (hyphenated binary) — not a real git command; no such default binary

    Design:
    1. Normalise ALL shell separators (including `;`, `(`, `)`, `{`, `}`) so that
       forms like `echo hi;git rm f`, `(git rm f)`, and `{ git rm f; }` tokenise
       correctly.
    2. Use the BROAD pattern (word-boundary ``git``) as the pre-filter — not
       ``_GIT_RM_PATTERN``.  The narrow pattern misses ``echo hi;git rm f``
       (``;`` before ``git`` fails the lookbehind) and ``git -C p rm f``
       (flags between ``git`` and ``rm`` fail the adjacency requirement).
    3. Tokenise with shlex.split so quoted strings stay as single tokens.
    4. Token-walker: scan for `git` at command positions (after separators),
       walk past global git options, check if subcommand is `rm`.
    5. Broad adjacent scan: walk the token list looking for the exact two-token
       sequence [git, rm] anywhere (independent of command position).
    6. Untokenisable input (unclosed quotes) falls back to `_GIT_RM_PATTERN` —
       conservative (fail-closed toward block).
    """
    # Step 1: normalise separators BEFORE any pattern test.
    # Add spaces around every shell separator so they become discrete shlex tokens.
    # Include ( ) { } so brace groups and subshells tokenise correctly.
    normalised = re.sub(r"&&", " && ", command)
    normalised = re.sub(r"\|\|", " || ", normalised)
    normalised = re.sub(r"[;(){}|]", lambda m: f" {m.group()} ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()

    # Step 2: broad pre-filter on the normalised string.
    # If there is no word-boundary `git` token at all, definitely not a git rm.
    if not _GIT_BROAD_PATTERN.search(normalised):
        return False

    # Step 3: tokenise.
    try:
        tokens = shlex.split(normalised)
    except ValueError:
        # Untokenisable (e.g. unclosed quotes) — fall back to the narrow regex.
        # Conservative: fail-closed (block if the narrow pattern matches).
        return bool(_GIT_RM_PATTERN.search(command))

    # Step 4: token-walker — scan for `git` at any command position, including
    # after `(`, `)`, `{`, `}`.  We do our own scan rather than calling
    # _command_positions() so we can use the extended separator set.
    next_is_cmd = True
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GIT_RM_WALK_SEPARATORS:
            next_is_cmd = True
            i += 1
            continue
        if next_is_cmd:
            # Skip env-var assignments (FOO=bar) and `env` keyword
            if tok == "env" or ("=" in tok and not tok.startswith("-")):
                i += 1
                continue
            next_is_cmd = False
            if not _is_git_token(tok):
                i += 1
                continue
            # Found `git` (or path-prefixed git) at a command position.  Walk forward past global git options
            # to find the real subcommand.
            j = i + 1
            while j < len(tokens):
                sub = tokens[j]
                # End of this pipeline stage
                if sub in _GIT_RM_WALK_SEPARATORS:
                    break
                # Global git options that consume the next token as their argument.
                # Includes -c (git -c key=val <subcommand>) which always has a separate value token.
                if sub in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                    j += 2
                    continue
                # Global git options with embedded argument (--git-dir=<x>, --work-tree=<x>)
                if sub.startswith("--git-dir=") or sub.startswith("--work-tree="):
                    j += 1
                    continue
                # Other global flags (--no-pager, --paginate, --version, etc.)
                if sub.startswith("-"):
                    j += 1
                    continue
                # First non-flag token is the git subcommand
                if sub.lower() == "rm":
                    if exempt_cached and _git_rm_args_have_cached(tokens, j + 1):
                        break  # index-only untrack — not a real git-rm here
                    return True
                break  # subcommand is something other than rm
        i += 1

    # Step 5: broad adjacent-token scan — catch `xargs git rm`, `{ git rm f; }`,
    # and any other form where `git` is not at command position in the walker's view.
    # Operates on the same shlex token list so quoted strings remain as single tokens
    # (the false-positive guard: `echo "git rm f"` → tokens = ["echo", "git rm f"]).
    # Requires the exact token "rm" (not a prefix) so `git rmsomething` is safe.
    for idx in range(len(tokens) - 1):
        if _is_git_token(tokens[idx]) and tokens[idx + 1].lower() == "rm":
            if exempt_cached and _git_rm_args_have_cached(tokens, idx + 2):
                continue  # index-only untrack — keep scanning the rest
            return True

    return False


def _is_bash_wrapping_git_write(command: str, worktree_root: str) -> Optional[str]:
    """Detect `bash -c '...'` or `sh -c '...'` wrapping a git write-verb."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    for i, tok in enumerate(tokens):
        if tok in ("bash", "sh") and i + 1 < len(tokens) and tokens[i + 1] == "-c":
            if i + 2 < len(tokens):
                inner = tokens[i + 2]
                # Recursively classify the inner command
                inner_decision = classify_bash(inner, worktree_root)
                if not inner_decision.allow:
                    return inner_decision.reason
    return None


def _is_kernel_device(path: str) -> bool:
    """Return True if *path* is a kernel virtual device (e.g. /dev/null).

    Kernel devices are never real file writes and must never be blocked,
    regardless of worktree context.
    """
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _KERNEL_DEVICE_PREFIXES)


# Heredoc start marker: `<<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`.
# Group 1 is `-` (or None) for the `<<-`/tab-stripping form; group 3 is the
# delimiter word. Used only by _strip_heredoc_bodies() below.
_HEREDOC_START_RE = re.compile(r"<<(-)?\s*(['\"]?)(\w+)\2")


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc body lines from *command* (D#1898 round 2, B1).

    A heredoc body is free-form text the shell passes through literally to
    the receiving command's stdin — it is never itself parsed as shell
    syntax, so a `>`/`~`/`$HOME` mention inside one (e.g. a doc explaining
    the bug this PR fixes, or a test file's own source containing an
    example command as a string literal) must not be scanned as a real
    write target. Line-oriented and deliberately simple — a best-effort
    filter for the common case (one heredoc per line, delimiter alone on
    its own line), not a full shell parser: multiple heredocs chained on
    one line (`cmd <<A <<B`) are not specially handled, and an ill-formed
    heredoc with no matching terminator just consumes the rest of the
    command, which is the same "when in doubt, see less" direction this
    module already takes elsewhere (see _classify_unenumerated_write for the
    reasoning why the rest of this module doesn't try to be a real parser
    either).

    Used ONLY ahead of the tokenised `~`/`$HOME` scans below
    (_home_prefixed_redirect_targets, _cd_escape_relative_write) — the
    untouched `/`-prefixed raw regex (_REDIRECT_PATTERN) and the rest of
    classify_bash still see the original, unstripped command string.
    """
    lines = command.split("\n")
    out_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out_lines.append(line)
        match = _HEREDOC_START_RE.search(line)
        i += 1
        if match is None:
            continue
        strip_leading_tabs = match.group(1) == "-"
        delimiter = match.group(3)
        while i < n:
            body_line = lines[i]
            i += 1
            check = body_line.lstrip("\t") if strip_leading_tabs else body_line
            if check == delimiter:
                break
            # Body line dropped — its content is never scanned.
    return "\n".join(out_lines)


def _tokenize_punctuation_aware(command: str) -> list[str]:
    """Tokenise *command* the way `shlex.split` does, but with shell
    redirect/control operators split into their own tokens even when they
    have no surrounding whitespace (D#1898 round 3).

    `shlex.split(cmd)` treats `>` as an ordinary word character, so
    `echo x >~/.bashrc` tokenises as `['echo', 'x', '>~/.bashrc']` — a
    single token, not a `>` operator followed by a target. Both
    `_home_prefixed_redirect_targets` and `_cd_escape_relative_write` scan
    for a literal `>`/`>>` token immediately followed by a target token, so
    that no-space spelling silently fell through both checks: `echo x
    >~/.bashrc` and `echo x >>$HOME/.bashrc` allowed, and `cd ~ && echo x
    >>.bashrc` allowed too since `>>.bashrc` never matched `tok in (">",
    ">>")` either. All three are one keystroke away from the round-1 repro
    commands (same escape, no space) and round-2 security confirmed all
    three write past the sandbox against real bash.

    `shlex.shlex(..., punctuation_chars=True)` fixes this: it splits on the
    shell operator characters `();<>|&` even mid-token, while leaving `~`
    alone (not a punctuation char), so `~/.bashrc` and `$HOME/.bashrc`
    still come back as single word tokens — `>~/.bashrc` becomes `['>',
    '~/.bashrc']`, `>>.bashrc` becomes `['>>', '.bashrc']`. `whitespace_split
    = True` is required alongside `punctuation_chars` per the stdlib docs so
    ordinary whitespace-delimited words aren't shredded into individual
    wordchars. `commenters = ''` matches what `shlex.split()` itself does by
    default (comments=False) — without it a bare `#` earlier in the command
    would truncate tokenisation, which `shlex.split` never did here.

    Same failure mode as `shlex.split`: raises `ValueError` on unbalanced
    quoting. Callers already handle that.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _home_prefixed_redirect_targets(command: str) -> list[str]:
    """Return `~`/`$HOME`/`${HOME}`-prefixed and quoted `/`-prefixed redirect
    targets, expanded to absolute paths (D#1898 round 2, B1; widened D#1792).

    Deliberately a SEPARATE, TOKENISED pass rather than a widened
    _REDIRECT_PATTERN alternation — see the comment above _REDIRECT_PATTERN
    for why raw-string matching over-blocks here (quoted mentions, `gh`
    comment bodies, etc.). Tokenising means a `>` living inside a quoted
    argument is part of that argument's single token, not a standalone
    operator token, so it's never mistaken for a real redirect. Heredoc
    bodies are stripped first (_strip_heredoc_bodies) so an unquoted mention
    inside one doesn't tokenise as a bare `>` either.

    Uses `_tokenize_punctuation_aware` (D#1898 round 3), not bare
    `shlex.split`, so a no-space redirect like `>~/.bashrc` still yields a
    `>` operator token followed by a `~/.bashrc` target token — see that
    function's docstring for why plain `shlex.split` missed this.

    D#1792: the candidate filter used to accept only `~`/`$HOME`/`${HOME}`
    prefixes, so a QUOTED `/`-prefixed target (`echo x > "<main-repo>/CLAUDE.md"`)
    fell through to the raw `_REDIRECT_PATTERN` regex above, which is
    quote-blind by design and can't see past the opening `"` — that regex
    requires `/` to immediately follow the `>` (mod whitespace), and a quote
    character breaks the match. This tokeniser already unquotes the target
    into a single clean token (that's the whole reason this pass exists), so
    accepting a bare `/`-prefixed candidate here too costs nothing new and
    closes that gap. An unquoted `/`-prefixed target is still also caught by
    the raw regex above — the two can overlap on the same target and that's
    fine, `_absolute_path_targets` de-dupes nothing but a duplicate entry is
    harmless (same target, same verdict).

    Falls back to returning no candidates (not command.split()) when
    tokenising can't parse the command — conservative in the safe
    direction: an untokenisable command just doesn't get this coverage here,
    it isn't treated as a false write.
    """
    try:
        tokens = _tokenize_punctuation_aware(_strip_heredoc_bodies(command))
    except ValueError:
        return []
    targets: list[str] = []
    for i, tok in enumerate(tokens):
        if tok in (">", ">>") and i + 1 < len(tokens):
            candidate = tokens[i + 1]
            if (
                candidate.startswith("~")
                or candidate.startswith("$HOME")
                or candidate.startswith("${HOME}")
                or candidate.startswith("/")
            ):
                expanded = _expand_home_prefix(candidate)
                if os.path.isabs(expanded) and not _is_kernel_device(expanded):
                    targets.append(expanded)
    return targets


def _is_ephemeral_tmp_path(path: str) -> bool:
    """True if *path* is under /tmp or /var/tmp — ephemeral filesystem, not
    repo state, always safe regardless of which write mechanism reaches it
    (redirect, cd-then-relative-write, or an enumerated write-command).

    Single source of truth for this exemption (D#1898 round 2, B2) so the
    absolute-target loop in classify_bash and _cd_escape_relative_write
    can't drift apart on what counts as "the scratchpad".

    Normalises *path* first (os.path.normpath — purely lexical, no
    filesystem access) so a target spelled with a parent-dir segment, e.g.
    "/tmp/../etc/passwd", can't satisfy a raw prefix test and slip through
    as if it were really under /tmp (D#1992).
    """
    normalised = os.path.normpath(path)
    return (
        normalised == "/tmp"
        or normalised == "/var/tmp"
        or normalised.startswith(("/tmp/", "/var/tmp/"))
    )


def _absolute_path_targets(command: str) -> list[str]:
    """Return absolute paths that a command writes to.

    Looks at:
      - Output redirects: `> /abs/path`, `>> /abs/path`, and `~`/`$HOME`-
        prefixed equivalents (via the tokenised _home_prefixed_redirect_targets)
      - Write commands: `tee /abs/path`, `cp src /abs/path`, `mv src /abs/path`

    Kernel device paths (/dev/null, /dev/stdout, etc.) are excluded — they are
    never real writes and must not be treated as worktree-boundary violations.

    D#1898: `~`/`$HOME`/`${HOME}`-prefixed write-command arguments are run
    through _expand_home_prefix() before the is_absolute() check, so `~/x`,
    `$HOME/x`, and `${HOME}/x` are recognised as the absolute paths they are,
    the same as a literal `/home/.../x`. Redirect targets get the same
    treatment via the separate tokenised scan below (see its docstring for
    why redirects need a different code path than write-command arguments).
    """
    paths: list[str] = []

    # Output redirects — `/`-prefixed via the raw regex (untouched, see its
    # comment), `~`/`$HOME`-prefixed via the tokenised scan.
    for match in _REDIRECT_PATTERN.finditer(command):
        candidate = match.group(1)
        if os.path.isabs(candidate) and not _is_kernel_device(candidate):
            paths.append(candidate)
    paths.extend(_home_prefixed_redirect_targets(command))

    # Write-command path targets
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("&&", "||", ";", "|"):
            i += 1
            continue
        cmd_name = os.path.basename(tok)
        if cmd_name in _PATH_WRITE_COMMANDS:
            # Last argument that looks absolute is the destination
            remaining = tokens[i + 1 :]
            for arg in reversed(remaining):
                if arg.startswith("-"):
                    continue
                candidate = _expand_home_prefix(arg)
                if os.path.isabs(candidate):
                    paths.append(candidate)
                break
        i += 1

    return paths


def _cd_escape_relative_write(command: str, cwd: str, worktree_root: str) -> Optional[str]:
    """Detect a relative write landing outside the worktree because a `cd`
    earlier in the same command moved the shell there (D#1898), e.g.
    `cd ~ && echo x >> .bashrc`.

    This is a distinct blind spot from _absolute_path_targets: the write's
    own path string (`.bashrc`) carries no `/`, `~`, or `$HOME` marker at
    all — it only escapes because of the directory `cd` left the shell in,
    which _absolute_path_targets never looks at.

    Order-aware (D#1898 round 2, should-fix from the security review of PR
    #1901): walks the command's tokens in sequence, tracking the effective
    CWD the same way resolve_effective_cwd does, and only flags a relative
    redirect whose OWN position in that walk has the effective CWD outside
    the worktree at that point. The original version called
    resolve_effective_cwd() once for the whole command and used that single
    final CWD to judge every redirect in it, which blocked commands like
    `echo hi > out.txt && cd ~` even though the write happens before the
    `cd` and lands squarely inside the worktree.

    Also exempts /tmp and /var/tmp via the same _is_ephemeral_tmp_path()
    check the absolute-target loop in classify_bash uses (B2 from the same
    review): `cd <scratchpad> && echo hi > notes.txt` is now allowed the
    same way `echo hi > /tmp/scratch.txt` already was — same operation, same
    answer, instead of two different ones depending on spelling.

    Fires only when a redirect's own target is relative (an absolute target
    is caught by the loop in classify_bash on its own merits, home-prefixed
    or not) and the command contains a `cd` at all — ordinary relative
    writes with no `cd` in the command never reach the CWD-outside-worktree
    branch below, since the walk's starting `effective` is `cwd` itself.

    Heredoc bodies are stripped before tokenising (_strip_heredoc_bodies,
    B1) so a `cd`/`>` mentioned in a heredoc's literal text isn't mistaken
    for a real command in the walk below.

    Uses `_tokenize_punctuation_aware` (D#1898 round 3), not bare
    `shlex.split`, so a no-space redirect like `>>.bashrc` still yields a
    `>>` operator token followed by a `.bashrc` target token instead of one
    fused `>>.bashrc` token that never matches `tok in (">", ">>")` below —
    see that function's docstring for the full explanation.
    """
    if "cd" not in command:
        return None
    try:
        tokens = _tokenize_punctuation_aware(_strip_heredoc_bodies(command))
    except ValueError:
        return None
    try:
        worktree_resolved = Path(worktree_root).resolve()
    except Exception:
        return None

    effective = Path(cwd)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("&&", "||", ";", "|"):
            i += 1
            continue
        if tok == "cd":
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                target = _expand_home_prefix(tokens[i + 1])
                try:
                    candidate = Path(target)
                    effective = candidate if candidate.is_absolute() else effective / candidate
                except Exception:
                    pass
                i += 2
                continue
        if tok in (">", ">>") and i + 1 < len(tokens):
            target = _expand_home_prefix(tokens[i + 1])
            i += 2
            if os.path.isabs(target):
                continue
            try:
                effective_resolved = effective.resolve()
            except Exception:
                effective_resolved = effective
            if _is_ephemeral_tmp_path(str(effective_resolved)):
                continue
            if not effective_resolved.is_relative_to(worktree_resolved):
                return "output redirect outside worktree (cd left the worktree)"
            continue
        i += 1
    return None


# ---------------------------------------------------------------------------
# Unenumerated-write detection (D#1749) — deny-by-default path scan
# ---------------------------------------------------------------------------
#
# _absolute_path_targets (above) is enumeration-based: it only recognises
# tee/cp/mv/install/rsync and shell redirects.  Any other program doing its own
# file I/O — `sed -i`, `python3 -c "...write_text(...)"`, or a completely unknown
# binary — was invisible to it and could write anywhere on the filesystem the
# sandbox is supposed to confine it away from, including the audit.jsonl file the
# sandbox's own tamper-evidence log depends on (see D#1749).
#
# The fix below is an INVERSION, not an extension of that enumeration: instead of
# trying to keep growing an unbounded list of writer program names — which can
# never be complete against arbitrary programs — detection flips to deny-by-default.
# A small, reviewable allowlist of READ-ONLY commands (_READONLY_COMMAND_NAMES) is
# the only enumeration this detector permits. Any command whose name is not on that
# allowlist is treated as "could write anywhere" and every absolute-path-shaped
# substring in its arguments — including inside quoted arguments, e.g. the code
# string of `python3 -c "...open('/abs/path','w')..."` — is run through the same
# worktree/tmp/dial-registry checks the enumerated writers above already use.
#
# Two narrow, deliberate exceptions exist purely to avoid false-positive blocking of
# ordinary reads (the Spec's "over-blocking is a hard fail" requirement) — they are
# NOT a reintroduction of writer-enumeration:
#   * sed/awk are only treated as "writing" when their own in-place-edit flag
#     (-i/--in-place, or GNU awk's `-i inplace`) is present. Without it neither can
#     write a file no matter what path it's given, so scanning them would only
#     produce false positives with zero security benefit.
#   * `python3 -c "<code>"` / `python -c "<code>"` payloads are exempted from the
#     path scan ONLY when the payload is narrowly shaped as a pure read (see
#     _python_payload_is_read_only below, which walks the payload's AST). This is
#     an allowlist of proven-safe shapes, not a denylist of write call names —
#     round 1 of this fix enumerated write-call names (_PY_WRITE_SIGNAL_RE) and
#     round-1 security review live-verified that os.system(...), shutil.rmtree(...),
#     subprocess.run(['tee', ...]), os.open(O_CREAT|O_WRONLY), os.truncate(...), and
#     os.symlink(...) all slipped straight through it — the same enumeration
#     mistake this Discussion exists to reject, one level deeper. The allowlist
#     exists because tests/test_dial_sandbox_integration.py::
#     test_allows_reading_dial_registry requires a plain `python3 -c
#     "...open(path).read()..."` read of a dial-protected path to keep working, and
#     under pure "any absolute path in a non-allowlisted command blocks" logic it
#     would not.
#
# KNOWN, ACKNOWLEDGED LIMITATION (Spec F19 — do not claim this is fixed):
# `python3 -c` is the only program whose payload this module can read as text.
# `python3 scripts/seed.py` — i.e. python running a SCRIPT FILE, not a `-c` payload
# — carries no path token in the command string at all; the write target lives
# inside seed.py, invisible to any static command-string inspection. That repro
# from the Discussion is NOT closed by this change and is not closeable by this
# class of fix — it would require executing or statically analysing the script's
# own source, which classify_bash intentionally never does (no subprocess, no
# filesystem I/O, pure string/token work only — see module docstring constraints).
# The same blind spot applies to any other interpreter/program invoked without an
# inline payload we can inspect (e.g. `node script.js`, `./compiled-binary`) when
# the string arguments given to it don't themselves reference the write path.
#
# A second, distinct blind spot in the same family: a shell-variable or
# tilde-expanded path (`$HOME/x`, `${HOME}/x`, `~/x`) never becomes a path
# candidate here, because classify_bash inspects the literal command STRING and
# never invokes a shell to expand it (no subprocess, by design). The shell
# expands these at actual exec time, after this check has already run, so
# `sed -i s/a/b/ $HOME/...` and `sed -i s/a/b/ ${HOME}/...` are not detected —
# confirmed live during round-2 security review of this PR. A bare `~/...`
# is caught ONLY when the path segment right after `~/` starts with a word
# character (e.g. `~/fulcrumaxe/CLAUDE.md`, where the regex matches the
# `/fulcrumaxe/CLAUDE.md` remainder and it doesn't lexically match the
# worktree) — a segment starting with `.` (e.g. `~/.cache/x`, `~/.ssh/x`) is
# NOT caught, since `_ABS_PATH_TOKEN_RE` requires `/` to be followed by a word
# character. Both forms were confirmed live during round-2 review. This is not
# a real defense against `~`-expansion in any form and should not be relied on.
#
# D#1898 fixed the `~`/`$HOME`/`${HOME}` gap for redirects and the enumerated
# writers (tee/cp/mv/install/rsync) in step 4 above, plus the `cd ~ && <relative
# write>` spelling — see _expand_home_prefix() and _cd_escape_relative_write().
# It deliberately did NOT extend that fix into THIS scan (_classify_unenumerated_
# write / _ABS_PATH_TOKEN_RE below): doing so would mean widening an already
# carefully-reasoned, previously-reviewed regex-and-AST scan, which is exactly
# the kind of completeness-chasing the owner asked this fix not to pursue, and
# none of D#1898's four required repro commands touch this scan (they're all
# plain redirects, caught in step 4). The blind spot described in this note
# remains real and unfixed for `sed -i`/`python3 -c` payloads specifically.


# Command basenames that are read-only regardless of arguments — the only
# enumeration this detector permits (see module note above). Kept deliberately
# narrow: too wide and the allowlist becomes the new bypass surface. Extend only
# with commands that categorically cannot write a file through normal usage.
_READONLY_COMMAND_NAMES: frozenset[str] = frozenset(
    [
        "cat", "grep", "rg", "ls", "find", "head", "tail", "wc",
        "diff", "stat", "file", "md5sum", "sha1sum", "sha256sum",
        "less", "more", "nl", "od", "cksum", "du", "pwd",
        "readlink", "basename", "dirname", "true", "false",
        # Widened round 2 (security review over-blocking findings, non-blocking
        # but confirmed safe): these categorically cannot write a file through
        # normal usage either. `echo`/`printf` naming an outside path via a bare
        # argument are still safe to allow here because a REDIRECT write
        # (`echo hi > /outside/path`) is caught earlier by step 4's
        # _absolute_path_targets, which runs before this allowlist is ever
        # consulted — confirmed live during round-2 review.
        #
        # Round 3 (security review finding R2-3): `sort`, `uniq`, `xxd`, `tree`
        # were dropped from this widening. All four take a named output-file
        # operand/flag (`sort -o FILE`, `uniq IN OUT`, `xxd -o`/redirected form,
        # `tree -o FILE`) that genuinely writes — live-verified for `sort`/`uniq`,
        # including reaching audit.jsonl, since this allowlist short-circuits
        # _is_segment_write_candidate before the dial-registry basename check
        # ever runs. That widening was reviewer-suggested in round 1 and
        # reviewer-retracted in round 2 after checking it against the real
        # command interfaces — see PR #1751 review history.
        "echo", "printf", "test", "[", "jq", "cut", "tr",
        "column", "realpath", "strings", "comm", "which", "type",
    ]
)

# Interpreters whose `-c <code>` payload we inspect for write-shaped calls before
# deciding whether to path-scan (see module note above). Without `-c`, the
# segment is still scanned for absolute-path tokens like any other unenumerated
# command (this covers `-m <module>` invocations, whose module name and
# arguments are visible tokens) — the one shape genuinely NOT scannable this way
# is a plain script-file invocation (`python3 seed.py`), where the write target
# lives inside a file we cannot see into; see the F19 residual-risk note above.
_PY_INTERPRETER_NAMES: frozenset[str] = frozenset(["python3", "python"])

# Round-1 write-call-name enumeration (`_PY_WRITE_SIGNAL_RE`) was removed in round
# 2 — round-1 security review live-verified it was incomplete by design (os.system,
# shutil.rmtree, subprocess.run(['tee', ...]), os.open(O_CREAT), etc. all bypassed
# it). Round 2 replaced it with an inverted regex pair (_PY_WRITE_MODULE_RE /
# _PY_READ_SHAPE_RE, both `search()`-based) — round-2 security review (finding
# R2-1) found that pair was still enumeration wearing an allowlist's clothes: it
# proved "a read call appears somewhere in the payload", not "the payload is only
# a read", so `Path(x).write_text(Path('/etc/hostname').read_text())`,
# `open(path, chr(119)).write(...)`, a mode built from a variable, and
# `Path(x).unlink()` next to an unrelated read all cleared it and were live-
# verified to actually write/delete — a regression against round 1, which caught
# all four incidentally via its write-call-name list.
#
# Round 3 replaces both regexes with an AST-based structural proof: parse the
# payload with `ast.parse` (pure, no filesystem I/O, no subprocess — same
# constraint as the rest of this module), then require EVERY node in the tree to
# be one of a tiny set of side-effect-free shapes, EVERY Call to resolve to a name
# in a narrow read-only allowlist (with open() additionally checked for a
# read-mode), and EVERY Import/ImportFrom to name only json/pathlib. Anything
# unparseable, or containing any node outside that set — an Assign, a BinOp used
# to build a mode string, a call to an unrecognised name — is NOT exempted; it
# falls through to the same absolute-path-token scan every other unenumerated
# command already gets. This is structural, not enumerative: an unrecognised
# construct blocks *because* it is unrecognised, not because it matches a
# denylist entry one level down (the same mistake made twice already — see the
# history above).
_PY_READ_ALLOWED_CALL_NAMES: frozenset[str] = frozenset(
    ["open", "print", "read", "read_text", "read_bytes", "readlines", "Path", "load", "loads", "str", "len"]
)

# Modules a payload may `import` or `from ... import ...`, and — for the `from`
# form — the specific names it may pull in. Kept to exactly what the one existing
# legitimate use case needs (tests/test_dial_sandbox_integration.py::
# test_allows_reading_dial_registry: `import json; print(open(path).read())`) plus
# `pathlib.Path` for read-only Path usage. Anything else fails closed.
_PY_READ_ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "json": frozenset(["load", "loads"]),
    "pathlib": frozenset(["Path"]),
}

# Node types a read-only payload's AST may consist of. Deliberately excludes
# Assign, BinOp, Subscript, comprehensions, function/lambda defs, and every other
# construct that isn't needed to express "call a handful of read functions and
# print/return the result" — an Assign or BinOp is exactly how the round-2
# bypasses smuggled a dynamically-built write mode (`m = 'w' + ''`) past a
# substring check, so those shapes are simply not in the recognised set at all.
_PY_READ_SAFE_NODE_TYPES: tuple[type, ...] = (
    ast.Module,
    ast.Expr,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.keyword,
)


def _py_call_target_name(node: ast.Call) -> Optional[str]:
    """Return the resolvable name of a Call's target.

    Only a bare name (`open(...)`) or a single-level attribute access
    (`x.read()`, `json.load(...)`) resolve — anything else (a subscript, a call
    result being called again, etc.) returns None and is treated as unrecognised
    by the caller.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _py_read_mode_constant(node: ast.expr) -> bool:
    """Return True if *node* is a literal string constant that is a pure read
    mode: `r`, optionally followed by `b`/`t` modifiers only.

    Round-3 security review (finding R3-1a) live-verified the prior
    `startswith("r")` check admitted `r+`, `rb+`, `r+b` — all read-*write*
    modes, since `"r+".startswith("r")` is True even though the trailing `+`
    makes the file writable. `re.fullmatch` constrains the whole string
    instead of just its prefix, so a `+` anywhere fails the match.
    """
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"r[bt]*", node.value) is not None
    )


def _py_open_call_is_read_mode(node: ast.Call) -> bool:
    """Return True if an `open(...)`-named Call has no mode argument, or a literal
    mode string starting with `r`. A non-literal mode (a variable, a
    concatenation, a `chr()` call) is never provable as read-only, so it returns
    False — this is what closes the `open(path, chr(119))` and mode-via-variable
    bypasses: neither survives to this check at all, since `chr(...)` is itself
    an unrecognised call name and a bare variable name is not an `ast.Constant`.

    The builtin `open(path, mode)` and an attribute-style `.open(mode)` method
    call (e.g. `Path(x).open('w')`) are NOT the same shape: the builtin's mode is
    its 2nd positional arg (the receiver has no implicit `self`), while the
    method form's mode is its 1st (the path is supplied by the receiver, not an
    argument). Both resolve to the same call name (`"open"`) in
    `_py_call_target_name`, since that helper only looks at the trailing
    attribute/name and can't see the receiver's type — treating them identically
    would either mis-scan the path argument as a mode (bare-Name case) or the
    mode as a mode at the wrong index (attribute case). Distinguished here by
    `node.func`'s shape. An attribute-style open() with 2+ positional args isn't
    a shape `Path.open` supports at all, so it's treated conservatively as NOT
    read-only rather than guessed at.
    """
    is_builtin_open = isinstance(node.func, ast.Name) and node.func.id == "open"
    if is_builtin_open:
        if len(node.args) >= 2:
            return _py_read_mode_constant(node.args[1])
    else:
        if len(node.args) >= 2:
            return False
        if len(node.args) == 1:
            return _py_read_mode_constant(node.args[0])
    for kw in node.keywords:
        if kw.arg == "mode":
            return _py_read_mode_constant(kw.value)
    return True


def _python_payload_is_read_only(payload: str) -> bool:
    """Return True only if *payload* parses as a pure read (see module note above
    `_PY_READ_ALLOWED_CALL_NAMES` for the full design rationale and history).

    Fails closed (returns False) on: a SyntaxError from `ast.parse`; any node in
    the tree outside `_PY_READ_SAFE_NODE_TYPES`; any Call whose target doesn't
    resolve to a name in `_PY_READ_ALLOWED_CALL_NAMES`; any Call carrying a
    `file=` keyword; any `open()` call whose mode isn't a literal read mode; or
    any Import/ImportFrom naming a module or symbol outside
    `_PY_READ_ALLOWED_IMPORTS`. False here does not mean the command is blocked
    — it means the payload is scanned for absolute-path tokens like everything
    else non-exempt, and only blocks if one is found outside the
    worktree/tmp/dial-registry.

    `file=` is checked on every Call, not just `print`, per round-3 security
    review (finding R3-1b): `print(..., file=open(p, 'r+'))` turns `print` —
    read-shaped when it writes to stdout — into a general file writer, and
    banning the receiver-redirect keyword itself is the structural fix rather
    than special-casing `print`'s name.
    """
    try:
        tree = ast.parse(payload, mode="exec")
    except (SyntaxError, ValueError):
        return False

    for node in ast.walk(tree):
        if not isinstance(node, _PY_READ_SAFE_NODE_TYPES):
            return False
        if isinstance(node, ast.Call):
            name = _py_call_target_name(node)
            if name is None or name not in _PY_READ_ALLOWED_CALL_NAMES:
                return False
            if name == "open" and not _py_open_call_is_read_mode(node):
                return False
            if any(kw.arg == "file" for kw in node.keywords):
                return False
        elif isinstance(node, ast.Import):
            for alias_node in node.names:
                if alias_node.name not in _PY_READ_ALLOWED_IMPORTS:
                    return False
        elif isinstance(node, ast.ImportFrom):
            allowed_names = _PY_READ_ALLOWED_IMPORTS.get(node.module or "")
            if allowed_names is None:
                return False
            for alias_node in node.names:
                if alias_node.name not in allowed_names:
                    return False
    return True


# Absolute-path-shaped substring inside any token (a bare argument, or the contents
# of a quoted string such as a python -c payload). The negative lookbehind excludes
# `:`, a word character, or another `/` immediately before the leading `/` so URLs
# (`https://host/path`) and sed substitution syntax (`s/a/b/`) are not mistaken for
# path candidates — both are real false-positive risks flagged in the Spec's
# Implementation Notes.
#
# Round-1 defect (security review B2a): this regex alone never matches a
# `//`-rooted path (`//home/...`) — the lookbehind rejects a `/` preceded by `/`,
# so the second slash never starts a match either. Callers must collapse a leading
# run of 2+ slashes (not preceded by `:`, so URL schemes are untouched) via
# `_COLLAPSE_MULTISLASH_RE` on each token BEFORE scanning with this regex — see
# `_classify_unenumerated_write`.
_ABS_PATH_TOKEN_RE = re.compile(r"(?<![:\w/])(/[\w][\w./-]*)")

# Collapses a run of 2+ slashes not preceded by `:` down to one, applied to each
# token before `_ABS_PATH_TOKEN_RE` scans it (round-1 B2a fix). The `(?<!:)`
# keeps `https://host//path` intact at the scheme boundary — the `:` lookbehind
# in `_ABS_PATH_TOKEN_RE` already excludes that case on its own merits — while
# still collapsing a bare `//home/...` root-path down to a real, matchable
# `/home/...` candidate.
_COLLAPSE_MULTISLASH_RE = re.compile(r"(?<!:)/{2,}")

# Reason string for this detector. Deliberately distinct from the pre-existing
# "output redirect outside worktree" reason (used by the enumerated-writer loop in
# classify_bash step 4) so block audits in .autonomous-team/hook-events/blocks-*.jsonl
# can tell the two detectors apart, per the Spec's Implementation Notes.
_UNENUMERATED_WRITE_REASON = "unenumerated command wrote outside worktree (path-token scan)"

# D#2246: matches a token that IS a path, not a token that merely CONTAINS
# one. The run of path characters must start at the token's own beginning,
# or right after a `key=` glue point (`of=/dev/x`, `--out=/path`) — the one
# shape `dd if=.../of=...` and glued long-flags need — and must extend all
# the way to the end of the token, with nothing else around it. That anchor
# is what a sed script (`s|/x|y|`), a quoted prose argument ("fixes the
# /api/v1/loop route"), or a `python -c` payload can never satisfy — none of
# them end the token at the path. D#2246's PM reproduction (R3, R4, R8) found
# the OLD unanchored `_ABS_PATH_TOKEN_RE` substring scan blocked all three
# for exactly that reason: it never required the match to consume the token.
#
# D#2246 re-review (security + code review, PR #2265): the character class
# is deliberately wider than a strict `[\w./-]` path would need, for two
# separate reasons the review found both matter:
#   - shell glob metacharacters (`* ? [ ] { } ,`) must be ADMITTED as
#     candidates rather than dropped — `rm -rf <dir>/*` is the reflexive
#     spelling for "empty this directory" and was going BLOCK -> ALLOW
#     because `*` fell outside the old class and the anchored match found
#     nothing at all. `_glob_literal_prefix` below turns the admitted
#     glob-shaped candidate back into the concrete, glob-free directory it
#     actually expands inside before the containment check runs.
#   - ordinary path punctuation (`@ + : ~ ( )`) must be admitted too — an
#     npm-scoped package directory (`tui/node_modules/@types`), a `+`/`:`
#     in a filename, or an editor backup suffix (`CLAUDE.md~`) are not
#     obfuscation, they're just legal path characters the original class
#     happened to exclude.
_WHOLE_TOKEN_PATH_RE = re.compile(r"(?:^|=)(/[\w./*?\[\]{}@+:~(),-]*)$")

# Shell glob metacharacters. A candidate containing one of these is a
# PATTERN, not a literal path — see `_glob_literal_prefix`.
_GLOB_META_RE = re.compile(r"[*?\[\]{}]")


def _glob_literal_prefix(path: str) -> str:
    """Return the concrete, glob-free directory prefix of *path*.

    `<dir>/*` and `<dir>/*.md` are patterns the shell expands, not literal
    paths — `_WHOLE_TOKEN_PATH_RE` admits them as candidates so they aren't
    silently dropped (D#2246 review finding #1), but the worktree-
    containment check needs the literal directory the glob expands INSIDE,
    not the pattern text. Truncates at the path SEGMENT containing the
    first glob metacharacter and returns everything before it; a path with
    no glob metacharacter at all is returned unchanged.
    """
    match = _GLOB_META_RE.search(path)
    if match is None:
        return path
    prefix = path[: match.start()]
    return prefix.rsplit("/", 1)[0] or "/"

# Call names that are unambiguous evidence of a filesystem/process mutation
# inside a `python3 -c` / `python -c` payload. Used ONLY to decide whether a
# write-candidate payload also deserves the deep `_ABS_PATH_TOKEN_RE`
# substring scan (see `_python_payload_has_write_call`) — never as the
# allow/block verdict itself, and deliberately NOT the inverse of
# `_PY_READ_ALLOWED_CALL_NAMES`. A payload can fail that narrow read-only
# proof for reasons that have nothing to do with writing — an `import sys`,
# a `d.get(...)` call, a bare assignment — and D#2246's R6 is exactly that
# shape: `d=json.load(open(path)); print(d.get('k'))` is a plain read that
# fails `_python_payload_is_read_only` on the assignment alone. Re-using
# "not proven read-only" here would deep-scan and re-block that read, which
# is the regression this fix exists to remove.
_PY_WRITE_CALL_NAMES: frozenset[str] = frozenset(
    [
        "write", "write_text", "write_bytes", "writelines",
        "unlink", "remove", "rmdir", "removedirs", "rmtree",
        "rename", "replace", "move", "copy", "copyfile", "copytree", "copy2",
        "mkdir", "makedirs", "chmod", "chown", "truncate", "symlink", "link",
        "system", "popen", "run", "Popen", "call", "check_call", "check_output",
        # D#2246 re-review (security + code review, PR #2265): both reviews
        # independently reproduced ordinary, non-obfuscated stdlib calls
        # sailing through — Path(...).touch() in particular is one of the
        # most common ways to create a file in Python. extractall/writestr
        # write an arbitrary number of files with no path token of their
        # own on the call; mknod creates a filesystem node directly.
        "touch", "symlink_to", "hardlink_to", "utime",
        "extractall", "writestr", "mknod",
    ]
)


def _python_payload_has_write_call(payload: str) -> bool:
    """Return True if *payload* contains a call that's unambiguous evidence
    of a write or other filesystem/process mutation, or is unparseable.

    Gate for `_classify_unenumerated_write`'s deep substring scan of a `-c`
    payload (D#2246 item 1) — a POSITIVE, narrow proof of write intent,
    deliberately separate from `_python_payload_is_read_only`'s much
    stricter "is this ENTIRELY a proven read" proof. See the module note
    above `_PY_WRITE_CALL_NAMES` for why conflating the two re-introduces
    the false positive this fix exists to remove.

    Fails open to True on a SyntaxError — an unparseable payload proves
    nothing either way, and the deep scan itself only blocks when it finds
    an actual outside-worktree path candidate, so failing open here costs
    nothing but a wider (still precise) scan.
    """
    try:
        tree = ast.parse(payload, mode="exec")
    except (SyntaxError, ValueError):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _py_call_target_name(node)
            if name in _PY_WRITE_CALL_NAMES:
                return True
            if any(kw.arg == "file" for kw in node.keywords):
                return True
            if name == "open" and not _py_open_call_is_read_mode(node):
                return True
    return False


def _looks_like_real_fs_path(candidate: str, worktree_resolved: Path) -> bool:
    """Return False for a path-shaped candidate that isn't really naming a
    filesystem location this scan should judge — a URL, or a token whose
    top-level directory doesn't correspond to anything real (D#2246 R2/R8:
    `/repos`, `/api`, `/v1`, a bare `/CLAUDE.md` fragment mistaken for a
    write target).

    A candidate under this invocation's own worktree, or under
    `MAIN_REPO_ROOT` / the filesystem-derived root, is always "real"
    regardless of whether that root exists on THIS host's disk —
    testsupport/fixture_paths.py deliberately roots its fixtures at a path
    (`/synthetic/...`) that exists nowhere on any real machine (see that
    module's docstring), and this scan must judge those fixtures exactly the
    way it judges a genuine checkout. Anything else falls back to a real
    `os.path.isdir` check on the candidate's own top-level segment — `/home`,
    `/tmp`, `/etc` exist on any host this runs on; `/repos`/`/api` never do.

    A stat() failure (permission, exotic filesystem) fails toward "not
    real" — exclude the candidate rather than block on it, consistent with
    this fix's over-blocking-is-worse-than-under-blocking priority.
    """
    if "://" in candidate:
        return False
    for known_root in (worktree_resolved, MAIN_REPO_ROOT, _DERIVED_MAIN_REPO_ROOT):
        try:
            if Path(candidate).is_relative_to(known_root):
                return True
        except Exception:
            continue
    try:
        parts = candidate.split("/", 2)
        root_segment = "/" + parts[1] if len(parts) > 1 and parts[1] else "/"
        return os.path.isdir(root_segment)
    except Exception:
        return False


def _evaluate_unenumerated_candidate(candidate: str, worktree_resolved: Path) -> Optional["Decision"]:
    """Judge one path candidate found by `_classify_unenumerated_write`'s
    token scan. Returns a block Decision, or None to keep looking.

    Shared by both matching strategies below (the whole-token scan and the
    established-write-intent deep payload scan) so the exemptions and the
    reason text can't drift between them. Check order is unchanged from the
    pre-D#2246 scan: kernel/input-only devices, then the /tmp exemption,
    then dial-registry protection, then worktree containment.

    D#2246 re-review: *candidate* may be a glob PATTERN (`<dir>/*`), since
    `_WHOLE_TOKEN_PATH_RE` now admits glob metacharacters as candidates
    rather than dropping them. Every check below runs against the pattern's
    concrete `_glob_literal_prefix` — the directory a glob actually expands
    inside — while the block message still names the original candidate
    text so it reads as what the agent actually typed.
    """
    literal = _glob_literal_prefix(candidate)
    if _is_kernel_device(literal) or _is_input_only_device(literal):
        return None
    if _is_ephemeral_tmp_path(literal):
        return None
    if not _looks_like_real_fs_path(literal, worktree_resolved):
        return None
    if _is_dial_protected_path(literal):
        return Decision(
            allow=False,
            reason=f"dial-registry write blocked: {Path(literal).name} is read-only for sub-agents",
        )
    try:
        outside = not Path(literal).is_relative_to(worktree_resolved)
    except Exception:
        outside = True
    if outside:
        # D#2246 item 6: name the offending token so the block message can
        # point at a recovery path instead of only forbidding retry.
        return Decision(allow=False, reason=f"{_UNENUMERATED_WRITE_REASON} [token: {candidate}]")
    return None


def _segment_cd_target(tokens: list[str]) -> Optional[str]:
    """Return this segment's `cd` target operand, or None if it is not a
    `cd` invocation.

    `cd` itself is never a write (see `_is_segment_write_candidate`), but a
    write LATER in the same command can still land outside the worktree
    because of a `cd` earlier in it — the compensating guard D#2246 item 4
    requires. `_classify_unenumerated_write`'s segment walk calls this on
    every segment to track that, independently of write-candidacy.
    """
    if _segment_command_name(tokens) != "cd":
        return None
    i = 0
    n = len(tokens)
    while i < n and (
        tokens[i] == "env"
        or (_ENV_PREFIX_RE.match(tokens[i]) and "=" in tokens[i] and not tokens[i].startswith("-"))
    ):
        i += 1
    for tok in tokens[i + 1 :]:
        if not tok.startswith("-"):
            return tok
    return None


def _advance_cd_for_scan(
    effective_cwd: Path, target_tok: str, worktree_resolved: Path
) -> tuple[Path, bool]:
    """Apply one `cd` target to *effective_cwd* for the unenumerated-write
    scan's own escape tracking. Returns (new_effective_cwd, escaped).

    Mirrors `_cd_escape_relative_write`'s cwd walk (same `_expand_home_prefix`
    / ephemeral-tmp exemption), but answers a different question for a
    different caller: not "does THIS redirect escape", but "is the shell
    currently outside the worktree", so `_classify_unenumerated_write` can
    judge a LATER, non-redirect write segment (`touch`, `sed -i <relative
    file>`) against it — a shape `_cd_escape_relative_write`'s redirect-only
    scan never looks at (D#2246 item 4).
    """
    target = _expand_home_prefix(target_tok)
    try:
        candidate = Path(target)
        new_cwd = candidate if candidate.is_absolute() else effective_cwd / candidate
    except Exception:
        new_cwd = effective_cwd
    try:
        resolved = new_cwd.resolve()
    except Exception:
        resolved = new_cwd
    if _is_ephemeral_tmp_path(str(resolved)):
        return new_cwd, False
    try:
        escaped = not resolved.is_relative_to(worktree_resolved)
    except Exception:
        escaped = True
    return new_cwd, escaped

# Matches a dial-protected basename as a whole word, regardless of whether it's
# embedded in an absolute or relative path (or bare) — mirrors what
# classify_path_write already does for relative paths (see _is_dial_protected_path).
# Longest-first ordering avoids any accidental partial-match ordering issues.
_PROTECTED_BASENAME_RE = re.compile(
    r"(?<![\w.-])("
    + "|".join(re.escape(s) for s in sorted(_DIAL_PROTECTED_SUFFIXES, key=len, reverse=True))
    + r")(?![\w.-])"
)


def _split_command_segments(tokens: list[str]) -> list[list[str]]:
    """Split an already-normalised shlex token list into per-command segments.

    Uses _SHELL_SEPARATORS (the same set _command_positions uses) so `;`, `&&`,
    `||`, `|`, `&` each start a new segment. This is what closes the "segment
    laundering" bypass (Spec section C): a read-only command earlier in the string
    must not exempt a write later in the same command string, e.g.
    `cat /etc/hosts && python3 -c "...write..."` must still block on the second half.

    Precondition: *tokens* must come from a command string that was normalised
    with `_classify_unenumerated_write`'s separator-padding step first, NOT a raw
    `shlex.split(command)`. shlex only ever emits `;`/`&&`/`||`/`|`/`&` as their
    own standalone tokens when the source string already has whitespace around
    them — `cat /x;sed -i ...` (no spaces) tokenises as a single glued token and
    silently defeats this splitter (round-1 security review, finding B1: a live,
    verified file overwrite through exactly this gap).
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _segment_command_name(tokens: list[str]) -> Optional[str]:
    """Return the base executable name for a segment, skipping env-var assignments."""
    for tok in tokens:
        if tok == "env":
            continue
        if _ENV_PREFIX_RE.match(tok) and "=" in tok and not tok.startswith("-"):
            continue
        return os.path.basename(tok)
    return None


def _sed_is_in_place(tokens: list[str]) -> bool:
    """Return True if a `sed` segment uses in-place editing.

    Matches `-i`, `-i.bak` (GNU no-space suffix form), and `--in-place`/
    `--in-place=.bak` (long form — Spec acceptance item A4 requires this form
    caught, not just `-i`).
    """
    for tok in tokens[1:]:
        if tok == "-i" or (tok.startswith("-i") and not tok.startswith("--")):
            return True
        if tok == "--in-place" or tok.startswith("--in-place="):
            return True
    return False


def _awk_is_in_place(tokens: list[str]) -> bool:
    """Return True if an `awk` segment uses GNU awk's `-i inplace` extension."""
    for idx, tok in enumerate(tokens[1:], start=1):
        if tok == "-i" and idx + 1 < len(tokens) and tokens[idx + 1] == "inplace":
            return True
        if tok.startswith("-i") and "inplace" in tok:
            return True
    return False


# Matches a token that carries a `-c` flag in any single-dash form Python
# accepts: bare (`-c`), clustered with other short flags before it (`-Ic`), code
# glued directly after with no separating space (`-cCODE`), or both (`-IcCODE`).
# Round-2 security review (finding R2-2) live-verified `_python_c_payload`'s old
# `tok == "-c"` exact-match missed the glued and clustered forms entirely —
# shlex preserves both (`python3 -c'code'` tokenises as `['python3', '-ccode']`,
# `python3 -Ic 'code'` as `['python3', '-Ic', 'code']`) — and the resulting
# `payload is None` short-circuited the whole segment out of both the path scan
# AND the dial-registry basename check before either ever ran.
_PY_DASH_C_TOKEN_RE = re.compile(r"^-([A-Za-z]*)c(.*)$")


def _python_c_payload(tokens: list[str]) -> tuple[Optional[str], bool]:
    """Return (payload, saw_dash_c) for a python invocation's `-c` code, if any.

    payload is the inline code string when it can be cleanly extracted: an exact
    `-c <code>` as two tokens, a clustered `-Ic <code>` (letters before `c`, code
    as the next token), or code glued directly onto the flag (`-cCODE` /
    `-IcCODE`). saw_dash_c is True whenever a token looks like it carries a `-c`
    flag at all, even when payload couldn't be pulled out (e.g. `-c` is the very
    last token with nothing after it) — callers use this to fail closed (scan the
    segment) rather than silently treating an unparseable `-c` invocation the
    same as a plain `python3 script.py` run, which is the genuine "no -c at all"
    case this function returns (None, False) for.
    """
    for idx, tok in enumerate(tokens):
        if not tok.startswith("-") or tok.startswith("--"):
            continue
        match = _PY_DASH_C_TOKEN_RE.match(tok)
        if not match:
            continue
        glued = match.group(2)
        if glued:
            return glued, True
        if idx + 1 < len(tokens):
            return tokens[idx + 1], True
        return None, True
    return None, False


def _segment_git_has_verb(tokens: list[str]) -> bool:
    """Return True if a `git` segment's tokens contain a discoverable verb-shaped
    subcommand (D#1931 defect 2).

    *tokens* is one already-isolated shell segment (from
    `_split_command_segments`) whose command name resolved to `git`. Mirrors
    the per-invocation option-skipping walk in `_walk_git_invocations` (same
    `_GIT_VALUE_TAKING_GLOBAL_OPTS` skip-the-value handling and
    `_GIT_VERB_SHAPE_RE` verb test) but scoped to a single, already-isolated
    invocation — no cwd or cross-invocation state needed, it only answers
    "would step 3 have found a verb to vet for this invocation".

    A verbless invocation (`git <path>`, `git -C <dir>` with nothing after)
    is exactly the one step 3 never pairs with a verb, so
    `_is_segment_write_candidate` must not blanket-exempt it here either.
    """
    i = 0
    n = len(tokens)
    # Skip env-var-assignment / `env` prefix tokens, mirroring _segment_command_name.
    while i < n and (
        tokens[i] == "env"
        or (_ENV_PREFIX_RE.match(tokens[i]) and "=" in tokens[i] and not tokens[i].startswith("-"))
    ):
        i += 1
    if i >= n or not _is_git_token(tokens[i]):
        return False
    i += 1
    while i < n:
        tok = tokens[i]
        opt_name, glued_value = _split_glued_git_option(tok)
        if opt_name in _GIT_VALUE_TAKING_GLOBAL_OPTS:
            i += 1 if glued_value is not None else 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if _GIT_VERB_SHAPE_RE.match(tok):
            return True
        i += 1
    return False


def _is_segment_write_candidate(tokens: list[str]) -> bool:
    """Return True if *tokens* (one shell segment) should be scanned for write targets.

    Deny-by-default: True (scan it) unless the command name is on the narrow
    read-only allowlist, or is a sed/awk invocation without its in-place flag, or is
    a `python -c` invocation whose payload contains no write-shaped call.

    `git` is also exempt (D#1756/D#1903), but for a different reason than the
    allowlist above: by the time classify_bash falls through to step 4b (this
    scan), every git verb in the WHOLE command has already been walked and
    verdicted by step 3 — an always-blocked verb (checkout/reset/etc.) already
    returned a block, and a write verb (commit/push/etc.) already returned a
    block unless its effective CWD stayed inside the worktree. So a `git`
    segment reaching here is already known-safe; the only thing left to catch
    is whatever ELSE is chained onto the same command line. Scanning it anyway
    would misread a bare path operand — most commonly `-C <path>` — as a write
    target, which is exactly the regression this Spec's "regression trap"
    warns about: `git -C <main-repo> log --oneline -5` and `git -C <main-repo>
    status` must keep allowing.

    The exemption above only holds when step 3 actually HAD a verb to vet
    (D#1931 defect 2): a verbless invocation (`git <path>`, bare `git -C
    <dir>`) is never paired with a verb by `_walk_git_invocations`, so step 3
    silently skips it, and step 4b was blanket-exempting it too — `git
    <main-repo>/CLAUDE.md` read as `allow=True` on `main` with no check ever
    having looked at the path operand. `_segment_git_has_verb` re-derives,
    for THIS segment only, whether step 3 had anything to vet; only a verbed
    invocation gets the blanket exemption, a verbless one falls through to
    the normal scan below like any other command.
    """
    name = _segment_command_name(tokens)
    if name is None:
        return False
    if name == "cd":
        # D#2246 item 4: `cd` alone writes nothing. The compensating guard —
        # a write candidate reached AFTER a `cd` that left the worktree —
        # lives in `_classify_unenumerated_write`'s own segment walk, which
        # tracks `cd` targets independently of this per-segment check.
        return False
    if name in _READONLY_COMMAND_NAMES:
        return False
    if name == "git":
        return not _segment_git_has_verb(tokens)
    if name == "sed":
        return _sed_is_in_place(tokens)
    if name == "awk":
        return _awk_is_in_place(tokens)
    if name in _PY_INTERPRETER_NAMES:
        payload, saw_dash_c = _python_c_payload(tokens)
        if payload is not None:
            return not _python_payload_is_read_only(payload)
        # A `-c`-shaped flag was present but we couldn't cleanly extract its
        # code (R2-2 fail-closed fix) — scan the segment rather than skip it.
        if saw_dash_c:
            return True
        # No `-c` flag at all. This covers two shapes: `python3 -m <module> ...`
        # (the module and its arguments, including any output path, are right
        # there in the tokens — scan them) and `python3 <script>.py ...` (the
        # documented F19 residual risk: the write target lives inside a script
        # file we cannot see into, so there's nothing in the command string to
        # scan). Round-3 security review (finding R3-2) live-verified this arm
        # used to `return False` for both shapes, which skipped `-m` invocations
        # entirely — `python3 -m json.tool <in> <outside>` and `python3 -m
        # zipfile --create <outside> ...` both wrote real files. Scanning here
        # only blocks if an outside-worktree absolute path token is actually
        # present, so the genuine F19 case (`python3 scripts/seed.py`, no
        # absolute path operand) still allows.
        return True
    return True


# Devices that carry no write-target information when they appear as an operand
# in step 4b's scan (D#1749 round 5). Deliberately NOT folded into
# _KERNEL_DEVICE_PREFIXES / _is_kernel_device above, even though the two lists
# overlap in spirit -- the two call sites ask different questions:
#
#   * _absolute_path_targets (redirect/write-target context) asks "is writing
#     HERE a worktree-boundary violation?" Its docstring's claim is that these
#     paths "are never real writes" as a DESTINATION -- i.e. safe to treat as a
#     no-op write target.
#   * _classify_unenumerated_write's operand scan (this list) asks "does this
#     token being PRESENT imply a write outside the worktree?" A command that
#     merely reads from /dev/urandom, /dev/random, /dev/zero, or /dev/full as an
#     INPUT (e.g. `dd if=/dev/urandom of=<worktree>/seed.bin`) implies nothing
#     about where its actual output goes -- the real write target is caught
#     separately when it's an outside-worktree token in its own right.
#
# Widening the shared _KERNEL_DEVICE_PREFIXES instead would have been the
# smaller diff, but it would silently assert `> /dev/urandom` and `> /dev/random`
# are never real writes at the _absolute_path_targets call site too -- writing to
# /dev/urandom/​/dev/random feeds the kernel entropy pool, a real (if narrow)
# system-level side effect, unlike /dev/null's pure discard. That's a claim this
# fix does not make, so the exemption stays scan-local to step 4b rather than
# merged into the shared list.
_INPUT_ONLY_DEVICE_PREFIXES: tuple[str, ...] = (
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
)


def _is_input_only_device(path: str) -> bool:
    """Return True if *path* is a device that never carries write-target meaning.

    Scan-local to _classify_unenumerated_write's operand scan (step 4b) -- see the
    module note above _INPUT_ONLY_DEVICE_PREFIXES for why this is a separate list
    from _is_kernel_device rather than a widening of it.
    """
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _INPUT_ONLY_DEVICE_PREFIXES
    )


# Shell interpreter names — a heredoc fed to one of these is read as a
# SCRIPT, the same way `_PY_INTERPRETER_NAMES` are for python. See
# `_heredoc_feeding_kind`.
_SHELL_INTERPRETER_NAMES: frozenset[str] = frozenset(["bash", "sh", "zsh"])

# Heredoc-within-heredoc recursion is already bounded by construction — a
# captured body is always a strict substring of the text it was found in,
# so it terminates on its own. This is belt-and-suspenders against a
# pathologically deep nesting wasting CPU, not a correctness requirement.
_MAX_HEREDOC_RECURSION_DEPTH = 5


def _heredoc_feeding_kind(line_before_marker: str) -> Optional[str]:
    """Return "python", "shell", or None for what will read the heredoc
    started on this line AS ITS OWN PROGRAM (D#2246 review finding #2).

    `python3 <<EOF` / `bash <<EOF` read the heredoc body as the program to
    run — stripping that body (as `_strip_heredoc_bodies` does unconditionally
    for the general R5 diff-header case) throws away the only text
    describing what the command will do. An ordinary command that merely
    receives the heredoc as stdin input (`cat <<EOF`, `tee file <<EOF`) is
    NOT one of these — that shape is left to `_strip_heredoc_bodies`'s
    existing unconditional strip, unchanged.

    *line_before_marker* is the heredoc-start line with everything from
    `<<` onward already removed by the caller.
    """
    try:
        tokens = _tokenize_punctuation_aware(line_before_marker)
    except ValueError:
        return None
    positions = _command_positions(tokens)
    if not positions:
        return None
    name = os.path.basename(tokens[positions[-1]])
    if name in _PY_INTERPRETER_NAMES:
        return "python"
    if name in _SHELL_INTERPRETER_NAMES:
        return "shell"
    return None


def _strip_heredoc_bodies_capturing(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Like `_strip_heredoc_bodies`, but returns the bodies fed to a python
    or shell interpreter instead of silently discarding them (D#2246 review
    finding #2), tagged `("python" | "shell", body_text)`.

    Every other heredoc body — fed to `cat`, `tee`, or anything else that
    just reads it as input, the R5 diff-header case this was built for — is
    still discarded exactly as `_strip_heredoc_bodies` already did; this
    function's `stripped_command` return value is byte-for-byte what
    `_strip_heredoc_bodies` would have produced for the same input.
    """
    lines = command.split("\n")
    out_lines: list[str] = []
    captured: list[tuple[str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out_lines.append(line)
        match = _HEREDOC_START_RE.search(line)
        i += 1
        if match is None:
            continue
        strip_leading_tabs = match.group(1) == "-"
        delimiter = match.group(3)
        kind = _heredoc_feeding_kind(line[: match.start()])
        body_lines: list[str] = []
        while i < n:
            body_line = lines[i]
            i += 1
            check = body_line.lstrip("\t") if strip_leading_tabs else body_line
            if check == delimiter:
                break
            body_lines.append(body_line)
        if kind is not None:
            captured.append((kind, "\n".join(body_lines)))
    return "\n".join(out_lines), captured


def _scan_command_segments(
    command: str, cwd: str, worktree_resolved: Path, *, depth: int = 0
) -> Optional[Decision]:
    """Deny-by-default path scan for writers _absolute_path_targets doesn't enumerate.

    See the module note above _READONLY_COMMAND_NAMES for the full design rationale.
    Returns a block Decision, or None to fall through to allow.

    This is the core `_classify_unenumerated_write` delegates to — pulled
    into its own function (D#2246 review finding #2) so a shell heredoc
    body (`bash <<EOF ... EOF`) can be judged by the EXACT SAME rules as
    the outer command it's embedded in, rather than a cruder raw-substring
    rescan that couldn't tell a benign `cat /etc/hostname` read inside the
    heredoc from a `rm -f <main>/CLAUDE.md` write. `depth` bounds
    heredoc-within-heredoc recursion (see `_MAX_HEREDOC_RECURSION_DEPTH`).

    D#2246: heredoc bodies are stripped FIRST, before the newline-to-`;`
    normalisation below ever runs — a heredoc body line (a diff hunk's own
    `--- /path` / `+++ /path` header text, e.g.) would otherwise be turned
    into its own pseudo-segment by that normalisation and misread as an
    unknown command with a path-shaped operand (R5). A body fed to a
    python or shell interpreter is captured rather than discarded, and
    scanned below instead of being thrown away (review finding #2).
    """
    stripped, heredoc_payloads = _strip_heredoc_bodies_capturing(command)

    # Normalise shell separators to whitespace-padded form, and newlines to `;`,
    # BEFORE shlex.split — mirrors the normalise-before-tokenise pattern used
    # elsewhere in this module (check_claude_spawn, _extract_all_git_verbs,
    # is_real_git_rm_invocation). shlex.split only emits `;`/`&&`/`||`/`|`/`&` as
    # standalone tokens when they're already whitespace-padded in the source, and
    # never splits on a bare newline at all — a single missing space or a `\n`
    # instead of `;` defeats _split_command_segments entirely (round-1 security
    # review, finding B1, live-verified file overwrite via `cat /x;sed -i ...`
    # and via a literal newline in place of `;`).
    # Round-3 cleanup (non-blocking review note): the `&&`/`||`-specific padding
    # that used to run here was immediately undone by the generic `[;&|]` pass
    # below, which re-splits each `&&`/`||` into two individually-padded single
    # characters — both forms land in _SHELL_SEPARATORS (":190"), which
    # _split_command_segments checks membership against, so the two extra passes
    # were dead weight with no behavioural effect. Removed.
    normalised = stripped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ; ")
    normalised = re.sub(r"[;&|]", lambda m: f" {m.group()} ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()

    try:
        tokens = shlex.split(normalised)
    except ValueError:
        # Untokenisable — nothing safe to scan structurally. The redirect-regex path
        # in _absolute_path_targets already ran independently of tokenisation.
        tokens = None

    if tokens is not None:
        effective_cwd = Path(cwd)
        escaped_via_cd = False

        for segment in _split_command_segments(tokens):
            cd_target = _segment_cd_target(segment)
            if cd_target is not None:
                effective_cwd, escaped_via_cd = _advance_cd_for_scan(
                    effective_cwd, cd_target, worktree_resolved
                )
                continue

            if not _is_segment_write_candidate(segment):
                continue

            # D#2246 item 4's compensating guard: `cd` itself is exempt above,
            # but a write reached AFTER a `cd` that left the worktree must still
            # block — `cd {main} && sed -i s/a/b/ CLAUDE.md` writes a RELATIVE
            # path that carries no token of its own for the scan below to catch.
            if escaped_via_cd:
                return Decision(
                    allow=False,
                    reason=f"{_UNENUMERATED_WRITE_REASON} [cd left the worktree before this write]",
                )

            # Relative-path dial-registry basename protection, parity with what
            # classify_path_write already does for relative paths (Spec item B8).
            segment_text = " ".join(segment)
            basename_match = _PROTECTED_BASENAME_RE.search(segment_text)
            if basename_match:
                return Decision(
                    allow=False,
                    reason=f"dial-registry write blocked: {basename_match.group(1)} is read-only for sub-agents",
                )

            # D#2246 item 1: whole-token match, not substring search. A token
            # (or the value half of a `key=/path` glued argument) is a candidate
            # only when the path run reaches the token's own end — see
            # `_WHOLE_TOKEN_PATH_RE`'s docstring for why this is what separates
            # a real path-shaped argument from one merely mentioned inside a
            # longer string.
            for tok in segment:
                collapsed_tok = _COLLAPSE_MULTISLASH_RE.sub("/", tok)
                match = _WHOLE_TOKEN_PATH_RE.search(collapsed_tok)
                if match is None:
                    continue
                candidate = os.path.normpath(match.group(1))
                decision = _evaluate_unenumerated_candidate(candidate, worktree_resolved)
                if decision is not None:
                    return decision

            # D#2246 item 1: the deep, unanchored substring scan survives ONLY
            # for a `python3 -c` / `python -c` payload whose own content already
            # demonstrates write intent — see `_python_payload_has_write_call`
            # for why that's a different, narrower gate than "not proven
            # read-only" (which also fires for a genuine read like R6).
            seg_name = _segment_command_name(segment)
            if seg_name in _PY_INTERPRETER_NAMES:
                payload, _saw_dash_c = _python_c_payload(segment)
                if payload is not None and _python_payload_has_write_call(payload):
                    collapsed_payload = _COLLAPSE_MULTISLASH_RE.sub("/", payload)
                    for match in _ABS_PATH_TOKEN_RE.finditer(collapsed_payload):
                        candidate = os.path.normpath(match.group(1))
                        decision = _evaluate_unenumerated_candidate(candidate, worktree_resolved)
                        if decision is not None:
                            return decision

    if depth < _MAX_HEREDOC_RECURSION_DEPTH:
        for kind, payload in heredoc_payloads:
            if kind == "python":
                # Same gate as the `-c` payload above: deep-scan only when
                # the body itself demonstrates write intent.
                if _python_payload_has_write_call(payload):
                    collapsed_payload = _COLLAPSE_MULTISLASH_RE.sub("/", payload)
                    for match in _ABS_PATH_TOKEN_RE.finditer(collapsed_payload):
                        candidate = os.path.normpath(match.group(1))
                        decision = _evaluate_unenumerated_candidate(candidate, worktree_resolved)
                        if decision is not None:
                            return decision
            else:
                # "shell" — the body is itself a shell script. Judge it with
                # the exact same rules as the outer command (recursively),
                # not a blind substring scan that can't tell a read from a
                # write.
                decision = _scan_command_segments(
                    payload, cwd, worktree_resolved, depth=depth + 1
                )
                if decision is not None:
                    return decision

    return None


def _classify_unenumerated_write(command: str, cwd: str, worktree_root: str) -> Optional[Decision]:
    """Entry point for the deny-by-default path scan. See `_scan_command_segments`."""
    try:
        worktree_resolved = Path(worktree_root).resolve()
    except Exception:
        worktree_resolved = Path(worktree_root)
    return _scan_command_segments(command, cwd, worktree_resolved)


def _all_path_operands(command: str) -> list[str]:
    """Return every path-shaped token anywhere in *command* worth checking.

    Structural, verb-agnostic scan (SEC-6 / D#1672 round 4, broadened for path
    spelling in SEC-8 / D#1672 round 5). `_absolute_path_targets()` only
    inspects output-redirect targets and the destination argument of a fixed
    verb list (`_PATH_WRITE_COMMANDS` — tee/cp/mv/install/rsync), so it only
    guards dial-protected files against being *written*. It has no opinion on
    `rm`, `unlink`, `shred`, `mv <marker> <dst>`'s SOURCE argument, or any
    other deletion form — Kai's round-3 review reproduced the original SEC-2
    bypass end-to-end through exactly that gap: `rm -f <store> <marker>` was
    ALLOWED and dropped both files.

    Rather than enumerating deletion verbs (which just moves the goalpost to
    the next one — `unlink`, `shred`, `find -delete`, ...), this walks every
    shell token in the command and every output-redirect target. Round 4
    filtered this list down to `os.path.isabs()`-shaped tokens only, which
    Kai's round-4 review showed was itself a verb-shaped gap one level down:
    `rm ~/<state>/<marker>` (shlex never expands `~`), `cd <state> && rm
    <marker>` (a bare relative token), and `rm ../../../<state>/<marker>`
    (dotdot-relative) all never reached the check because none of them are
    `os.path.isabs()`. Every token is now returned regardless of shape; the
    caller matches on basename via `_protected_basename_operand()`, an exact
    match (SEC-11 / D#1672 round 6 reverted an intermediate glob-aware
    version — see that function's docstring).

    Known, accepted limitation (documented here per Kai's review, not fixed
    this round): a token that is itself shell/Python code rather than a
    literal path — `python3 -c "os.unlink('/path')"`, a variable-assembled
    path built with `$VAR`, `find <dir> -name <pattern> -delete`, or a glob
    or brace-expansion spelling of the protected basename (`*.initialized`,
    `{d,}`) — is invisible to a lexical token scan. Closing that durably
    requires moving the "store was initialized" fact out of the filesystem
    entirely (a row in the append-only audit trail or state.db) so the
    property stops depending on filesystem ACLs the hook only approximates.
    See the code comment on `_DIAL_PROTECTED_SUFFIXES` below.
    """
    paths: list[str] = []

    # Redirect targets — same extraction as _absolute_path_targets. The
    # regex itself only matches targets beginning with `/`, so this stays
    # absolute-only; that's a narrower surface than the deletion forms SEC-8
    # is about, and out of scope for this round.
    for match in _REDIRECT_PATTERN.finditer(command):
        candidate = match.group(1)
        if not _is_kernel_device(candidate):
            paths.append(candidate)

    # Every token in the command, not just specific commands' destination
    # args, and not filtered to absolute-shaped tokens (SEC-8) — a relative,
    # `~`-prefixed, or dotdot-relative token is just as real an operand.
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            continue
        if os.path.isabs(tok) and _is_kernel_device(tok):
            continue
        paths.append(tok)

    return paths


def _is_gh_merge(command: str) -> bool:
    """Return True if the command attempts to merge a PR via gh."""
    # gh pr merge ...
    if re.search(r"\bgh\s+pr\s+merge\b", command):
        return True
    # gh api graphql ... mergePullRequest
    if re.search(r"\bgh\s+api\b", command) and _GRAPHQL_MERGE_PATTERN.search(command):
        return True
    # gh api -X PUT .../pulls/<n>/merge
    if _REST_MERGE_PATTERN.search(command):
        return True
    # curl / direct PUT to merge endpoint (belt-and-suspenders)
    if re.search(r"/pulls/\d+/merge\b", command) and re.search(
        r"\b(?:PUT|PATCH|-X\s+PUT)\b", command
    ):
        return True
    return False


# gh api mutation detection (Step 2) — command-position aware (D#2225).
#
# The original implementation matched the raw substring `gh api ... -X
# PATCH|POST|PUT|DELETE` anywhere in the command string, including inside an
# inert `echo`/quoted argument that never executes `gh api` at all — an
# executor verifying PR #2224 hit exactly this on a harmless string-dispatch
# echo. Per CLAUDE.md's scoring rule (over-blocking is worse than
# under-blocking for hooks/), round 1 fixed the over-block by scoping the
# check to `gh api` at a real `_command_positions` command position.
#
# Round 2 (code review): round 1's position check used only the PLAIN
# separator set (;, &&, ||, |, &), so it missed six ordinary invocation
# shapes that a real mutation executes through just as much as a bare `gh
# api -X PATCH` does: the `command` builtin, backtick and `$()` command
# substitution, a `(...)` subshell, a `{ ...; }` brace group, and
# `bash`/`sh`/`zsh -c '...'`. Every one of those is already handled
# SOMEWHERE in this module — `is_real_git_rm_invocation` proved out the
# subshell/brace-group separator extension, `check_claude_spawn` proved out
# the `command`-builtin skip and the substitution/`-c` recursion — so this
# generalises `_command_positions` (extra_separators / skip_command_builtin,
# both opt-in and off by default for every existing caller) and adds ONE
# new keyword-agnostic walker, `_find_real_command_segments`, instead of
# forking a third bespoke copy of hardening that already exists twice.

_MUTATING_HTTP_METHODS = frozenset(["POST", "PATCH", "PUT", "DELETE"])

# Subshell `(...)` and brace-group `{ ...; }` delimiters — proven by
# `_GIT_RM_WALK_SEPARATORS` to be safe additions to the separator set (a
# quoted string containing these characters still folds into one shlex
# token, so this does not reopen the quoted-mention false-positive guard;
# see test_git_rm_in_gh_body_double_quotes and its siblings).
_SUBSHELL_BRACE_SEPARATORS: frozenset[str] = frozenset(["(", ")", "{", "}"])

# `$(...)` command substitution body — one level of nested parens tolerated
# (`$(echo $(date))`), matching what a real shell would need to balance.
_DOLLAR_PAREN_BODY_RE = re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)")

# Backtick command substitution body: `` `...` ``.
_BACKTICK_BODY_RE = re.compile(r"`([^`]*)`")

# Shells whose `-c` flag hands the rest of the command a single opaque
# script string worth recursing into. Deliberately excludes python/python3
# (same reasoning `_DASH_C_SHELLS`'s docstring gives for check_claude_spawn:
# a python -c payload is Python syntax, not shell syntax, so re-parsing it
# as a shell command would misread ordinary Python source as invocations).
_SHELL_DASH_C_INTERPRETERS: frozenset[str] = frozenset(["bash", "sh", "zsh"])


def _is_gh_token(tok: str) -> bool:
    """Return True if *tok* is a gh executable reference: exactly "gh" or ending in "/gh".

    Mirrors `_is_git_token` above.
    """
    return tok == "gh" or tok.endswith("/gh")


def _find_real_command_segments(
    command: str, is_target_token: Callable[[str], bool]
) -> list[tuple[list[str], int, int]]:
    """Return (tokens, start, end) for every REAL command-invocation position
    in *command* whose executable token satisfies *is_target_token* — not
    merely a mention of matching text inside a quoted string.

    `tokens` is that fragment's OWN token list (a subshell, substitution, or
    `-c` payload is tokenised independently, matching what a real shell
    actually does); `start` is the executable's index in `tokens`; `end` is
    one past the last token of that pipeline stage — the slice a caller
    should scan for the invocation's own flags.

    Generalises three techniques that already exist in this module, so nothing
    here is a rewrite of logic invented elsewhere:
      - `_command_positions` with the plain separator set finds top-level and
        `;`/`&&`/`||`/`|`/`&`-chained positions (existing, unchanged default).
      - `extra_separators=_SUBSHELL_BRACE_SEPARATORS` extends that same walker
        to also treat `(`, `)`, `{`, `}` as separators — the technique
        `is_real_git_rm_invocation` already proved catches `(cmd)` subshells
        and `{ cmd; }` brace groups without breaking quoted mentions.
      - `skip_command_builtin=True` skips the `command` builtin the way
        `check_claude_spawn`'s dedicated `command claude` check does, so the
        REAL executable one token later is what gets tested.
    Two recursions handle the shapes position-scanning alone cannot reach:
      - `$(...)`/backtick substitution bodies are extracted and walked as
        their OWN command string (same idea as `check_claude_spawn`'s
        `_substitution_is_claude_spawn`, generalised past a hardcoded
        "claude" keyword filter).
      - `bash`/`sh`/`zsh -c '<payload>'` hands its payload to the same
        function recursively (same idea as `check_claude_spawn` step 3,
        extended to `zsh` since an alternate shell's `-c` is not an exotic
        spelling — it is an ordinary way people invoke things).
    Recursion always operates on a strictly shorter substring (the matched
    group excludes its own wrapping delimiters), so it terminates.
    """
    results: list[tuple[list[str], int, int]] = []

    normalised = re.sub(r"&&", " && ", command)
    normalised = re.sub(r"\|\|", " || ", normalised)
    normalised = re.sub(r"[;(){}|]", lambda m: f" {m.group()} ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()
    try:
        tokens = shlex.split(normalised)
    except ValueError:
        tokens = normalised.split()

    positions = _command_positions(
        tokens, extra_separators=_SUBSHELL_BRACE_SEPARATORS, skip_command_builtin=True
    )
    for idx, pos in enumerate(positions):
        if is_target_token(tokens[pos]):
            end = positions[idx + 1] if idx + 1 < len(positions) else len(tokens)
            results.append((tokens, pos, end))

    for match in _DOLLAR_PAREN_BODY_RE.finditer(command):
        results.extend(_find_real_command_segments(match.group(1), is_target_token))
    for match in _BACKTICK_BODY_RE.finditer(command):
        results.extend(_find_real_command_segments(match.group(1), is_target_token))

    try:
        orig_tokens = shlex.split(command)
    except ValueError:
        orig_tokens = []
    for idx, tok in enumerate(orig_tokens):
        shell_name = tok.rsplit("/", 1)[-1]
        if (
            shell_name in _SHELL_DASH_C_INTERPRETERS
            and idx + 1 < len(orig_tokens)
            and orig_tokens[idx + 1] == "-c"
            and idx + 2 < len(orig_tokens)
        ):
            results.extend(
                _find_real_command_segments(orig_tokens[idx + 2], is_target_token)
            )

    return results


def _gh_api_command_segments(command: str) -> list[tuple[list[str], int, int]]:
    """Return (tokens, start, end) for every real `gh api ...` invocation in
    *command*, across every shape `_find_real_command_segments` covers.

    A `gh` token not immediately followed by `api` (e.g. `gh pr create`) is
    not a `gh api` invocation and is skipped.
    """
    segments: list[tuple[list[str], int, int]] = []
    for tokens, pos, end in _find_real_command_segments(command, _is_gh_token):
        if pos + 1 < end and tokens[pos + 1] == "api":
            segments.append((tokens, pos, end))
    return segments


def _segment_has_mutating_method_flag(tokens: list[str], start: int, end: int) -> bool:
    """Return True if the `-X`/`--method` flag inside tokens[start:end] names
    a mutating HTTP verb (POST/PATCH/PUT/DELETE).

    Matches both the space-separated form (`-X PATCH`, `--method PATCH`) and
    the `=`-joined long form (`--method=PATCH`) — the same coverage the
    original regex had, just scoped to one already-verified `gh api`
    invocation instead of the whole raw command string.
    """
    i = start
    while i < end:
        tok = tokens[i]
        if tok in ("-X", "--method"):
            if i + 1 < end and tokens[i + 1].upper() in _MUTATING_HTTP_METHODS:
                return True
        elif tok.startswith("--method="):
            if tok[len("--method="):].upper() in _MUTATING_HTTP_METHODS:
                return True
        i += 1
    return False


def _gh_api_mutation_method_in_command_position(command: str) -> bool:
    """Return True if a REAL `gh api` invocation (at a command position, not
    merely mentioned as text — see `_find_real_command_segments` for the full
    list of shapes covered) uses -X/--method with a mutating HTTP verb.
    """
    for tokens, start, end in _gh_api_command_segments(command):
        if _segment_has_mutating_method_flag(tokens, start, end):
            return True
    return False


# Matches `gh api graphql` calls that contain the literal word `mutation` anywhere
# in the command string (query body, --field, or -f argument).
# `gh api graphql` always POSTs — there is no read-only graphql method flag.
# A graphql `query` operation is always GET-equivalent and is explicitly allowed
# (test_gh_api_graphql_list_allowed verifies this).
_GH_API_GRAPHQL_MUTATION = re.compile(
    r"\bgh\s+api\s+graphql\b.*\bmutation\b",
    re.IGNORECASE | re.DOTALL,
)

# Common gh CLI mutation aliases that don't go through `gh api`.
_GH_MUTATION_ALIASES = re.compile(
    r"\bgh\s+(?:"
    r"pr\s+merge"
    r"|issue\s+close"
    r"|issue\s+delete"
    r"|pr\s+close"
    r"|pr\s+review\s+.*--request-changes"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)

# Matches ALL -f/-F/--field/--raw-field query=<value> parameters in a gh api graphql call.
# Used to extract every query body so we can check each one — not just the first.
# The gh CLI uses last-wins for duplicate -f flags, so ALL must be checked to catch
# the bypass pattern: -f query='allowlisted' -f query='blocked'.
# Captures the value after `query=` up to the next whitespace boundary.
# NOTE: This regex is applied to the raw command string, so values may include shell
# quoting that we strip in _extract_all_gh_query_values().
_GH_QUERY_PARAM_RE = re.compile(
    r"""(?:^|\s)(?:-f|-F|--field|--raw-field)\s+query=(\S+)""",
    re.MULTILINE,
)

# Matches a @file reference (fail-closed — we cannot inspect file contents).
_GH_QUERY_FILE_REF_RE = re.compile(r"^@")

# Strips GraphQL line comments (# to end of line) before mutation name extraction.
# GraphQL spec: comments are `#` to end-of-line, treated as whitespace.
# Our walker could accidentally count `# mutation { closeDiscussion }` as a mutation
# name if we don't strip comments first.
_GRAPHQL_COMMENT_RE = re.compile(r"#[^\n]*")

# ---------------------------------------------------------------------------
# GraphQL mutation allowlist (D#1148)
# ---------------------------------------------------------------------------
# Hardcoded — not configurable from outside code so sub-agents cannot tamper.
# Only top-level mutation field names that are safe for PM/reviewer roles:
#   addDiscussionComment  — PM Spec posts, reviewer status lines
#   updateDiscussion      — STATUS: line updates in Discussion body
#   addLabelsToLabelable  — reviewer label flips (code-review-passed, etc.)
#   removeLabelsFromLabelable — label cleanup
_GH_API_GRAPHQL_MUTATION_ALLOWLIST: frozenset[str] = frozenset(
    [
        "addDiscussionComment",
        "updateDiscussion",
        "addLabelsToLabelable",
        "removeLabelsFromLabelable",
    ]
)

# Finds the start of a mutation block in the command string.
# Matches all four mutation declaration forms:
#   mutation { ... }                    bare
#   mutation Name { ... }               named, no params
#   mutation($v: T, ...) { ... }        anonymous parametrized
#   mutation Name($v: T) { ... }        named parametrized
# Whitespace-tolerant, including multi-line variable lists.
_GRAPHQL_MUTATION_START_RE = re.compile(
    r"\bmutation\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?(?:\([^)]*\))?\s*\{",
    re.IGNORECASE | re.DOTALL,
)

# Matches an identifier followed immediately by `(` — a top-level field call.
_GRAPHQL_FIELD_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _strip_graphql_comments(query: str) -> str:
    """Strip GraphQL line comments (# to end-of-line) from a query string.

    GraphQL spec treats `#` to end-of-line as whitespace.  Our mutation walker
    must not count commented-out mutation names as real operations.
    """
    return _GRAPHQL_COMMENT_RE.sub("", query)


def _extract_all_gh_query_values(command: str) -> list[str]:
    """Return all -f/-F/--field/--raw-field query=<value> strings from a gh api command.

    Uses shlex.split to correctly handle shell-quoted values (including multi-word
    GraphQL bodies enclosed in single or double quotes).

    Strategy:
      1. shlex.split to tokenize the command respecting shell quoting.
      2. Walk the resulting tokens looking for -f/-F/--field/--raw-field flags.
      3. The next token is the key=value pair (e.g. "query=mutation { ... }").
         shlex already stripped outer quotes, so we just split on the first `=`.

    Falls back to the regex-based extractor (_GH_QUERY_PARAM_RE) if shlex.split fails
    (untokenisable command), but that path only captures unquoted single-word values.

    Returns a list of query body strings (may be empty if none found).
    """
    _QUERY_FLAGS = frozenset(["-f", "-F", "--field", "--raw-field"])

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Untokenisable — fall back to regex (captures unquoted values only)
        values: list[str] = []
        for m in _GH_QUERY_PARAM_RE.finditer(command):
            raw = m.group(1)
            if (raw.startswith("'") and raw.endswith("'")) or (
                raw.startswith('"') and raw.endswith('"')
            ):
                raw = raw[1:-1]
            values.append(raw)
        return values

    values = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _QUERY_FLAGS and i + 1 < len(tokens):
            kv = tokens[i + 1]
            if kv.startswith("query="):
                values.append(kv[len("query="):])
            i += 2
            continue
        # Handle --field=query=... or -f=query=... (equals-separated form)
        for flag in ("-f=", "-F=", "--field=", "--raw-field="):
            if tok.startswith(flag):
                rest = tok[len(flag):]
                if rest.startswith("query="):
                    values.append(rest[len("query="):])
                break
        i += 1
    return values


def _extract_graphql_mutation_names(command: str) -> Optional[list[str]]:
    """Extract top-level mutation field names from a GraphQL mutation command string.

    Returns a list of top-level field names (identifiers immediately before `(`) that
    appear at depth 1 inside the outermost mutation block.  Returns None if the
    structure cannot be parsed (caller must block, fail-closed).

    Algorithm: find the opening `{` of the mutation block, then walk the remaining
    characters counting brace depth.  At depth 1 (directly inside the outer block),
    collect every `identifier(` — these are the top-level mutation fields.  Nested
    sub-selections (depth > 1) are ignored.

    GraphQL comments (`# ... newline`) are stripped before walking so that commented-out
    mutation names do not appear in the extracted list.
    """
    # Strip GraphQL comments before searching for mutation names.
    cleaned = _strip_graphql_comments(command)

    start_match = _GRAPHQL_MUTATION_START_RE.search(cleaned)
    if not start_match:
        return None

    # pos is the index of the `{` that opens the mutation block.
    pos = start_match.end() - 1  # last char of the match is `{`
    text = cleaned[pos:]  # slice from the opening brace onward

    # Walk through the text keeping a brace depth counter.
    # Collect identifier( tokens at depth == 1.
    names: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth <= 0:
                break  # end of outer mutation block
            i += 1
        elif depth == 1:
            # At top level of the mutation block — check for `identifier(`
            m = _GRAPHQL_FIELD_CALL_RE.match(text, i)
            if m:
                names.append(m.group(1))
                i = m.end()
            else:
                i += 1
        else:
            i += 1

    return names if names else None


# ---------------------------------------------------------------------------
# Actions-variable mutation deny (D#1944 SEC-7)
# ---------------------------------------------------------------------------
# CI_DISABLED is a repository Actions variable that governs whether ANY PR is
# machine-verified before it merges. It is settable by the operating token
# (admin/maintain), so a sub-agent could flip the whole merge gate off from a
# worktree. `-X PATCH` is already caught by _gh_api_mutation_method_in_command_position below,
# but two spellings are not:
#
#   gh api repos/O/R/actions/variables -f name=CI_DISABLED -f value=true
#       — `gh api` switches to POST implicitly when any -f/-F field is given,
#         with no -X flag for the method regex to see.
#   gh variable set CI_DISABLED --body true
#       — a gh alias, not `gh api` at all.
#
# Reads stay allowed: the CI gate itself reads this variable on every merge
# (scripts/lib/ci-status-check.sh), and over-blocking that would break the
# thing this rule exists to protect.
_ACTIONS_VARIABLES_PATH = re.compile(r"/actions/variables\b", re.IGNORECASE)

_GH_VARIABLE_ALIAS_MUTATION = re.compile(
    r"\bgh\s+variable\s+(?:set|delete|remove)\b",
    re.IGNORECASE,
)

_HTTP_MUTATION_METHOD = re.compile(
    r"-X\s+(?:POST|PATCH|PUT|DELETE)\b|--method\s+(?:POST|PATCH|PUT|DELETE)\b",
    re.IGNORECASE,
)

# gh api sends POST rather than GET as soon as any field flag is present.
_GH_API_IMPLICIT_POST = re.compile(
    r"\bgh\s+api\b.*(?:\s-f\s|\s-F\s|\s--field\s|\s--raw-field\s|\s--input\s)",
    re.IGNORECASE | re.DOTALL,
)


def _is_actions_variable_mutation(command: str) -> bool:
    """Return True if *command* attempts to WRITE a repository Actions variable.

    Reads (`gh api repos/O/R/actions/variables`, `gh variable list|get`) are
    not matched — only writes.
    """
    if _GH_VARIABLE_ALIAS_MUTATION.search(command):
        return True
    if not _ACTIONS_VARIABLES_PATH.search(command):
        return False
    if _HTTP_MUTATION_METHOD.search(command):
        return True
    # Implicit-POST only counts for a real `gh api` REST call. `gh api graphql
    # -f query=...` always carries -f and would otherwise match on nothing more
    # than the path string appearing inside a query body.
    if re.search(r"\bgh\s+api\s+graphql\b", command, re.IGNORECASE):
        return False
    return bool(_GH_API_IMPLICIT_POST.search(command))


def _is_gh_api_mutation(command: str) -> bool:
    """Return True if *command* contains a gh api call with a mutating HTTP method,
    a graphql mutation operation, or a common gh mutation alias.

    Does NOT match read-only calls (GET, HEAD, default gh api GET, or graphql queries).
    Merges are already caught by _is_gh_merge(); this function adds the broader
    POST/PATCH/DELETE and graphql-mutation coverage.

    Multi-query bypass protection (CWE-20):
    The gh CLI accepts multiple -f/-F/--field/--raw-field flags; the LAST query= value
    wins when duplicates exist.  A naive check that only inspects the first mutation block
    can be bypassed with:
        -f query='mutation { addDiscussionComment(...) }' \
        -f query='mutation { closeDiscussion(...) }'
    To close this, we extract ALL query= values and check EACH independently.
    The command is blocked if ANY single query contains a non-allowlisted mutation,
    OR if any query value is a @file reference (fail-closed — we cannot read the file).

    GraphQL comment stripping:
    Comments (`# ... newline`) are stripped before walking so that a commented-out
    mutation name like `# mutation { closeDiscussion }` is not mis-classified as real.
    """
    if _gh_api_mutation_method_in_command_position(command):
        return True

    if re.search(r"\bgh\s+api\s+graphql\b", command, re.IGNORECASE):
        # Extract every -f/-F/--field/--raw-field query= value from the command.
        # We check ALL query values for @file references and non-allowlisted mutations.
        query_values = _extract_all_gh_query_values(command)

        # Fail-closed: any @file reference in any query param blocks the whole call.
        # We cannot inspect the file contents so we must block unconditionally.
        for value in query_values:
            if _GH_QUERY_FILE_REF_RE.match(value):
                return True

        # If no mutation keyword appears anywhere in the command, it's a safe query.
        if not _GH_API_GRAPHQL_MUTATION.search(command):
            return False

        # Mutation keyword is present — inspect each query value.
        if not query_values:
            # The mutation keyword appeared somewhere but we couldn't extract any
            # query= parameter — fall back to the single-block extractor for
            # inline query bodies (e.g. --field query=mutation{...} without quotes).
            names = _extract_graphql_mutation_names(command)
            if names is not None and all(
                n in _GH_API_GRAPHQL_MUTATION_ALLOWLIST for n in names
            ):
                return False
            return True

        # Check each query value independently.
        for value in query_values:
            # Only check query values that contain a mutation keyword.
            # (A query= value may be a read-only `query { ... }` — allow those.)
            cleaned = _strip_graphql_comments(value)
            if not re.search(r"\bmutation\b", cleaned, re.IGNORECASE):
                continue

            # Extract mutation names from this individual query value.
            names = _extract_graphql_mutation_names(value)
            if names is None:
                # Cannot parse — block fail-closed.
                return True
            if not all(n in _GH_API_GRAPHQL_MUTATION_ALLOWLIST for n in names):
                # At least one non-allowlisted mutation in this query — block.
                return True

        # All query values passed — none contained a non-allowlisted mutation.
        return False

    if _GH_MUTATION_ALIASES.search(command):
        return True
    return False


# ---------------------------------------------------------------------------
# Public classifiers
# ---------------------------------------------------------------------------


def classify_agent_spawn(cwd: str, args: dict) -> Decision:
    """Classify an Agent() tool call from *cwd*.

    Sub-agents running in worktrees or untrusted paths must not spawn further
    agents — doing so would bypass the single-spawner invariant and allow
    recursive privilege escalation.

    Only the Team Lead context (classify_cwd == "team_lead") may spawn agents.

    Parameters
    ----------
    cwd:
        Working directory of the agent attempting the spawn.
    args:
        The tool_input dict from the Agent() call (for audit logging — not used
        in the allow/deny decision itself).

    Returns
    -------
    Decision(allow=False, reason="agent_spawn_in_worktree") if cwd is a worktree.
    Decision(allow=False, reason="agent_spawn_in_untrusted_cwd") if cwd is untrusted.
    Decision(allow=True) for team_lead cwd.
    """
    tier = classify_cwd(cwd)
    if tier == "team_lead":
        return Decision(allow=True, reason="")
    if tier == "worktree":
        return Decision(allow=False, reason="agent_spawn_in_worktree")
    # untrusted
    return Decision(allow=False, reason="agent_spawn_in_untrusted_cwd")


def classify_git_rm(command: str) -> Decision:
    """Classify any command that invokes `git rm`.

    Returns Decision(allow=False, ...) when the command contains a real `git rm`
    invocation targeting project files, which violates the archive protocol.
    Returns Decision(allow=True) when the command does not contain a real `git rm`
    invocation.

    This classifier is context-free (no CWD needed) and runs before classify_bash
    so that the block message cites the archive protocol rather than a generic reason.

    False-positive guards:
      - `git rmiscellaneous-thing` — no match (word boundary after `rm`)
      - `mygit rm foo` — no match (requires `git` preceded only by whitespace or start-of-string)
      - `gh issue comment --body "never git rm files"` — no match (appears only inside
        a quoted argument; is_real_git_rm_invocation() uses tokenisation to detect this)

    `git rm --cached <path>` is exempt (opt-in via exempt_cached=True): it only
    drops the index entry and leaves the working-tree file in place, so it
    cannot violate the archive protocol, which protects working-tree presence.
    """
    if is_real_git_rm_invocation(command, exempt_cached=True):
        return Decision(
            allow=False,
            reason=(
                "git rm violates archive protocol — use "
                "`git mv path archive/<name>-<date>/` instead. "
                "See CLAUDE.md Archive Protocol."
            ),
        )
    return Decision(allow=True, reason="")


def classify_bash(command: str, cwd: str) -> Decision:
    """Classify a Bash tool call from *cwd*.

    Returns Decision(allow=False, reason=...) to block, Decision(allow=True, reason="") to pass.
    Caller is responsible for checking is_worktree(cwd) first.
    """
    # 1a. git rm check — archive protocol violation, applies regardless of worktree status.
    git_rm_decision = classify_git_rm(command)
    if not git_rm_decision.allow:
        return git_rm_decision

    # 1b. Merge check — applies regardless of worktree status (belt-and-suspenders,
    #    but caller only invokes us when in a worktree).
    if _is_gh_merge(command):
        return Decision(allow=False, reason="sub-agents may not merge")

    # 1c. gh api mutation check — blocks POST/PATCH/PUT/DELETE and common mutation
    #     aliases from any non-team-lead context.  Applies to worktree AND untrusted CWDs.
    #     (classify_bash is only called when is_worktree is truthy or from tests; the
    #     sandbox.py caller also invokes it for untrusted paths via the main block.)
    # 1c-i. Actions-variable write check (D#1944 SEC-7) — checked before the
    #     generic gh api mutation rule so the block message names the CI kill
    #     switch rather than reading as a generic API-mutation refusal. This
    #     also covers the two spellings the generic rule misses: gh api's
    #     implicit POST (any -f field, no -X flag) and `gh variable set`.
    if _is_actions_variable_mutation(command):
        return Decision(
            allow=False,
            reason=(
                "sandbox_block_actions_variable_write: repository Actions "
                "variables (CI_DISABLED is the CI merge gate's kill switch) "
                "may not be changed from a sub-agent worktree — use "
                "scripts/set-ci-kill-switch.sh from the Team Lead context, "
                "which writes a ci_kill_switch_changed audit row"
            ),
        )

    if _is_gh_api_mutation(command):
        return Decision(
            allow=False,
            reason="sandbox_block_gh_api_mutation: gh api mutation calls are not permitted from a sub-agent worktree",
        )

    # 1d. Dial-protected path operand check (SEC-6 / D#1672 round 4, path
    #     spelling broadened in SEC-8 / D#1672 round 5, glob over-block
    #     reverted in SEC-11 / D#1672 round 6) — structural scan across EVERY
    #     token in the command (absolute, relative, `~`-prefixed, or
    #     dotdot-relative), not just redirect/copy destinations. Closes the
    #     deletion vector `rm`, `unlink`, and `mv <marker> <dst>`'s source
    #     argument left open: those verbs are not in _PATH_WRITE_COMMANDS and
    #     produce no "target" for step 4 below to see, so `rm -f <store>
    #     <marker>` was previously ALLOWED and reproduced the original SEC-2
    #     bypass verbatim. Round 4 filtered tokens to `os.path.isabs()` first,
    #     which meant `rm ~/<state>/<marker>`, `cd <state> && rm <marker>`,
    #     and `rm ../../../<state>/<marker>` all bypassed it — the same
    #     bypass, a different spelling. `_protected_basename_operand()` drops
    #     that gate so every token reaches an exact basename match, regardless
    #     of whether it looks absolute. Runs unconditionally (not gated on
    #     cwd or verb), so it also covers reads of these files, not just
    #     writes/deletes — hence "access blocked" below rather than "write
    #     blocked" (SEC-9 / D#1672 round 4 non-blocking finding: the old
    #     wording was misleading for e.g. `tail -100 <state>/audit.jsonl`,
    #     which this step also blocks fail-closed).
    for operand in _all_path_operands(command):
        if _is_ephemeral_tmp_path(operand):
            continue
        if _protected_basename_operand(operand):
            return Decision(
                allow=False,
                reason=f"dial-registry access blocked: {Path(operand).name} is read-only for sub-agents",
            )

    # 2. bash -c / sh -c wrapping a write-verb
    wrapped = _is_bash_wrapping_git_write(command, cwd)
    if wrapped is not None:
        return Decision(allow=False, reason=wrapped)

    # 3. Git verb analysis — walk EVERY git invocation in the command, not just the
    # first, and pair each one with ITS OWN effective CWD (D#1746 property 4). A
    # read-only verb (e.g. `log`, `status`) must not shield a write or always-blocked
    # verb that follows it in the same command — first-verb-wins was a live escape:
    # `git log;git reset --hard origin/main` and `git status && git worktree remove
    # <path>` both ALLOWed on main today because the walker returned as soon as it
    # saw the leading read-only verb and never inspected the rest of the command
    # (D#1729 F3). Per-invocation pairing (rather than one CWD for the whole
    # command) is what lets `git -C <main-repo> log && git commit -m wip` keep
    # ALLOWing: the escaping CWD belongs to the read-only `log` invocation, not to
    # `commit`, and only a (write-verb AND escaping-cwd) pairing on the SAME
    # invocation blocks (see `_walk_git_invocations`'s docstring for the over-block
    # this replaces).
    git_tokens = _tokenize_shell_command(command)
    git_invocations, _final_cwd = _walk_git_invocations(git_tokens, cwd)
    verbs = [verb for verb, _invocation_cwd, _args in git_invocations]
    if verbs:
        # Always-blocked verbs win regardless of position or effective CWD — these
        # can corrupt the parent repo's branch state even when run from within the
        # worktree, and even when a read-only verb precedes them in the command.
        # D#2058: membership alone is no longer sufficient — a read-only spelling
        # of `branch`/`worktree` (or `--help` on any of the seven) escapes this,
        # per-invocation, via _is_git_readonly_invocation. Every invocation whose
        # verb is always-blocked must be a read-only spelling for this rule to
        # NOT block — one write spelling among several always-blocked-verb
        # invocations still blocks the whole command (mirrors the pre-existing
        # "every invocation is examined, not just the first" property below).
        for verb, _invocation_cwd, args in git_invocations:
            if verb in _GIT_ALWAYS_BLOCKED_VERBS and not _is_git_readonly_invocation(verb, args):
                return Decision(allow=False, reason="git write-verb outside worktree")
        # CWD-dependent write verbs (commit, push, merge, etc.): allowed only when
        # THAT invocation's own effective CWD stays within the worktree.
        worktree_root = _worktree_root_from_cwd(cwd)
        try:
            worktree_resolved = Path(worktree_root).resolve()
        except Exception:
            worktree_resolved = Path(worktree_root)
        for verb, invocation_cwd, _args in git_invocations:
            if verb not in _GIT_WRITE_VERBS:
                continue
            try:
                escapes = not Path(invocation_cwd).resolve().is_relative_to(worktree_resolved)
            except Exception:
                # Fail closed on a CWD we can't resolve rather than silently allow.
                escapes = True
            if escapes:
                return Decision(
                    allow=False,
                    reason="git write-verb outside worktree",
                )
            # This invocation's effective CWD is within the worktree — the git
            # operation itself is permitted. Deliberately NOT `return
            # Decision(allow=True, ...)` here (D#1756/D#1903): this used to
            # short-circuit the whole classification, which meant a write chained
            # onto the same command line after the git invocation (`git commit -m
            # x && rm -rf <main-repo>/scripts`) was never examined by the
            # redirect/write-target scans below. Keep checking the remaining
            # invocations, then fall through.
        # Every verb found is read-only/unknown, or (per the loop above) a write
        # verb whose OWN effective CWD stayed inside the worktree. The git
        # invocation(s) in this command are therefore fully vetted — but
        # D#1756/D#1903 found this branch also used to `return Decision(allow=True,
        # ...)` unconditionally, for the same reason: it shielded anything else
        # chained onto the command line (a redirect, or a non-git write after
        # `&&`/`;`) from ever reaching steps 4/4a/4b. Fall through instead.
        # `_is_segment_write_candidate` treats a `git` segment as never a write
        # candidate on its own (see its docstring) — this git invocation was just
        # vetted above — so a bare `-C <path>` operand isn't misread as a write
        # target by the unenumerated path-token scan (the regression this Spec's
        # "regression trap" warns about: `git -C <main-repo> log --oneline -5` and
        # `git -C <main-repo> status` must keep allowing).

    # 4. Output redirects / write-commands targeting absolute paths outside worktree
    worktree_root = _worktree_root_from_cwd(cwd)

    # 4a. `cd <target> && <relative write>` (D#1898) — a spelling where the
    # write's own path string never becomes a candidate for the loop below,
    # because it's relative and only escapes via the directory `cd` left us
    # in (e.g. `cd ~ && echo x >> .bashrc`). Shares _is_ephemeral_tmp_path()
    # with the loop below so the /tmp exemption can't drift between the two
    # (D#1898 round 2, B2).
    cd_escape_reason = _cd_escape_relative_write(command, cwd, worktree_root)
    if cd_escape_reason is not None:
        return Decision(allow=False, reason=cd_escape_reason)

    for target in _absolute_path_targets(command):
        # /tmp/ and /var/tmp/ are ephemeral filesystem — not repo state, always safe.
        if _is_ephemeral_tmp_path(target):
            continue
        # Dial-registry files are protected regardless of location.
        if _is_dial_protected_path(target):
            return Decision(
                allow=False,
                reason=f"dial-registry write blocked: {Path(target).name} is read-only for sub-agents",
            )
        if not Path(target).is_relative_to(Path(worktree_root).resolve()):
            return Decision(allow=False, reason="output redirect outside worktree")

    # 4b. Deny-by-default path scan for writers step 4 above doesn't enumerate —
    # unknown programs, `sed -i`, `python3 -c "...write..."` (D#1749). See the
    # module note above _classify_unenumerated_write for the full rationale,
    # including the two documented false-positive-avoidance exceptions and the
    # acknowledged seed.py-style residual risk this does NOT close.
    unenumerated_decision = _classify_unenumerated_write(command, cwd, worktree_root)
    if unenumerated_decision is not None:
        return unenumerated_decision

    return Decision(allow=True, reason="")


def _is_dial_protected_path(file_path: str) -> bool:
    """Return True if *file_path* matches a dial-registry protected filename.

    Sub-agents in worktrees must never write these files — only the Team Lead
    (running outside any worktree) may mutate the dial registry or audit log.

    Checks are suffix-only (basename match) so the function is independent of
    the exact state-dir location, which may vary per environment.
    """
    try:
        name = Path(file_path).name
    except Exception:
        return False
    return any(name == suffix for suffix in _DIAL_PROTECTED_SUFFIXES)


def _protected_basename_operand(tok: str) -> bool:
    """Return True if *tok*'s basename is a dial-protected filename.

    SEC-8 (D#1672 round 5): dropped the round-4 `os.path.isabs(tok)` gate so
    `~`-prefixed, relative, and dotdot-relative spellings of the marker
    (which all bypassed the isabs-gated check) reach this basename match too.
    That part stays.

    SEC-11 (D#1672 round 6, Kai round-5 review): round 5 also glob-matched
    every token's basename against the protected names via `fnmatch`, to
    additionally cover glob spellings of the marker. `*` matches every
    protected name, so it over-blocked any ordinary command with a bare `*`,
    `*.json`, or `*.jsonl` token — `rm -rf *`, `git add *`, `pytest tests/*`,
    and more. Coverage was incomplete anyway (bash brace expansion still
    reaches the file; `fnmatch` doesn't implement `{}`), so it traded a wide
    availability regression for a partial close of an evasion that already
    requires foreknowledge of the control — the same residual-gap class as
    `python3 -c` and `$VAR` below. Reverted to exact match.

    Used only by classify_bash() step 1d's operand scan — classify_path_write()
    and the step 4 write-target check receive resolved paths from the tool
    call, not adversarial shell tokens, so their exact-match
    `_is_dial_protected_path()` is unaffected.
    """
    try:
        name = Path(tok).name
    except Exception:
        return False
    return bool(name) and name in _DIAL_PROTECTED_SUFFIXES


def classify_path_write(file_path: str, cwd: str) -> Decision:
    """Classify an Edit or Write tool call.

    Returns Decision(allow=False, reason="file_path outside worktree") to block,
    Decision(allow=True, reason="") to pass.
    Caller is responsible for checking is_worktree(cwd) first.
    Relative paths are allowed — Claude Code resolves them against cwd (the worktree).
    """
    if not os.path.isabs(file_path):
        # Even for relative paths, check dial-registry protection by name.
        if _is_dial_protected_path(file_path):
            return Decision(
                allow=False,
                reason=f"dial-registry write blocked: {Path(file_path).name} is read-only for sub-agents",
            )
        return Decision(allow=True, reason="")

    # Dial-registry files are protected regardless of their absolute location.
    if _is_dial_protected_path(file_path):
        return Decision(
            allow=False,
            reason=f"dial-registry write blocked: {Path(file_path).name} is read-only for sub-agents",
        )

    # D#2246 item 5: /tmp and /var/tmp are ephemeral, not repo state — the
    # same exemption classify_bash's `sed -i /tmp/x` already gets via
    # `_is_ephemeral_tmp_path`. Before this, `Write /tmp/x` and `sed -i
    # /tmp/x` gave different answers for the identical operation.
    if _is_ephemeral_tmp_path(file_path):
        return Decision(allow=True, reason="")

    worktree_root = _worktree_root_from_cwd(cwd)
    try:
        if Path(file_path).is_relative_to(Path(worktree_root).resolve()):
            return Decision(allow=True, reason="")
    except Exception:
        pass
    return Decision(allow=False, reason="file_path outside worktree")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _worktree_root_from_cwd(cwd: str) -> str:
    """Return the worktree root directory for the agent at *cwd*.

    E.g.:
      <main-repo-root>/.claude/worktrees/abc123/src
      → <main-repo-root>/.claude/worktrees/abc123

    Falls back to cwd itself if no worktree pattern matches (Team Lead path,
    already guarded by callers).
    """
    try:
        resolved = str(Path(cwd).resolve())
    except Exception:
        resolved = cwd

    for prefix in _WORKTREE_PREFIXES:
        if resolved.startswith(prefix):
            rest = resolved[len(prefix):]
            worktree_id = rest.split("/")[0] if rest else ""
            return prefix + worktree_id
    return resolved
