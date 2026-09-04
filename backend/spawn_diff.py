"""
spawn_diff.py — unified diff of a spawn template rendered at two different git refs.

Shows prompt engineers and reviewers exactly which lines of a rendered prompt changed
between two commits (typically base vs head of a PR). Use this inside PR review when
backend/spawn_templates/ or backend/spawn_templates.py changes.

Usage:
    python3 backend/spawn_diff.py --role executor --base main --head HEAD
    python3 backend/spawn_diff.py --role code-reviewer --base main --head HEAD \\
        --context-file backend/tests/fixtures/spawn_inspect_fixture.json

Exit codes:
    0  success (empty diff = no changes; non-empty diff = changes shown)
    1  missing/invalid arguments, unknown role, or unreadable git ref
    2  rendering error when importing or calling the template module
"""

import argparse
import difflib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Ensure repo root is importable when run as script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import KNOWN_ROLES  # noqa: E402
from backend._repo import REPO as _GH_REPO  # noqa: E402

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
            "Defaults to a built-in minimal fixture."
        ),
    )
    return parser.parse_args(argv)


def _load_context(context_file: str | None) -> dict:
    """Load render context from file or return built-in fixture."""
    if context_file is None:
        return dict(_DEFAULT_FIXTURE)
    path = Path(context_file)
    if not path.exists():
        print(f"ERROR: context-file not found: {context_file}", file=sys.stderr)
        sys.exit(1)
    try:
        with path.open() as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"ERROR: could not parse context-file as JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def _get_module_source_for_ref(ref: str, role: str) -> str:
    """
    Return the source text of backend/spawn_templates.py at the given git ref.
    If ref is HEAD or 'working-tree', read from disk.
    Exits 1 on git errors.
    """
    rel_path = "backend/spawn_templates.py"

    # Resolve HEAD to the actual current working-tree file for accuracy
    if ref in ("HEAD", "working-tree"):
        disk_path = _REPO_ROOT / rel_path
        if not disk_path.exists():
            print(
                f"ERROR: {rel_path} not found on disk (ref={ref})", file=sys.stderr
            )
            sys.exit(1)
        return disk_path.read_text()

    # Otherwise fetch via git show
    cmd = ["git", "-C", str(_REPO_ROOT), "show", f"{ref}:{rel_path}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: could not read '{rel_path}' at ref '{ref}' via git show:\n{exc.stderr}",
            file=sys.stderr,
        )
        sys.exit(1)


def _render_from_source(source: str, role: str, context: dict, ref_label: str) -> str:
    """
    Import spawn_templates source in an isolated namespace and call render().
    Returns the rendered prompt string.
    Exits 2 on import/render failure.

    The .tmpl files are always read from the on-disk templates directory so
    that the diff focuses on logic changes in spawn_templates.py itself
    rather than .tmpl content changes (which should be diffed separately).
    """
    # Inject a __file__ override so Path(__file__).parent resolves to the real
    # backend/ dir when the source uses it to locate the spawn_templates/ dir.
    real_path = str(_REPO_ROOT / "backend" / "spawn_templates.py")
    preamble = f'__file__ = {real_path!r}\n'
    patched_source = preamble + source

    tmp_dir = _REPO_ROOT / ".autonomous-team"
    use_dir = tmp_dir if tmp_dir.exists() else None
    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix="spawn_templates_",
        dir=use_dir,
        delete=False,
        mode="w",
    ) as tmp:
        tmp.write(patched_source)
        tmp_path = Path(tmp.name)

    try:
        spec = importlib.util.spec_from_file_location(
            f"_spawn_templates_{ref_label}", tmp_path
        )
        if spec is None or spec.loader is None:
            print(
                f"ERROR: importlib could not create spec from temp file for ref '{ref_label}'",
                file=sys.stderr,
            )
            sys.exit(2)
        module = types.ModuleType(spec.name)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            print(
                f"ERROR: failed to exec spawn_templates at ref '{ref_label}': {exc}",
                file=sys.stderr,
            )
            sys.exit(2)

        if not hasattr(module, "render"):
            print(
                f"ERROR: spawn_templates at ref '{ref_label}' has no render() function",
                file=sys.stderr,
            )
            sys.exit(2)

        try:
            return module.render(role, context)  # type: ignore[attr-defined]
        except Exception as exc:
            print(
                f"ERROR: render('{role}') failed at ref '{ref_label}': {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
    finally:
        tmp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.role not in KNOWN_ROLES:
        print(
            f"ERROR: unknown role '{args.role}'. Known roles: {', '.join(sorted(KNOWN_ROLES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    context = _load_context(args.context_file)

    # Resolve HEAD: if head equals "HEAD" we use the working-tree file directly
    head_ref = args.head

    # Fetch sources for each ref
    base_source = _get_module_source_for_ref(args.base, args.role)
    head_source = _get_module_source_for_ref(head_ref, args.role)

    # Render prompts
    base_label = args.base
    head_label = head_ref if head_ref not in ("HEAD",) else "HEAD (working tree)"

    base_rendered = _render_from_source(base_source, args.role, context, base_label)
    head_rendered = _render_from_source(head_source, args.role, context, head_label)

    # Diff
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
