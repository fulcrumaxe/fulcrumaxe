"""
spawn_inspect.py — dump the full pre-spawn JSON envelope plus rendered prompt for a
given role and discussion so humans can see exactly what a spawned agent will receive.

Usage:
    python3 backend/spawn_inspect.py --role executor --discussion 365
    python3 backend/spawn_inspect.py --role code-reviewer --discussion 365 --pr 400
    python3 backend/spawn_inspect.py --role executor --discussion 365 --json-only
    python3 backend/spawn_inspect.py --role executor --discussion 365 --prompt-only

Exit codes:
    0  success
    1  missing/invalid arguments or unknown role
    2  rendering error from spawn_templates
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Ensure repo root is importable when run as script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import KNOWN_ROLES, render  # noqa: E402
from backend._repo import REPO as _GH_REPO  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect the pre-spawn JSON envelope and rendered prompt for a role.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--role",
        required=True,
        help=f"Agent role. Known roles: {', '.join(sorted(KNOWN_ROLES))}",
    )
    parser.add_argument(
        "--discussion",
        required=True,
        type=int,
        help="Discussion number (required, used as spawn context)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number (optional, passed to pre-spawn-check if provided)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only the JSON envelope (no rendered prompt)",
    )
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="Print only the rendered prompt (no JSON envelope)",
    )
    return parser.parse_args(argv)


def _run_dry_run(role: str, discussion: int, _pr: int | None) -> dict:
    """Invoke pre-spawn-check.sh --dry-run and return parsed JSON.

    D#1788: pre-spawn-check.sh has no --pr flag and does not need one — its
    envelope (persona_voice, working_principles, gate_context, project_context,
    agent_memory) is PR-independent. Forwarding --pr here used to make it fail
    before rendering ever started ("Unknown argument: --pr", exit 1) — verified
    against current main; PR data is fetched separately and merged into the
    render vars by main()/_build_render_vars instead. *_pr* is accepted for
    call-site/signature stability (existing callers pass it positionally) but
    intentionally unused — leading underscore marks that deliberately.
    """
    script = _REPO_ROOT / "scripts" / "pre-spawn-check.sh"
    cmd = ["bash", str(script), "--role", role, "--discussion", str(discussion), "--dry-run"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: pre-spawn-check.sh --dry-run failed for role '{role}':\n{exc.stderr}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: pre-spawn-check.sh produced non-JSON output: {exc}\nOutput was:\n{result.stdout}",
            file=sys.stderr,
        )
        sys.exit(1)


def _build_render_vars(
    envelope: dict,
    discussion: int,
    pr: int | None = None,
    pr_branch: str = "",
) -> dict:
    """Map pre-spawn-check JSON fields (+ optional PR context) to spawn_templates
    render variables.

    D#1788: pr_number / pr_url / pr_branch are only added when *pr* is given —
    a role whose template references {{pr_number}} without one is exactly the
    case the new render() contract must reject loudly (criterion 5), so the
    key must be genuinely absent here rather than present-but-empty.
    """
    vars_ = {
        "discussion_number": str(discussion),
        "discussion_title": f"Discussion #{discussion}",
        "discussion_url": (
            f"https://github.com/{_GH_REPO}/discussions/{discussion}"
        ),
        "task_brief": f"(inspect run for Discussion #{discussion})",
        "project_context": envelope.get("project_context", ""),
        "agent_memory": envelope.get("agent_memory", ""),
        "gate_context": (
            json.dumps(envelope.get("gate_context", {}))
            if isinstance(envelope.get("gate_context"), dict)
            else str(envelope.get("gate_context", ""))
        ),
        # REQUIRED_VARS["runbook-writer"] declares release_id required, and that
        # check (in spawn_templates.render()) only tests key *presence* in the
        # caller-supplied vars dict, not whether the value is truthy — a
        # pre-existing quirk, unrelated to D#1788. release_id has no supplier
        # yet (RENDER_EMPTY_BY_DESIGN excuses it for the new contract check),
        # so an explicit empty string here satisfies both without pretending
        # to have real release data.
        "release_id": "",
    }
    if pr is not None:
        vars_["pr_number"] = str(pr)
        vars_["pr_url"] = f"https://github.com/{_GH_REPO}/pull/{pr}"
        vars_["pr_branch"] = pr_branch
    return vars_


def _fetch_pr_branch(repo: str, pr: int) -> tuple[str, str | None]:
    """Fetch the PR's head branch name via `gh api`.

    Not on the production spawn path (that's scripts/spawn-agent.sh, which
    already makes this exact call and is not touched here) — this is the
    separate manual-inspection tool, and only runs when --pr is given.

    Returns (branch, error). error is None on success; on failure branch is
    "" and error names the real cause. Without a distinct error signal, an
    empty pr_branch downstream surfaces only as a contract error naming
    "pr_branch" — true but useless for debugging an API/network failure, an
    invalid PR number, or a `gh` auth problem (see main() — that's exactly
    the gap D#1788 round 3 closed).
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.ref"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip(), None
        return "", result.stderr.strip() or f"gh api exited {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.role not in KNOWN_ROLES:
        print(
            f"ERROR: unknown role '{args.role}'. Known roles: {', '.join(sorted(KNOWN_ROLES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.json_only and args.prompt_only:
        print("ERROR: --json-only and --prompt-only are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    # Step 1: get pre-spawn envelope (dry-run — no side effects)
    envelope = _run_dry_run(args.role, args.discussion, args.pr)

    # Step 1b: fetch the PR's head branch when --pr is given (D#1788).
    pr_branch = ""
    if args.pr is not None:
        pr_branch, pr_branch_error = _fetch_pr_branch(_GH_REPO, args.pr)
        if pr_branch_error is not None:
            print(
                f"WARN: gh api failed to resolve head branch for PR #{args.pr}: {pr_branch_error}",
                file=sys.stderr,
            )
            # D#1788 round 3: pr_number/pr_url are hard-required with no
            # network involved. pr_branch is a best-effort `gh api` lookup
            # that can fail on a rate limit or blip — but three templates
            # (docs-writer, accessibility-reviewer, runbook-writer)
            # reference {{pr_branch}} and would otherwise either render it
            # blank or fail later inside render() with a generic contract
            # error that never says the real cause was an API failure.
            # Checking the template directly (not a hand-maintained role
            # list) is what keeps this from drifting the way REQUIRED_VARS
            # did.
            tmpl_path = _REPO_ROOT / "backend" / "spawn_templates" / f"{args.role}.tmpl"
            if tmpl_path.exists() and "{{pr_branch}}" in tmpl_path.read_text(encoding="utf-8"):
                print(
                    f"ERROR: role '{args.role}' requires {{{{pr_branch}}}}, but gh api failed to "
                    f"resolve the head branch for PR #{args.pr} (see WARN above). Retry, or check "
                    "gh auth/rate limits.",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Step 2: render prompt from spawn_templates
    render_vars = _build_render_vars(envelope, args.discussion, args.pr, pr_branch)
    try:
        rendered_prompt = render(args.role, render_vars)
    except Exception as exc:
        print(
            f"ERROR: spawn_templates.render failed for role '{args.role}': {exc}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 3: print output
    if args.json_only:
        print(json.dumps(envelope, indent=2))
    elif args.prompt_only:
        print(rendered_prompt)
    else:
        print(json.dumps(envelope, indent=2))
        print()
        print("--- RENDERED PROMPT ---")
        print(rendered_prompt)


if __name__ == "__main__":
    main()
