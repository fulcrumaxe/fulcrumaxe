"""
spawn_diff.py — unified diff of a spawn template rendered at two different git refs.

Shows prompt engineers and reviewers exactly which lines of a rendered prompt changed
between two commits (typically base vs head of a PR). Use this inside PR review when
backend/spawn_templates/ or backend/spawn_templates.py changes.

Usage:
    python3 backend/spawn_diff.py --role executor --base main --head HEAD
    python3 backend/spawn_diff.py --role code-reviewer --base main --head HEAD \\
        --context-file backend/tests/fixtures/spawn_inspect_fixture.json

Security model
--------------
Each non-disk ref is rendered by checking it out into a detached git worktree
*outside* this repo and running THAT ref's own `backend/spawn_templates.py render
...` CLI as a subprocess, capturing stdout. Nothing from an untrusted ref is ever
imported or exec'd inside this interpreter.

Before any of that happens, the ref is resolved to a commit sha and gated: a sha
is trusted only if it is an ancestor of a branch tracked from one of OUR OWN
configured git remotes -- a remote whose own URL resolves to one of our
repos, not merely a ref that lives under a `refs/remotes/<name>/...` path.
That distinction matters: `refs/remotes/` can hold refs with no configured
remote behind them at all (anyone can `git update-ref refs/remotes/pr/9999
<sha>` by hand -- exactly what fetching a PR head to look at it produces),
and a sha did not get there by us pushing or merging it just because some
ref, anywhere, happens to point at it. A sha that fails this is refused by
default. Use --allow-untrusted-ref to override; doing so prints a warning
naming exactly what is about to execute.

This containment is a process boundary, not a sandbox: the subprocess still runs
as the operator's own uid, on the operator's own filesystem and network. On this
host the `gh`/git credential lives in the system keyring (reached via the same
uid, not via GH_TOKEN or $HOME), so scrubbing GH_TOKEN/GITHUB_TOKEN/$HOME from the
subprocess environment buys nothing against that credential — it is done anyway
because it costs nothing and removes a trivially-inherited path, but it is not a
containment claim. What the fix actually buys: an untrusted ref never reaches
git-show/exec at all unless explicitly overridden, and a trusted ref's code runs
outside this repo and this interpreter, consumed only as stdout.

The disk/working-tree arm (--head HEAD, the tool's default and daily use) is
deliberately NOT gated — gating it would refuse the operator's own unpushed
work for no security gain, since it is already what the operator runs constantly.
A reviewer who has checked out a hostile ref onto disk has already run its code
by that point; that is a separate, already-tracked problem this tool does not
try to solve. It prints which arm it used so that is visible rather than silent.

Severity expiry condition: the case for treating this as "not yet reachable in
practice" (as opposed to merely "bad if it happened") rests on one measured
fact: 194 PRs on the resolved code plane (fulcrumaxe/fulcrumaxe), all 194 with
`isCrossRepository: false`, measured 2026-09-12 via
`gh pr list --state all --limit 300` from the operator checkout. That count is
a snapshot, not a guarantee, and it becomes void the day the first fork PR
lands — after that day only the "bad if it happened" half remains, and this
gate is what still has to hold.

Exit codes:
    0  success (empty diff = no changes; non-empty diff = changes shown)
    1  missing/invalid arguments, unknown role, or unresolvable git ref
    2  rendering error: the ref's own render CLI is missing/failed, or the
       worktree could not be created — never a fallback to in-process import
    3  ref refused by the trust gate (not an ancestor of a branch tracked
       from one of our own configured remotes); pass --allow-untrusted-ref
       to override
"""

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure repo root is importable when run as script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import KNOWN_ROLES  # noqa: E402
from backend._repo import (  # noqa: E402
    REPO as _GH_REPO,
    CODE_REPO as _CODE_REPO_SLUG,
    DISCUSSION_REPO as _DISCUSSION_REPO_SLUG,
)
from backend._repo_remote import _slug_from_url  # noqa: E402

# Repos whose URL makes a git remote "ours" for the ref-trust gate below.
# Deliberately NOT "any configured remote" -- a remote can be configured
# pointing at somebody else's fork -- and deliberately NOT "any
# refs/remotes/* namespace" -- that includes names with no `git remote`
# behind them at all (e.g. a hand-planted `refs/remotes/pr/<n>`, which is
# exactly the shape a reviewer creates when fetching a PR head to look at
# it, and exactly what must NOT be trusted).
_OUR_REPO_SLUGS = frozenset(
    slug for slug in (_GH_REPO, _CODE_REPO_SLUG, _DISCUSSION_REPO_SLUG) if slug
)

# Minimal fixture context used when no --context-file is provided.
# Includes pr_number so code-reviewer and security-reviewer templates render.
_DEFAULT_FIXTURE: dict = {
    "discussion_number": "0",
    "discussion_title": "spawn-diff fixture",
    "discussion_url": f"https://github.com/{_GH_REPO}/discussions/0",
    "task_brief": "(spawn-diff fixture run)",
    "project_context": "[project_context placeholder]",
    "agent_memory": "[agent_memory placeholder]",
    "gate_context": "{}",
    "pr_number": "0",
    "pr_url": f"https://github.com/{_GH_REPO}/pull/0",
    "context_summary": "(spawn-diff fixture)",
    "security_triggers": "",
}

# Set as AUTONOMOUS_TEAM_REPO in the subprocess env so the ref's own
# spawn_templates.py (which calls _load_repo() at import time) has something
# to resolve without needing .autonomous-team/ inside the detached worktree.
# Not a credential -- a benign fixture slug.
_FIXTURE_REPO_SLUG = "spawn-diff-fixture/spawn-diff-fixture"

_DISK_REFS = ("HEAD", "working-tree")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diff a spawn template prompt rendered at two git refs. "
            "Useful in PR review to see what agent prompt text changed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--role",
        required=True,
        help=f"Agent role. Known roles: {', '.join(sorted(KNOWN_ROLES))}",
    )
    parser.add_argument(
        "--base",
        default="main",
        help="Base git ref (default: main)",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="Head git ref (default: HEAD = working tree)",
    )
    parser.add_argument(
        "--context-file",
        default=None,
        help=(
            "Path to a JSON fixture file with render variables "
            "(project_context, agent_memory, etc.). "
            "Defaults to a built-in minimal fixture. Values must be flat "
            "strings/numbers/bools -- the render CLI only accepts --var KEY=VALUE."
        ),
    )
    parser.add_argument(
        "--allow-untrusted-ref",
        action="store_true",
        default=False,
        help=(
            "Override the ref-trust gate and execute a ref's own "
            "backend/spawn_templates.py even though it is not reachable from "
            "a branch tracked from one of our own configured remotes. "
            "Prints a warning first."
        ),
    )
    return parser.parse_args(argv)


def _load_context(context_file: str | None) -> dict:
    """Load render context from file or return built-in fixture.

    Rejects nested (dict/list) values: the render CLI this tool shells out to
    only accepts flat `--var KEY=VALUE` string pairs, so a nested value would
    otherwise have to be silently flattened or stringified in a way that loses
    structure. Refusing is clearer than guessing.
    """
    if context_file is None:
        return dict(_DEFAULT_FIXTURE)
    path = Path(context_file)
    if not path.exists():
        print(f"ERROR: context-file not found: {context_file}", file=sys.stderr)
        sys.exit(1)
    try:
        with path.open() as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"ERROR: could not parse context-file as JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    nested = sorted(k for k, v in data.items() if isinstance(v, (dict, list)))
    if nested:
        print(
            "ERROR: context-file has nested value(s) for key(s) "
            f"{', '.join(nested)}; the render CLI only accepts flat "
            "--var KEY=VALUE pairs. Flatten these to strings before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)
    return data


def _resolve_sha(ref: str) -> str:
    """Resolve `ref` to a full commit sha in _REPO_ROOT. Exits 1 if unresolvable."""
    cmd = ["git", "-C", str(_REPO_ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"ERROR: could not resolve ref '{ref}' to a commit: {result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)
    return result.stdout.strip()


def _our_remote_names() -> list[str]:
    """Names of configured git remotes (from `git remote`, i.e. backed by an
    actual remote.<name>.url in this repo's config) whose URL resolves to one
    of our own repos (_OUR_REPO_SLUGS).

    Deliberately does NOT trust every name that merely appears as a
    `refs/remotes/<name>/...` prefix -- that namespace can hold refs with no
    configured remote behind them at all (anyone can `git update-ref
    refs/remotes/pr/9999 <sha>` by hand), and it can hold a remote that IS
    configured but points at someone else's fork. Both are excluded here:
    the first because it never appears in `git remote`'s output, the second
    because its URL won't be in _OUR_REPO_SLUGS.
    """
    remotes_result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "remote"], capture_output=True, text=True,
    )
    if remotes_result.returncode != 0:
        return []
    trusted_names = []
    for name in remotes_result.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        url_result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "remote", "get-url", name],
            capture_output=True, text=True,
        )
        if url_result.returncode != 0:
            continue
        slug = _slug_from_url(url_result.stdout.strip())
        if slug is not None and slug in _OUR_REPO_SLUGS:
            trusted_names.append(name)
    return trusted_names


def _is_trusted_ref(sha: str) -> bool:
    """True if `sha` is an ancestor of a branch tracked from one of OUR OWN
    remotes -- a configured `git remote` whose URL resolves to one of
    _OUR_REPO_SLUGS -- via `git merge-base --is-ancestor`.

    This is the ref gate. An ad-hoc fetch of someone else's PR ref -- a fork
    head a reviewer pulled down just to look at -- is not reachable from any
    branch tracked from one of our own remotes, so it is refused, EVEN IF
    someone has hand-planted a ref that merely looks like a remote-tracking
    ref (e.g. `refs/remotes/pr/9999`) pointing at it: that namespace has no
    `git remote` behind it, so it is never consulted at all. A commit
    already on a branch we actually track from our own remote (merged, or
    pushed by us) is trusted.
    """
    for remote_name in _our_remote_names():
        refs_result = subprocess.run(
            [
                "git", "-C", str(_REPO_ROOT), "for-each-ref",
                "--format=%(refname)", f"refs/remotes/{remote_name}",
            ],
            capture_output=True, text=True,
        )
        if refs_result.returncode != 0:
            continue
        for remote_ref in refs_result.stdout.splitlines():
            remote_ref = remote_ref.strip()
            if not remote_ref or remote_ref.endswith("/HEAD"):
                continue
            check_result = subprocess.run(
                [
                    "git", "-C", str(_REPO_ROOT), "merge-base", "--is-ancestor",
                    sha, remote_ref,
                ],
                capture_output=True, text=True,
            )
            if check_result.returncode == 0:
                return True
    return False


def _build_subprocess_env() -> dict:
    """Env for the ref's own render CLI subprocess.

    Removes the three keys that are trivially inherited otherwise
    (GH_TOKEN, GITHUB_TOKEN, AUTONOMOUS_TEAM_STATE_DIR) and points
    AUTONOMOUS_TEAM_REPO at a benign fixture slug so the ref's module-level
    _load_repo() has something to resolve without needing .autonomous-team/
    inside the detached worktree. This is NOT a containment claim -- see the
    module docstring. Do not scrub the whole environment: _load_repo() fails
    loudly at import when it cannot resolve a slug, and a fully-scrubbed env
    breaks the render itself rather than proving the gate works.
    """
    env = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AUTONOMOUS_TEAM_STATE_DIR"):
        env.pop(key, None)
    env["AUTONOMOUS_TEAM_REPO"] = _FIXTURE_REPO_SLUG
    return env


def _run_render_cli(worktree_root: Path, role: str, context: dict, ref_label: str) -> str:
    """Run <worktree_root>/backend/spawn_templates.py render <role> --var ...
    as a subprocess and return its stdout. Exits 2 on any failure -- never
    falls back to importing the module in-process.
    """
    template_module = worktree_root / "backend" / "spawn_templates.py"
    if not template_module.exists():
        print(
            f"ERROR: backend/spawn_templates.py not found at ref '{ref_label}'",
            file=sys.stderr,
        )
        sys.exit(2)

    # Invoked as `-m backend.spawn_templates` (not the bare file path) with
    # cwd=worktree_root: spawn_templates.py's own render() does an absolute
    # `from backend.<x> import ...`, which only resolves when the worktree
    # root -- not backend/ itself -- is on sys.path. `-m` with this cwd puts
    # exactly that ref's own worktree root at sys.path[0], so this still
    # runs that ref's own file; it does not reach into the outer repo.
    cmd = [sys.executable, "-m", "backend.spawn_templates", "render", role]
    for key, value in context.items():
        cmd.extend(["--var", f"{key}={value}"])

    result = subprocess.run(
        cmd,
        cwd=str(worktree_root),
        capture_output=True,
        text=True,
        env=_build_subprocess_env(),
    )
    if result.returncode != 0:
        print(
            f"ERROR: render CLI failed at ref '{ref_label}': {result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(2)
    return result.stdout


def _remove_worktree(worktree_dir: str) -> None:
    """Best-effort cleanup: unregister and delete the detached worktree."""
    subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "worktree", "remove", "--force", worktree_dir],
        capture_output=True,
        text=True,
    )
    shutil.rmtree(worktree_dir, ignore_errors=True)


def _render_in_worktree(sha: str, role: str, context: dict, ref_label: str) -> str:
    """Check `sha` out into a detached worktree outside this repo and run
    that ref's own render CLI there. The worktree is always removed.
    """
    worktree_dir = tempfile.mkdtemp(prefix="spawn-diff-wt-")
    try:
        add_cmd = [
            "git", "-C", str(_REPO_ROOT), "worktree", "add", "--detach",
            worktree_dir, sha,
        ]
        add_result = subprocess.run(add_cmd, capture_output=True, text=True)
        if add_result.returncode != 0:
            print(
                f"ERROR: could not create detached worktree for ref '{ref_label}' "
                f"({sha}): {add_result.stderr.strip()}",
                file=sys.stderr,
            )
            sys.exit(2)
        return _run_render_cli(Path(worktree_dir), role, context, ref_label)
    finally:
        _remove_worktree(worktree_dir)


def _render_for_ref(
    ref: str,
    role: str,
    context: dict,
    ref_label: str,
    allow_untrusted_ref: bool,
) -> str:
    """Render `role`'s prompt at `ref`, gating on ref trust first.

    HEAD/working-tree take the disk path unconditionally (never gated -- see
    module docstring). Every other ref is resolved to a sha, gated, and then
    rendered inside a detached worktree via that ref's own render CLI.
    """
    if ref in _DISK_REFS:
        print(
            f"NOTE: '{ref_label}' rendered from the on-disk working tree -- "
            "this path is NOT checked against the ref-trust gate. It is the "
            "operator's own tree (daily use); a checked-out hostile head has "
            "already run its code by that point, which is a separate problem "
            "this gate does not try to solve.",
            file=sys.stderr,
        )
        return _run_render_cli(_REPO_ROOT, role, context, ref_label)

    sha = _resolve_sha(ref)
    if not _is_trusted_ref(sha):
        if not allow_untrusted_ref:
            print(
                f"ERROR: ref '{ref}' (resolved to {sha}) is not reachable from "
                "a branch tracked from one of our own configured remotes -- "
                "refusing to execute its backend/spawn_templates.py. Pass "
                "--allow-untrusted-ref to override.",
                file=sys.stderr,
            )
            sys.exit(3)
        print(
            f"WARNING: --allow-untrusted-ref set -- about to execute "
            f"backend/spawn_templates.py from untrusted ref '{ref}' "
            f"(resolved sha {sha}) in a detached worktree.",
            file=sys.stderr,
        )
    return _render_in_worktree(sha, role, context, ref_label)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.role not in KNOWN_ROLES:
        print(
            f"ERROR: unknown role '{args.role}'. Known roles: {', '.join(sorted(KNOWN_ROLES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    context = _load_context(args.context_file)

    head_ref = args.head
    base_label = args.base
    head_label = head_ref if head_ref not in ("HEAD",) else "HEAD (working tree)"

    base_rendered = _render_for_ref(
        args.base, args.role, context, base_label, args.allow_untrusted_ref
    )
    head_rendered = _render_for_ref(
        head_ref, args.role, context, head_label, args.allow_untrusted_ref
    )

    base_lines = base_rendered.splitlines(keepends=True)
    head_lines = head_rendered.splitlines(keepends=True)

    diff = list(
        difflib.unified_diff(
            base_lines,
            head_lines,
            fromfile=f"spawn_templates [{base_label}] role={args.role}",
            tofile=f"spawn_templates [{head_label}] role={args.role}",
        )
    )

    if diff:
        sys.stdout.writelines(diff)
    else:
        print(f"(no diff — rendered prompt for role '{args.role}' is identical at {args.base} and {head_ref})")


if __name__ == "__main__":
    main()
