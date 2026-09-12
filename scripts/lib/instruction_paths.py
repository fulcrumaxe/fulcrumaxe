#!/usr/bin/env python3
"""scripts/lib/instruction_paths.py — which paths in a PR diff are
instruction-bearing (the kind of path an agent reads as instructions rather
than as plain data: `CLAUDE.md`, `.claude/agents/*.md`, `hooks/`, ...)?

WHY THIS EXISTS
----------------
`scripts/spawn-agent.sh` pins `head.sha` at spawn time and the agent then
works in a checkout of that tree. On the public code plane an outside
contributor controls every byte at that sha — including the files an agent
reads as instructions rather than data. This module is the predicate half of
the answer: "does this PR's diff touch one of those paths?" It has no
opinion on what happens next — see `pr-instruction-path-notice.sh` for the
label + comment, and `pr_intake_gate.py` for the (separate) author/approval
gate this module does not touch or depend on.

SCOPE — detect-and-tell, nothing else
--------------------------------------
This module never blocks a spawn, never reverts a tree, never sanitises
anything at read time. It answers one question and writes nothing to
GitHub. The measured case for stopping at detect-and-tell (0 of the newest
100 code-plane PRs were cross-repository, so a blocking control would fire
only on our own work) lives with the caller, not here — this file only
needs to keep answering the question correctly if that measurement changes.

MATCHING — exact prefix, anchored at the repo root
---------------------------------------------------
No regex, no substring, no glob. A directory entry ends in "/" and matches
any path under it; a file entry matches only that exact path. This is
deliberately narrow: a broad `scripts/lib/` prefix would false-positive on
a PR that touches `scripts/lib/external_intake_gate.py` — security-adjacent
plumbing, not something an agent loads as instructions — and only the exact
prefix predicate keeps that PR quiet (see backend/tests/test_instruction_paths.py's
negative canary).

STDLIB ONLY, NO PROJECT IMPORTS EITHER
---------------------------------------
This file imports nothing outside the Python standard library — not even
`pr_intake_gate` or `backend._repo`, both of which do the closely related
things this file needs (cross-repository detection, code-repo resolution).
That is deliberate, not an oversight: this module is meant to be reusable by
other work (e.g. a ref-trust predicate) with zero coupling back into the PR
intake machinery, and a dependency check on this file specifically asserts
zero non-stdlib imports (see AC-11). The ~15 lines of overlap with
`pr_intake_gate.fetch_pr_meta` below are the cost of that independence.

CLI
---
    python3 scripts/lib/instruction_paths.py list
        prints one instruction-bearing path prefix per line, exit 0.

    python3 scripts/lib/instruction_paths.py check-pr <N> [--repo SLUG] \
        [--cross-repository true|false]
        prints JSON {"pr": N, "repo": ..., "paths_touched": [...],
        "cross_repository": bool} and exits 0. Writes nothing to GitHub.
        --cross-repository lets a caller that already resolved that fact
        (e.g. from a `pr_intake_gate.check_pr` result fetched moments
        earlier in the same loop iteration) pass it straight through
        instead of triggering the extra API call this module would
        otherwise make to get the same fact on its own.

    python3 scripts/lib/instruction_paths.py render-matched-paths
        reads a JSON array of path strings from stdin, prints a fenced,
        defanged block (see `render_matched_paths_for_comment`) safe to
        embed verbatim in a PR comment, and exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Exact-prefix match, anchored at the repo root. Directory entries end in
#: "/"; file entries are exact tracked paths. Covers at minimum the set the
#: consensus panel named: role/agent cards and settings (`.claude/`,
#: `CLAUDE.md`), hooks (`hooks/`), MCP server definitions (`.mcp.json`,
#: worse than prose if injected — tool definitions, not just text), the
#: trust-set input (`.autonomous-team/config.json`), spawn prompt assembly
#: (`backend/spawn_templates/`), CI workflow definitions
#: (`.github/workflows/`), and the working-principles fragment injected into
#: every spawn (`scripts/lib/working-principles.sh`).
#:
#: `.autonomous-team/config.json` is deliberately kept on this list even
#: though the code plane's own `.gitignore` excludes it and a PR diff there
#: can therefore never actually touch it: it is tracked (and
#: instruction-bearing) on the engine/operator checkout, and it is the
#: AC-9 trust-set input (`maintainer_allowlist`) that decides which repo
#: every PR/CI operation targets (`code_repo`) — about as instruction-
#: bearing as a config file gets. The completeness test in
#: backend/tests/test_instruction_paths.py checks `.gitignore` membership
#: explicitly for this entry rather than inferring "not tracked" as "wrong";
#: see that test's docstring for the full reasoning (D#2434 review round 2).
INSTRUCTION_PATH_PREFIXES: tuple[str, ...] = (
    "CLAUDE.md",
    ".claude/",
    "hooks/",
    ".mcp.json",
    ".autonomous-team/config.json",
    "backend/spawn_templates/",
    ".github/workflows/",
    "scripts/lib/working-principles.sh",
)


def is_instruction_path(path: str) -> bool:
    """True when *path* is exactly, or falls under, an instruction-bearing
    prefix. No regex, no substring, no glob — see module docstring."""
    if not path:
        return False
    for prefix in INSTRUCTION_PATH_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix:
            return True
    return False


def _gh(args: list) -> str:
    """Run `gh` and return stdout. Raises on any non-zero exit, so a failed
    read can never be mistaken for an empty result."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def _resolve_code_repo() -> str:
    """The repo that holds this PR — resolved, never hard-coded (AC-10).

    Mirrors `scripts/lib/repo-resolve.sh`'s `_resolve_code_repo()` /
    `backend._repo.CODE_REPO`'s precedence in pure stdlib Python, rather than
    importing either: a bash subprocess call is a heavier and slower way to
    get the same string, and importing `backend._repo` would pull a
    non-stdlib root name into this file's dependency graph (AC-11).
    Precedence: .autonomous-team/config.json "code_repo" -> "repo" ->
    AUTONOMOUS_TEAM_REPO env -> raise (never a fallback slug).
    """
    cfg_path = _REPO_ROOT / ".autonomous-team" / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text())
        except (OSError, ValueError):
            cfg = {}
        if isinstance(cfg, dict):
            for key in ("code_repo", "repo"):
                value = cfg.get(key)
                if isinstance(value, str) and value:
                    return value
    env_repo = os.environ.get("AUTONOMOUS_TEAM_REPO")
    if env_repo:
        return env_repo
    raise RuntimeError(
        "instruction_paths: could not resolve the code repo — set AUTONOMOUS_TEAM_REPO "
        'or add a "code_repo" (or "repo") field to .autonomous-team/config.json'
    )


def fetch_pr_files(pr: int, repo_slug: str, *, gh=None) -> list:
    """Sorted list of changed file paths for *pr*. Raises on an unreadable
    PR rather than returning an empty list, so a network failure can never
    read as "no instruction-bearing paths".

    Uses the paginated REST files endpoint (`gh api --paginate .../files`),
    not `gh pr view --json files`: the latter's pagination behaviour for a
    large diff is unverified, and a client that silently stops at the first
    page would let a diff padded past that cutoff hide a real match beyond
    it (D#2434 review round 2). `--paginate` follows every page GitHub
    hands back for this endpoint and gh merges the pages into one JSON
    array, up to GitHub's own hard per-PR file-count ceiling (3000 as of
    this writing) — the one limit this function cannot see past, because
    nothing client-side can.
    """
    call = gh or _gh
    raw = json.loads(call(["api", "--paginate", f"repos/{repo_slug}/pulls/{pr}/files"]) or "[]")
    entries = raw if isinstance(raw, list) else []
    paths = [
        entry.get("filename")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("filename"), str) and entry.get("filename")
    ]
    return sorted(paths)


#: Defense-in-depth caps for rendering matched paths into a PR comment
#: (D#2434 review round 2 finding #1): a path string comes straight out of
#: an attacker-controlled diff, with no escaping between the GitHub API and
#: a Markdown comment body otherwise. An embedded newline can break out of
#: the list item it's rendered on; a bare Markdown link/image sequence needs
#: no newline at all. Worse than the injection itself: the comment is
#: authored by our own bot account, which `pr_comment_trust.py` treats as
#: TRUSTED — an unsanitized render would launder attacker-written bytes into
#: the trusted half of our own comment-trust partition, under our own
#: signature.
_PATH_DISPLAY_MAX_LEN = 300
_PATH_DISPLAY_MAX_COUNT = 100


def _sanitize_path_for_display(path: str) -> str:
    """Defang a single matched path before it is ever interpolated into
    Markdown. Three independent defenses, each sufficient on its own:

      - every control character (including newline/CR) is replaced, so a
        path cannot break out of the list line it is rendered on;
      - every backtick is replaced, so a path cannot close the code fence
        `render_matched_paths_for_comment` wraps this in;
      - the result is length-capped, so one absurd path cannot balloon the
        comment.

    Markdown *metacharacters* (`[`, `]`, `(`, `)`, `!`, `` ` `` aside) are
    deliberately left untouched here — link/image syntax needs no escaping
    once the whole block is inside a fenced code block, which is where the
    caller always puts this. This function's own defenses (control chars,
    backticks, length) hold even if a future caller forgets the fence.
    """
    out_chars = []
    for ch in path:
        code_point = ord(ch)
        if code_point < 0x20 or code_point == 0x7F:
            out_chars.append("?")
        elif ch == "`":
            out_chars.append("'")
        else:
            out_chars.append(ch)
    sanitized = "".join(out_chars)
    if len(sanitized) > _PATH_DISPLAY_MAX_LEN:
        sanitized = sanitized[:_PATH_DISPLAY_MAX_LEN] + "...(truncated)"
    return sanitized


def render_matched_paths_for_comment(paths: list) -> str:
    """Render *paths* as a fenced, defanged block safe to embed verbatim in
    a PR comment. Every path is sanitized by `_sanitize_path_for_display`;
    the count is capped at `_PATH_DISPLAY_MAX_COUNT` so a diff with an
    absurd number of matches cannot balloon the comment either.
    """
    shown = [p for p in paths if isinstance(p, str)][:_PATH_DISPLAY_MAX_COUNT]
    lines = ["- " + _sanitize_path_for_display(p) for p in shown]
    if len(paths) > _PATH_DISPLAY_MAX_COUNT:
        lines.append(f"...and {len(paths) - _PATH_DISPLAY_MAX_COUNT} more")
    body = "\n".join(lines) if lines else "(none)"
    return "```text\n" + body + "\n```"


def fetch_cross_repository(pr: int, repo_slug: str, *, gh=None) -> bool:
    """True when *pr*'s head lives in a different repo than its base.

    Fail closed: an unreadable PR, or a head repo we cannot read, is not
    evidence of a same-repo PR — see `pr_intake_gate._cross_repository_from_payload`,
    which this deliberately mirrors rather than imports (module docstring).
    """
    call = gh or _gh
    try:
        raw = json.loads(call(["api", f"repos/{repo_slug}/pulls/{pr}"]) or "{}")
    except Exception:  # noqa: BLE001 — fail closed
        return True
    if not isinstance(raw, dict):
        return True
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    base_name = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    if not isinstance(head_name, str) or not head_name:
        return True
    return head_name != base_name


def classify_pr(
    pr: int,
    repo_slug: Optional[str] = None,
    *,
    gh=None,
    cross_repository: Optional[bool] = None,
) -> dict:
    """The predicate result for *pr*. No side effects — never writes to
    GitHub. See module docstring's `--cross-repository` note for why
    *cross_repository* is an optional pass-through rather than always being
    fetched here."""
    slug = repo_slug or _resolve_code_repo()
    files = fetch_pr_files(pr, slug, gh=gh)
    matched = sorted(p for p in files if is_instruction_path(p))

    if cross_repository is None:
        cross_repository = fetch_cross_repository(pr, slug, gh=gh)

    return {
        "pr": pr,
        "repo": slug,
        "paths_touched": matched,
        "cross_repository": bool(cross_repository),
    }


def _parse_check_pr_args(rest: list):
    """Returns (pr, repo_slug_or_None, cross_repository_or_None) or raises
    ValueError with a usage message."""
    slug = None
    cross_repository = None
    positional = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--repo":
            if i + 1 >= len(rest):
                raise ValueError("--repo requires a value")
            slug = rest[i + 1]
            i += 2
            continue
        if token == "--cross-repository":
            if i + 1 >= len(rest):
                raise ValueError("--cross-repository requires a value")
            value = rest[i + 1].strip().lower()
            if value not in ("true", "false"):
                raise ValueError("--cross-repository must be 'true' or 'false'")
            cross_repository = value == "true"
            i += 2
            continue
        positional.append(token)
        i += 1
    if not positional:
        raise ValueError("check-pr requires a PR number")
    try:
        pr = int(positional[0])
    except ValueError as exc:
        raise ValueError(f"check-pr: '{positional[0]}' is not a PR number") from exc
    return pr, slug, cross_repository


def _main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "Usage:\n"
            "  python3 scripts/lib/instruction_paths.py list\n"
            "  python3 scripts/lib/instruction_paths.py check-pr <N> [--repo SLUG] "
            "[--cross-repository true|false]\n"
        )
        return 2

    cmd = argv[1]
    if cmd == "list":
        for prefix in INSTRUCTION_PATH_PREFIXES:
            print(prefix)
        return 0

    if cmd == "check-pr":
        try:
            pr, slug, cross_repository = _parse_check_pr_args(argv[2:])
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        try:
            result = classify_pr(pr, slug, cross_repository=cross_repository)
        except Exception as exc:  # noqa: BLE001 — report, never traceback
            sys.stderr.write(f"check-pr failed for PR #{pr}: {exc}\n")
            return 1
        print(json.dumps(result))
        return 0

    if cmd == "render-matched-paths":
        raw = sys.stdin.read()
        try:
            paths = json.loads(raw or "[]")
        except Exception as exc:  # noqa: BLE001 — report, never traceback
            sys.stderr.write(f"render-matched-paths: stdin was not valid JSON: {exc}\n")
            return 1
        if not isinstance(paths, list):
            sys.stderr.write("render-matched-paths: expected a JSON array of strings on stdin\n")
            return 1
        print(render_matched_paths_for_comment(paths))
        return 0

    sys.stderr.write(f"unknown subcommand: {cmd}\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv))
