"""global.archive_protocol_honored — count of git rm invocations on tracked files.

Scans transcripts for Bash tool calls that invoke "git rm" on non-archived,
non-.gitignored files.  A count of 0 is healthy; any positive count is drift.

The check is intentionally conservative: it flags "git rm" mentions regardless
of whether the files were actually tracked, because the policy says git rm is
NEVER allowed for project files.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult
from backend.transcript_reader import TRANSCRIPT_GLOB, iter_turns
from hooks.sandbox_rules import is_real_git_rm_invocation

logger = logging.getLogger(__name__)

CLAIM_ID = "global.archive_protocol_honored"
ROLE_SCOPE = "global"

# Heredoc pattern: matches <<'MARKER', << MARKER, <<- MARKER, etc.
# Used to strip heredoc bodies so that "git rm" appearing only inside a heredoc
# body (e.g. a Python test script or a PR-body markdown file being written to
# disk) is not counted as a real invocation.
_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?([A-Za-z_][A-Za-z_0-9]*)['\"]?\s*\n.*?\n\1\b",
    re.DOTALL,
)

# Scratch / gitignored path patterns that are exempt when used with git rm
# (with or without --cached).  These are never project-tracked files:
#   - .pr-body.txt / pr-body.txt / pr-body-NNN.txt  — ephemeral PR-body files
#   - pr_body.txt / pr_body-NNN.txt  — alternate naming used by some scripts
#   - /tmp/...  — anything under /tmp is scratch
#
# The pr-body alternatives use (?:^|/) anchored with re.match (not re.search)
# in _is_scratch_path below.  re.match anchors at the start of the string, so
# "(?:^|/)" matches only at position 0 — meaning the whole path must begin with
# either an optional "/" or be the bare pr-body name itself.  A path like
# docs/pr_body.txt does NOT match because re.match tries position 0, where
# "(?:^|/)" matches "^" (zero-width) but then expects "pr[-_]body" and finds
# "docs" instead — so no match.  A bare "pr-body.txt" matches because "(?:^|/)"
# matches "^" at position 0 and "pr-body" follows immediately.
# The trailing $ ensures pr-body.txt.bak and pr-body/keep.py do not match.
_SCRATCH_PATH_RE = re.compile(
    r"(?:(?:^|/)pr[-_]body(?:-[^/\s]*)?\.txt$"
    r"|(?:^|/)\.pr-body\.txt$"
    r"|^/tmp/"
    r")"
)


def _strip_shell_comments(cmd: str) -> str:
    """Strip shell comments from each line of a command string.

    For each line, everything from the first unquoted '#' onwards is removed.
    This prevents matching "git rm" that appears only inside a comment such as:
        # This is equivalent to git rm
    """
    clean_lines = []
    for line in cmd.splitlines():
        clean_lines.append(line.split("#", 1)[0])
    return "\n".join(clean_lines)


def _executed_command_only(cmd: str) -> str:
    """Return cmd with heredoc bodies stripped and shell comments removed.

    Heredoc bodies are replaced with just the opening marker line, so that
    "git rm" appearing only inside a heredoc (e.g. a script being written to
    /tmp, or a PR body being composed inline) is not counted.

    Shell comments are stripped after heredoc stripping so that multi-line
    commands whose only "git rm" text appears in a comment line are not counted.
    """
    # Strip heredoc bodies first (before comment stripping — marker lines themselves
    # are not comments and must stay so that the surrounding command context is
    # preserved for the tokeniser).
    cmd = _HEREDOC_RE.sub(lambda m: f"<< {m.group(1)}", cmd)
    # Strip shell comments.
    cmd = _strip_shell_comments(cmd)
    return cmd


def _is_scratch_path(path: str) -> bool:
    """Return True if *path* is a known scratch / gitignored file, not a project file.

    Uses re.match (anchored at position 0) — not re.search — so the pr-body
    alternatives in _SCRATCH_PATH_RE only fire when the pr-body name appears at
    the very start of the path (bare filename with no directory component).  A
    path like docs/pr_body.txt does NOT match because re.match at position 0
    sees "docs/" before "pr_body.txt" and the pattern fails.  The ^/tmp/
    alternative is also anchored to position 0 via re.match.

    Examples:
      "pr-body.txt"        → True  (bare basename, no dir — scratch file)
      "docs/pr_body.txt"   → False (has a directory component — real tracked file)
      "/tmp/x/pr-body.txt" → True  (starts with /tmp/ — scratch area)
      "pr-body/keep.py"    → False (pr-body is a directory, keep.py is the file)
    """
    return bool(_SCRATCH_PATH_RE.match(path))


def _is_scratch_only_rm(cmd: str) -> bool:
    """Return True if every `git rm` in *cmd* targets only scratch / gitignored paths.

    Handles:
    - ``git rm --cached .pr-body.txt``  (index-only un-tracking of scratch file)
    - ``git -C <wt> rm pr_body.txt``    (removing a scratch PR body from a worktree)
    - ``git -C <wt> rm --cached pr-body-NNN.txt``
    - Any mix of the above — returns True only if ALL git rm sub-invocations are
      scratch-only.  If any sub-invocation targets a real project file, returns False.

    Global git options (-C, -c, --git-dir, --work-tree) are skipped to reach the
    subcommand, matching the same walk used by is_real_git_rm_invocation().
    """
    # Preserve || and && as distinct tokens before shlex so we can split on them.
    normalised = re.sub(r"&&", " && ", cmd)
    normalised = re.sub(r"\|\|", " || ", normalised)
    normalised = re.sub(r"[;(){}|]", lambda m: f" {m.group()} ", normalised)
    normalised = re.sub(r" +", " ", normalised).strip()
    try:
        tokens = shlex.split(normalised)
    except ValueError:
        return False

    # Tokens that are shell plumbing, not file paths.
    _SHELL_TOKENS = frozenset({"&&", "||", ";", "|", "true", "false", "echo", "||"})
    _REDIRECT_RE = re.compile(r"^\d*(?:>>?|&\d+)")

    def _is_path_token(t: str) -> bool:
        return (
            not t.startswith("-")
            and t not in _SHELL_TOKENS
            and not _REDIRECT_RE.match(t)
        )

    # Split into pipeline stages at && / || / ;
    _STAGE_SEPS = frozenset({"&&", "||", ";"})

    found_scratch = False
    found_real = False

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _STAGE_SEPS or tok == "|":
            i += 1
            continue
        if tok == "git" or (tok.endswith("/git") and "/" in tok):
            # Walk past global git options to reach the subcommand.
            j = i + 1
            while j < len(tokens):
                sub = tokens[j]
                if sub in _STAGE_SEPS or sub == "|":
                    break
                if sub in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                    j += 2
                    continue
                if sub.startswith("--git-dir=") or sub.startswith("--work-tree="):
                    j += 1
                    continue
                if sub.startswith("-"):
                    j += 1
                    continue
                # Found the subcommand.
                if sub == "rm":
                    k = j + 1
                    paths: list[str] = []
                    while k < len(tokens) and tokens[k] not in _STAGE_SEPS and tokens[k] != "|":
                        t = tokens[k]
                        if _is_path_token(t):
                            paths.append(t)
                        k += 1
                    if paths and all(_is_scratch_path(p) for p in paths):
                        found_scratch = True
                    else:
                        found_real = True
                    i = k
                    break
                else:
                    break  # different git subcommand
        i += 1

    # Exempt only if we found at least one scratch-only rm and no real-target rm.
    return found_scratch and not found_real


def _is_tmp_repo_only(cmd: str) -> bool:
    """Return True if any git rm in *cmd* is confined to a /tmp scratch repository.

    Covers the pattern: ``cd /tmp && git init && git rm <file>`` where the
    repository being operated on is a throwaway test repo in /tmp, not the
    project repository.
    """
    cleaned = _executed_command_only(cmd)
    # Quick bail-out: no git rm text remaining after cleanup.
    if not re.search(r"\bgit\b.*\brm\b", cleaned):
        return False
    # A cd /tmp (with no subsequent cd back to a project directory) means the
    # working directory for subsequent git commands is /tmp, making any git rm
    # a scratch-repo operation.
    has_cd_tmp = bool(re.search(r"\bcd\s+/tmp\b", cleaned))
    has_cd_project = bool(re.search(r"\bcd\s+/home/", cleaned))
    return has_cd_tmp and not has_cd_project


def _is_git_rm_violation(cmd: str) -> bool:
    """Return True if *cmd* contains a real git rm invocation on a project file.

    Steps:
    1. Strip heredoc bodies and shell comments to remove false-positive text.
    2. Feed the cleaned command to the authoritative tokeniser
       ``is_real_git_rm_invocation()`` (imported from hooks.sandbox_rules).
    3. If the tokeniser fires, apply narrow exemptions:
       a. All targets are scratch/gitignored paths → exempt.
       b. All git activity is confined to a /tmp scratch repo → exempt.
    """
    cleaned = _executed_command_only(cmd)
    if not is_real_git_rm_invocation(cleaned):
        return False
    # Apply exemptions on the *original* command so that path tokens are intact.
    if _is_scratch_only_rm(cmd) or _is_tmp_repo_only(cmd):
        return False
    return True


def _transcript_git_rm_count(path: Path) -> int:
    """Return the number of Bash calls in this transcript that invoke git rm.

    Only Bash tool inputs are inspected — text turns and non-Bash tool calls
    are ignored.  The check uses the authoritative tokeniser from
    hooks.sandbox_rules to avoid false positives from heredoc bodies, quoted
    commit messages, JSON test payloads, and shell comments.
    """
    count = 0
    try:
        for turn in iter_turns(path):
            for tc in turn.tool_calls:
                if tc.get("name", "") != "Bash":
                    continue
                cmd = tc.get("input", {}).get("command", "")
                if _is_git_rm_violation(cmd):
                    count += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("archive_protocol: error reading %s: %s", path, exc)
    return count


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    **_kwargs: Any,
) -> ClaimResult:
    """Count git rm invocations across all transcripts in the window.

    Parameters
    ----------
    runs:
        All agent_run rows in the window (any role — this is a global claim).
    transcripts_dir:
        Unused — transcripts are discovered via the canonical glob.
    window_days:
        Number of days in the audit window.
    sample_cap:
        Maximum transcripts to examine per claim (default 100).
    """
    since_seconds = window_days * 86400
    now = time.time()
    cutoff = now - since_seconds

    all_paths = sorted(
        p for p in glob.glob(TRANSCRIPT_GLOB)
        if os.path.getmtime(p) >= cutoff
    )[:sample_cap]

    sample_size = len(all_paths)

    if sample_size == 0:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=0,
            score=0,
            score_type="count",
            status="n/a",
            evidence="no transcripts found in window",
        )

    total_violations = 0
    last_violating_path: str = ""
    for p in all_paths:
        n = _transcript_git_rm_count(Path(p))
        if n > 0:
            total_violations += n
            last_violating_path = Path(p).stem

    status = ClaimResult.classify_count(total_violations, sample_size)

    if total_violations == 0:
        evidence = f"0 git rm calls in {sample_size} transcripts"
    else:
        evidence = f"{total_violations} git rm call(s); last in: {last_violating_path}"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=sample_size,
        score=total_violations,
        score_type="count",
        status=status,
        evidence=evidence,
        notes="pass = 0 violations; any positive count = drift",
    )
