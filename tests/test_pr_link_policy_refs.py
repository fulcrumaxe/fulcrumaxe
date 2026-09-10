"""tests/test_pr_link_policy_refs.py — D#2401: rule 1 of
scripts/ci/pr-link-policy.sh accepts an advancing Discussion reference
(Refs/Part of/Towards D#N), not only a closing one, and
scripts/lib/resolve-pr-discussion.sh resolves the same forms.

WHY THIS EXISTS

The old rule 1 only accepted Closes/Resolves/Fixes D#N, so a PR that only
advances a multi-PR Discussion had no honest way to cite it — close early and
lie, invent a Discussion to close, or leave the real PR unreviewable. The
gate's own header explains rule 1 exists for provenance (a public PR must
name its Discussion, and must not leak one by URL), not for closure, and a
`Refs D#N` line satisfies that exactly as well as `Closes D#N` does. Whether a
Discussion actually closes is decided elsewhere, after merge, by
`discussion_close_decision` reading `planned_prs` — unaffected by this change.

Two things must move together, or a green CI check becomes an unmergeable PR:
`pr-link-policy.sh` (the CI gate) and `resolve-pr-discussion.sh` (what
`merge-and-hook.sh`'s HG-7 check uses to find the Discussion when
`--discussion` isn't passed explicitly). Both are covered here.

The one hazard both files must resist: `Refs #123` (bare, no `D`) must NOT
resolve as Discussion 123. A bare `#N` is an ordinary PR/Issue cross-reference
on the code plane. The advancing verbs are therefore restricted to the `D#`
form only; the pre-existing closing verbs keep their `(D#|#)` leniency
unchanged (an Issue closed via `Closes #N` still works).
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PR_LINK_POLICY = _REPO_ROOT / "scripts" / "ci" / "pr-link-policy.sh"
_RESOLVE_PR_DISCUSSION = _REPO_ROOT / "scripts" / "lib" / "resolve-pr-discussion.sh"

_TIMEOUT = 30


def _run_link_policy(body: str, owner: str = "testowner") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PR_BODY"] = body
    env["PR_LINK_POLICY_CODE_OWNER"] = owner
    # Never let a real CI/local owner source leak in and change which owner
    # rule 2 compares against — this suite is about rule 1.
    env.pop("GITHUB_REPOSITORY", None)
    return subprocess.run(
        ["bash", str(_PR_LINK_POLICY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# scripts/ci/pr-link-policy.sh — rule 1
# ---------------------------------------------------------------------------

CLOSING_STILL_PASS = [
    "Closes D#2348",
    "resolves D#7 in the body",
    "Fixes D#1",
]

ADVANCING_NOW_PASS = [
    "Refs D#2401",
    "refs D#2401",
    "Part of D#2401",
    "part of D#2401",
    "Towards D#2401",
    "towards D#2401",
]

STILL_FAIL = [
    "",
    "No reference at all.",
    "Closes #2348",  # Issue form, no D — unchanged
    "Closes D#",
    "closes d#2348",  # lowercase d — unchanged
    "Refs #2401",  # bare, no D — the D#2401 hazard case
    "refs #2401",
    "Part of #2401",
    "Towards #2401",
]


@pytest.mark.parametrize("body", CLOSING_STILL_PASS)
def test_closing_reference_still_passes(body: str) -> None:
    result = _run_link_policy(body)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("body", ADVANCING_NOW_PASS)
def test_advancing_reference_now_passes(body: str) -> None:
    """Spec item 2 — the core mutation-shaped case (D#2401)."""
    result = _run_link_policy(body)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("body", STILL_FAIL)
def test_no_valid_reference_still_fails(body: str) -> None:
    """Spec items 4, 5, 6 — no reference, Issue-form closes, lowercase d#,
    and the bare 'Refs #N' hazard case must all still fail rule 1."""
    result = _run_link_policy(body)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "no Discussion reference" in result.stdout


def test_bare_refs_hash_is_explicitly_the_hazard_case() -> None:
    """Spec item 6, called out on its own: this is the case a careless fix
    (merging ADVANCES_RE into CLOSES_RE's (D#|#) alternation) breaks."""
    result = _run_link_policy("Refs #2401")
    assert result.returncode != 0, result.stdout + result.stderr


def test_rule2_foreign_owner_url_unaffected() -> None:
    """Spec item 10 — rule 2 must still fire regardless of which rule-1 verb
    is present, and the self-test (which exercises rule 2 in both directions
    on every invocation) must still report success."""
    result = _run_link_policy(
        "Refs D#2401\n\nSee https://github.com/some-other-owner/repo",
        owner="testowner",
    )
    assert result.returncode != 0
    assert "foreign-owner" in result.stdout.lower() or "github.com URL" in result.stdout
    # The self-test always runs first; if it printed a SELF-TEST FAIL line,
    # rule 2 (or rule 1) stopped discriminating and this run proves nothing.
    assert "SELF-TEST FAIL" not in result.stdout
    assert "SELF-TEST FAIL" not in result.stderr


def test_self_test_always_reports_success_on_a_passing_run() -> None:
    result = _run_link_policy("Refs D#2401")
    assert "self-test: both rules assert in both directions" in result.stdout
    assert "SELF-TEST FAIL" not in result.stdout


# ---------------------------------------------------------------------------
# scripts/lib/resolve-pr-discussion.sh — resolve_pr_discussion()
#
# gh is stubbed: `gh pr view ... --json body` returns STUB_PR_BODY, and
# `gh api graphql ... discussion(number:N) { id }` always returns a valid id
# (the fixture only exercises the raw_nums extraction, not GraphQL
# validation — that half is covered by tests/test_resolve_pr_discussion.sh).
# ---------------------------------------------------------------------------

_GH_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    ARGS="$*"
    if [[ "$ARGS" == *"--json body"* ]]; then
      printf '%s' "${STUB_PR_BODY:-}"
      exit 0
    fi
    if [[ "$ARGS" == *"graphql"* && "$ARGS" == *"discussion(number:"* ]]; then
      echo "D_kwDOFakeDiscussionId"
      exit 0
    fi
    echo "unexpected gh call in test stub: $ARGS" >&2
    exit 1
    """
)


@pytest.fixture()
def gh_stub(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(_GH_STUB)
    mode = gh_path.stat().st_mode
    gh_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _resolve(gh_stub_dir: Path, pr_body: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{gh_stub_dir}:{env['PATH']}"
    env["STUB_PR_BODY"] = pr_body
    script = (
        f'source "{_RESOLVE_PR_DISCUSSION}"; '
        'resolve_pr_discussion 99 "owner/code" "owner/disc"'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def test_resolve_refs_d_hash_returns_the_number(gh_stub: Path) -> None:
    """Spec item 7, case 1: 'Refs D#2401' alone resolves to 2401."""
    result = _resolve(gh_stub, "Refs D#2401")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2401"


def test_resolve_bare_refs_hash_returns_nothing(gh_stub: Path) -> None:
    """Spec item 7, case 2: 'Refs #2401' (no D) resolves to empty — the
    D#2401 hazard case, checked directly in the resolver too, not just the
    CI gate."""
    result = _resolve(gh_stub, "Refs #2401")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_resolve_closes_hash_unchanged(gh_stub: Path) -> None:
    """Spec item 7, case 3: 'Closes #2401' (Issue-style, no D) still resolves
    to 2401 — the pre-existing closing-verb (D#|#) leniency is untouched."""
    result = _resolve(gh_stub, "Closes #2401")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2401"


@pytest.mark.parametrize(
    "verb",
    ["Refs", "refs", "Part of", "part of", "Towards", "towards"],
)
def test_resolve_all_advancing_verbs_in_d_form(gh_stub: Path, verb: str) -> None:
    result = _resolve(gh_stub, f"{verb} D#2401")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2401"


def test_resolve_prefers_closing_over_advancing_when_both_present(gh_stub: Path) -> None:
    """A body citing two different Discussions — one via a closing verb, one
    via an advancing verb — resolves to the closing one, per the resolver's
    documented preference order."""
    result = _resolve(gh_stub, "Refs D#1111\n\nCloses D#2222")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2222"
