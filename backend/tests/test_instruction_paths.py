"""Tests for scripts/lib/instruction_paths.py — D#2434, the path predicate.

Coverage:
  AC-1  the predicate is inspectable — `list` prints the prefixes
  AC-2  the prefix list has a completeness criterion that fails loudly
  AC-3  per-PR classification is JSON, sorted, and writes nothing to GitHub
  AC-4  positive canary — the detector is observed firing on two real PRs
        (#188 touches `.claude/`, #193 touches `hooks/`)
  AC-5  negative canary — the detector stays quiet on a near miss (#190
        touches `scripts/lib/` files that are not the one exact `scripts/
        lib/working-principles.sh` entry in the list)
  AC-10 no hardcoded repo slug; a caller with no resolvable code plane fails
        loudly rather than calling `gh`
  AC-11 the module has zero non-stdlib dependencies

Fixtures for AC-4/AC-5 replay the exact file lists fetched live from PR
#188, #193 and #190 on the code plane on 2026-09-12 (see the PR description
for the live `check-pr` run against the real PRs — that is this Spec's
Gate 2; this file is Gate 1, synthetic-but-realistic).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = _REPO_ROOT / "scripts" / "lib" / "instruction_paths.py"
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import instruction_paths  # noqa: E402

SLUG = "example-org/example-code"


def _gh_files(paths):
    """A `gh` stand-in answering only the paginated REST files endpoint
    (`api --paginate repos/.../pulls/{pr}/files`) — D#2434 review round 2:
    switched off `pr view --json files` because its pagination behaviour for
    a large diff is unverified."""
    calls = []

    def _call(args):
        calls.append(args)
        assert args[0] == "api", f"unexpected gh call: {args}"
        assert "--paginate" in args, f"expected --paginate, got: {args}"
        assert any(a.endswith("/files") and "/pulls/" in a for a in args), f"unexpected gh call: {args}"
        return json.dumps([{"filename": p} for p in paths])

    return _call, calls


# ---------------------------------------------------------------------------
# AC-1 — the predicate exists and is inspectable
# ---------------------------------------------------------------------------


def test_ac1_list_prints_the_minimum_required_prefixes():
    proc = subprocess.run(
        [sys.executable, str(_MODULE_PATH), "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    printed = set(proc.stdout.splitlines())
    required = {
        "CLAUDE.md",
        ".claude/",
        "hooks/",
        ".mcp.json",
        ".autonomous-team/config.json",
        "backend/spawn_templates/",
        ".github/workflows/",
        "scripts/lib/working-principles.sh",
    }
    missing = required - printed
    assert not missing, f"list is missing required prefixes: {missing}"


# ---------------------------------------------------------------------------
# AC-2 — the list has a completeness criterion that fails loudly
# ---------------------------------------------------------------------------


def _tracked_files():
    proc = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
    )
    return proc.stdout.splitlines()


def _prefix_has_tracked_match(prefix, tracked_paths):
    if prefix.endswith("/"):
        return any(p.startswith(prefix) for p in tracked_paths)
    return prefix in tracked_paths


def _prefix_is_gitignored(prefix: str) -> bool:
    """True when *prefix* (stripped of any trailing "/") matches a
    `.gitignore` rule in this checkout, per `git check-ignore` — checked,
    never assumed. `git check-ignore` evaluates the pattern rules against
    the given path string; it does not require the path to exist on disk,
    which is exactly what's needed here since the code plane never checks
    this file out at all.
    """
    target = prefix.rstrip("/")
    proc = subprocess.run(
        ["git", "check-ignore", "--quiet", target],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def test_ac2_every_real_prefix_matches_a_tracked_path_or_is_deliberately_ignored():
    """AC-2's completeness criterion (D#2434 review round 2, option 4):
    every prefix must resolve to something real — either a path this
    checkout actually tracks, or a path this checkout's own `.gitignore`
    deliberately excludes.

    `.autonomous-team/config.json` is the reason this isn't a plain
    tracked-only check. It genuinely is instruction-bearing — it carries
    `code_repo`, which decides which repo every PR/CI operation targets,
    and it is the AC-9 trust-set input `maintainer_allowlist` lives in — but
    the code plane's own `.gitignore` deliberately excludes it (operator-
    local config, the same reason a `.env` file would be excluded), so it is
    never tracked there. AC-2's original form ("must be tracked by `git
    ls-files`") had a false premise for this one entry: "not tracked" was
    read as "wrong" when it can also mean "deliberately excluded." Checking
    `.gitignore` membership explicitly, rather than inferring intent from
    the ambient checkout's tracked-file list, is what keeps this prefix's
    presence in `INSTRUCTION_PATH_PREFIXES` honest on both planes at once:
    tracked (and instruction-bearing) on the engine/operator checkout,
    gitignored (and still instruction-bearing, just never present in a PR
    diff there) on the code plane.

    This still fails loudly on a genuinely bogus or renamed prefix — see
    test_ac2_completeness_check_is_not_vacuous below, which proves it.
    """
    tracked = _tracked_files()
    for prefix in instruction_paths.INSTRUCTION_PATH_PREFIXES:
        if _prefix_has_tracked_match(prefix, tracked):
            continue
        assert _prefix_is_gitignored(prefix), (
            f"{prefix!r} matches no path tracked by `git ls-files` and is not "
            "gitignored either — a directory was renamed or moved without "
            "updating the list, or this prefix was never real to begin with"
        )


def test_ac2_completeness_check_is_not_vacuous():
    """Demonstrates the AC-2 assertion actually fires on a bad prefix,
    rather than trusting that it would — a prefix that is neither tracked
    nor gitignored must fail, not silently pass. This is the check that
    keeps test_ac2_every_real_prefix_matches_a_tracked_path_or_is_deliberately_ignored
    from becoming a completeness test that reports the same result whether
    it ran or not."""
    tracked = _tracked_files()
    bogus = "scripts/lib/this-directory-was-renamed-away-nobody-noticed/"
    assert not _prefix_has_tracked_match(bogus, tracked), (
        "fixture assumption broken: a bogus prefix unexpectedly matched a real "
        "tracked path, so this test cannot demonstrate the assertion firing"
    )
    assert not _prefix_is_gitignored(bogus), (
        "fixture assumption broken: a bogus prefix is unexpectedly gitignored, "
        "so this test cannot demonstrate the assertion firing"
    )
    # Both checks the real test relies on say "no" for this prefix, so a
    # bogus/renamed entry in the real list would indeed fail loudly.


# ---------------------------------------------------------------------------
# AC-3 — per-PR classification, JSON, sorted, no side effects
# ---------------------------------------------------------------------------


def test_ac3_classify_pr_returns_required_shape_sorted_and_read_only():
    gh, calls = _gh_files(["b/two.py", "a/one.py", ".claude/agents/x.md"])
    result = instruction_paths.classify_pr(42, SLUG, gh=gh, cross_repository=False)

    assert result["pr"] == 42
    assert result["paths_touched"] == [".claude/agents/x.md"]
    assert result["cross_repository"] is False

    # No side effects: every call made was a read (`pr view`), never a
    # write (`pr comment`, `pr edit`, `label`, `-X POST`).
    for call in calls:
        joined = " ".join(call)
        assert "comment" not in joined and "-X POST" not in joined and "label" not in joined


def test_ac3_paths_touched_lists_actual_paths_not_prefixes():
    gh, _ = _gh_files(["hooks/sandbox.py", "hooks/subagent_stop_dial_audit.py", "README.md"])
    result = instruction_paths.classify_pr(1, SLUG, gh=gh, cross_repository=False)
    assert result["paths_touched"] == ["hooks/sandbox.py", "hooks/subagent_stop_dial_audit.py"]
    assert "hooks/" not in result["paths_touched"]


# ---------------------------------------------------------------------------
# AC-4 — positive canary: two real PRs, two different prefixes
# ---------------------------------------------------------------------------


def test_ac4_positive_canary_pr188_claude_prefix():
    """Live file list fetched from PR #188 on 2026-09-12."""
    files = [
        ".claude/agents/browser-tester.md",
        "dashboard/src/lib/__tests__/dashboardReady.test.ts",
        "dashboard/src/lib/dashboardReady.ts",
        "engine/manifest.json",
        "scripts/lib/pr-browser-tree.sh",
        "scripts/pr-browser-preview.sh",
        "tests/test_pr_browser_tree.sh",
    ]
    gh, _ = _gh_files(files)
    result = instruction_paths.classify_pr(188, SLUG, gh=gh, cross_repository=False)
    assert result["paths_touched"] == [".claude/agents/browser-tester.md"]


def test_ac4_positive_canary_pr193_hooks_prefix():
    """Live file list fetched from PR #193 on 2026-09-12."""
    files = [
        "engine/manifest.json",
        "hooks/sandbox.py",
        "scripts/post-agent-hook.sh",
        "tests/test_sandbox_hook.sh",
    ]
    gh, _ = _gh_files(files)
    result = instruction_paths.classify_pr(193, SLUG, gh=gh, cross_repository=False)
    assert result["paths_touched"] == ["hooks/sandbox.py"]


# ---------------------------------------------------------------------------
# AC-5 — negative canary: stays quiet on the near miss
# ---------------------------------------------------------------------------


def test_ac5_negative_canary_pr190_stays_quiet():
    """Live file list fetched from PR #190 on 2026-09-12. All three changed
    paths live under scripts/lib/ or backend/tests/ — security-adjacent, not
    instruction-bearing. A broad `scripts/lib/` prefix, or a substring match
    on "intake", would false-positive here; the exact-prefix predicate must
    not."""
    files = [
        "backend/tests/test_trust_id_resolver.py",
        "scripts/lib/external_intake_gate.py",
        "scripts/lib/trust_id_resolver.py",
    ]
    gh, _ = _gh_files(files)
    result = instruction_paths.classify_pr(190, SLUG, gh=gh, cross_repository=False)
    assert result["paths_touched"] == []


def test_ac5_is_instruction_path_rejects_the_near_miss_paths_directly():
    for path in (
        "scripts/lib/external_intake_gate.py",
        "scripts/lib/trust_id_resolver.py",
        "backend/tests/test_trust_id_resolver.py",
    ):
        assert not instruction_paths.is_instruction_path(path), (
            f"{path!r} false-positived — only the exact file "
            "scripts/lib/working-principles.sh should match under scripts/lib/"
        )


def test_ac5_exact_file_entry_still_matches_its_own_path():
    assert instruction_paths.is_instruction_path("scripts/lib/working-principles.sh")


# ---------------------------------------------------------------------------
# Matching semantics — no regex, no substring, no glob (belt-and-suspenders
# beyond the live-PR canaries above)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        ".claude/agents/executor.md",
        ".claude/settings.json",
        "hooks/sandbox.py",
        ".mcp.json",
        ".autonomous-team/config.json",
        "backend/spawn_templates/executor.tmpl",
        ".github/workflows/ci.yml",
        "scripts/lib/working-principles.sh",
    ],
)
def test_is_instruction_path_matches_every_covered_prefix(path):
    assert instruction_paths.is_instruction_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/CLAUDE.md.bak",  # not a substring match on "CLAUDE.md"
        "backend/spawn_templates_old/x.tmpl",  # not a prefix-of-a-sibling-dir match
        ".claude-extra/foo",  # not a prefix-of-a-sibling-dir match
        "scripts/lib/other_working_principles.sh",  # not a substring match
        "",
    ],
)
def test_is_instruction_path_rejects_lookalikes(path):
    assert not instruction_paths.is_instruction_path(path)


# ---------------------------------------------------------------------------
# fetch_cross_repository — fail-closed on an unreadable head
# ---------------------------------------------------------------------------


def test_fetch_cross_repository_fails_closed_on_gh_error():
    def _boom(_args):
        raise RuntimeError("gh api failed (exit 1): rate limited")

    assert instruction_paths.fetch_cross_repository(1, SLUG, gh=_boom) is True


def test_fetch_cross_repository_false_when_same_repo():
    def _gh(_args):
        return json.dumps(
            {"head": {"repo": {"full_name": SLUG}}, "base": {"repo": {"full_name": SLUG}}}
        )

    assert instruction_paths.fetch_cross_repository(1, SLUG, gh=_gh) is False


def test_classify_pr_reuses_caller_supplied_cross_repository_no_extra_call():
    """When the caller already knows cross_repository (the wired loop path),
    classify_pr must not make the extra `gh api .../pulls/{pr}` call to
    re-derive it — that is the "no additional API call on the common path"
    constraint."""
    gh, calls = _gh_files([".claude/agents/x.md"])
    result = instruction_paths.classify_pr(1, SLUG, gh=gh, cross_repository=True)
    assert result["cross_repository"] is True
    assert len(calls) == 1, "cross_repository pass-through must skip the extra api call"


# ---------------------------------------------------------------------------
# AC-10 — repo plane resolved, never hardcoded; guarded failure
# ---------------------------------------------------------------------------


def test_ac10_no_hardcoded_repo_slug_literal():
    src = _MODULE_PATH.read_text()
    assert "fulcrumaxe/fulcrumaxe" not in src


def test_ac10_resolve_code_repo_raises_when_unresolvable(monkeypatch, tmp_path):
    monkeypatch.setattr(instruction_paths, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)
    with pytest.raises(RuntimeError):
        instruction_paths._resolve_code_repo()


def test_ac10_resolve_code_repo_reads_code_repo_then_repo_then_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)

    # No config at all -> falls through to env.
    monkeypatch.setattr(instruction_paths, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "env-org/env-repo")
    assert instruction_paths._resolve_code_repo() == "env-org/env-repo"

    # config.json "repo" wins over the env var.
    cfg_dir = tmp_path / ".autonomous-team"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"repo": "repo-org/repo-repo"}))
    assert instruction_paths._resolve_code_repo() == "repo-org/repo-repo"

    # config.json "code_repo" wins over "repo".
    (cfg_dir / "config.json").write_text(
        json.dumps({"repo": "repo-org/repo-repo", "code_repo": "code-org/code-repo"})
    )
    assert instruction_paths._resolve_code_repo() == "code-org/code-repo"


# ---------------------------------------------------------------------------
# AC-11 — no new dependency: every module-level import root is stdlib
# ---------------------------------------------------------------------------


def test_ac11_stdlib_only_dependencies():
    tree = ast.parse(_MODULE_PATH.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                roots.add(node.module.split(".")[0])
    non_stdlib = roots - set(sys.stdlib_module_names)
    assert not non_stdlib, f"non-stdlib top-level imports in instruction_paths.py: {non_stdlib}"


def test_ac11_no_requirements_or_lockfile_touched():
    """Belt-and-suspenders: nothing under the repo names instruction_paths
    as a reason to add a dependency line. This module's own import graph is
    the real AC-11 check above; this just confirms no lockfile edit rode
    along with it."""
    for candidate in ("requirements.txt", "requirements-dev.txt", "package.json"):
        path = _REPO_ROOT / candidate
        if path.is_file():
            assert "instruction_paths" not in path.read_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_check_pr_cli_usage_errors_are_not_a_pass():
    assert instruction_paths._main(["instruction_paths.py"]) == 2
    assert instruction_paths._main(["instruction_paths.py", "check-pr"]) == 2
    assert instruction_paths._main(["instruction_paths.py", "check-pr", "not-a-number"]) == 2
    assert instruction_paths._main(["instruction_paths.py", "bogus-subcommand"]) == 2


def test_check_pr_cli_rejects_a_bad_cross_repository_value():
    with pytest.raises(ValueError):
        instruction_paths._parse_check_pr_args(["7", "--cross-repository", "maybe"])


# ---------------------------------------------------------------------------
# Comment-rendering sanitizer (D#2434 review round 2, finding #1) — a path
# string is attacker-controlled diff content, rendered into a comment posted
# by our own trusted bot account. Every defense is tested directly against
# adversarial input, not just against the paths a real PR happens to have.
# ---------------------------------------------------------------------------


def test_render_matched_paths_defangs_an_embedded_newline():
    """A newline in a path must not break out of the list item it's
    rendered on."""
    evil = "innocent.md\n](javascript:alert(1))\n[click](http://evil"
    block = instruction_paths.render_matched_paths_for_comment([evil])
    assert "\n](javascript:alert(1))" not in block
    assert "?](javascript:alert(1))" in block or "?" in block


def test_render_matched_paths_defangs_a_bare_markdown_link_with_no_newline():
    """Metacharacters alone (no newline needed) must not render as a link
    or image outside the fence — the whole block must stay inside one."""
    evil = "x](http://evil.example/track.png \"y\") or ![img](http://evil.example/p.png)"
    block = instruction_paths.render_matched_paths_for_comment([evil])
    assert block.startswith("```text\n")
    assert block.endswith("\n```")
    # The payload appears, but only inside the fence — GitHub renders fenced
    # content literally, never as Markdown.
    assert evil in block


def test_render_matched_paths_defangs_a_backtick_fence_breakout_attempt():
    """A path containing a triple-backtick sequence must not be able to
    close the fence early."""
    evil = "escape/```\n# now outside the fence\n```.md"
    block = instruction_paths.render_matched_paths_for_comment([evil])
    # Exactly two fence lines: the opening ```text and the closing ```.
    fence_lines = [line for line in block.splitlines() if line.strip().startswith("```")]
    assert fence_lines == ["```text", "```"], f"fence was broken: {fence_lines}"


def test_render_matched_paths_caps_an_absurdly_long_path():
    huge = "a" * 10_000 + ".md"
    block = instruction_paths.render_matched_paths_for_comment([huge])
    assert len(block) < 1000
    assert "...(truncated)" in block


def test_render_matched_paths_caps_the_number_of_paths_shown():
    paths = [f"p{i}.md" for i in range(250)]
    block = instruction_paths.render_matched_paths_for_comment(paths)
    shown = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(shown) == instruction_paths._PATH_DISPLAY_MAX_COUNT
    assert "...and 150 more" in block


def test_render_matched_paths_normal_case_is_unremarkable():
    block = instruction_paths.render_matched_paths_for_comment(
        [".claude/agents/browser-tester.md", "hooks/sandbox.py"]
    )
    assert block == (
        "```text\n"
        "- .claude/agents/browser-tester.md\n"
        "- hooks/sandbox.py\n"
        "```"
    )


def test_render_matched_paths_cli_subcommand_round_trips(capsys):
    payload = json.dumps([".claude/agents/x.md", "hooks/y.py"])
    import io

    sys.stdin = io.StringIO(payload)
    try:
        rc = instruction_paths._main(["instruction_paths.py", "render-matched-paths"])
    finally:
        sys.stdin = sys.__stdin__
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("```text\n")
    assert ".claude/agents/x.md" in out


def test_render_matched_paths_cli_fails_closed_on_bad_stdin(capsys):
    import io

    sys.stdin = io.StringIO("not json")
    try:
        rc = instruction_paths._main(["instruction_paths.py", "render-matched-paths"])
    finally:
        sys.stdin = sys.__stdin__
    assert rc == 1
