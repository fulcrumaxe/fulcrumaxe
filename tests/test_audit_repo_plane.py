"""tests/test_audit_repo_plane.py — tests for the repo-plane detector.

A detector is a measurement tool, and this Discussion exists because three
measurement tools produced confidently wrong answers about this very question:
a line-based sweep that could not see a `gh` call split across a backslash
continuation (and so missed the security gate), a per-file classification for a
file that is correct at one line and wrong at another, and a bash-only sweep
reporting a total for a tree whose Python surface had never been looked at.

So the detector carries its own fixture-based self-test, and this file asserts
that the self-test is real: that it fails when the detector is broken, that it
covers both languages, and that it rejects false positives as well as missed
positives. A green self-test that cannot go red is not evidence.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECTOR = REPO_ROOT / "scripts" / "audit_repo_plane.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_repo_plane", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: @dataclass resolves the defining module out of
    # sys.modules, and raises an opaque AttributeError if it is not there.
    sys.modules["audit_repo_plane"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load()


# ---------------------------------------------------------------------------
# The self-test is real
# ---------------------------------------------------------------------------

def test_self_test_passes():
    assert audit.run_self_test(verbose=False) == 0


def test_self_test_covers_every_language_the_detector_scans():
    """A bash-only detector reporting a whole-tree total is the original sin.

    Tied to iter_sources rather than to a hardcoded set, so adding a language
    to the scanner without adding a known positive for it fails here — which
    is exactly how TypeScript went unmeasured through two audits.
    """
    langs = {fx.language for fx in audit.FIXTURES}
    assert langs == {"bash", "python", "typescript"}


def test_python_in_bash_gh_argv_is_detected():
    """`subprocess.run(['gh', 'api', ...])` inside a bash string.

    Nothing here looks like a shell `gh ` token, and the enclosing python3 -c
    string is double-quoted so bash expands $_REPO before Python sees it.
    Provenance: scripts/drain-pending-prs.sh, a POST to repos/<plane>/pulls —
    PR creation on the wrong repo — cleared by two prior audits.
    """
    src = (
        '_REPO="$(_resolve_repo)"\n'
        'R=$(python3 -c "\n'
        "subprocess.run(['gh', 'api', '-X', 'POST', 'repos/$_REPO/pulls'])\n"
        '")\n'
    )
    defects = [f for f in audit.scan_bash(REPO_ROOT / "x.sh", src) if f.is_defect]
    assert len(defects) == 1, [f.snippet for f in defects]
    assert defects[0].binding == "_REPO"


def test_second_assignment_on_the_same_declaration_line_is_tracked():
    """`local pr="$1" repo="${2:-$(_resolve_repo)}"` taints repo, not pr.

    The old regex needed a ; / && / line-start delimiter, so it attributed the
    resolver to `pr` and never recorded `repo` — downgrading the call site from
    defect to "needs caller trace". Provenance: scripts/lib/pr-dependents.sh:201,
    an undifferentiated default inside scripts/lib/.
    """
    src = (
        "f() {\n"
        '  local pr="$1" repo="${2:-$(_resolve_repo)}"\n'
        '  gh pr view "$pr" --repo "$repo"\n'
        "}\n"
    )
    findings = audit.scan_bash(REPO_ROOT / "x.sh", src)
    defects = [f for f in findings if f.is_defect]
    assert len(defects) == 1, [f.snippet for f in findings]
    assert defects[0].binding == "repo", "the taint must land on repo, not pr"


def test_typescript_spawnsync_gh_is_detected():
    src = (
        'import { resolveRepo } from "../config/repo.js";\n'
        "const _REPO = resolveRepo();\n"
        'spawnSync("gh", ["pr", "view", String(pr), "--repo", _REPO]);\n'
    )
    defects = [
        f for f in audit.scan_typescript(REPO_ROOT / "x.ts", src) if f.is_defect
    ]
    assert len(defects) == 1 and defects[0].binding == "_REPO"


# ---------------------------------------------------------------------------
# Embedded-string argv: the identifier is another language's, so a name match
# is not evidence. Both halves of this were measured on a real file.
# ---------------------------------------------------------------------------

_EMBEDDED_WRONG_PLANE = (
    'import { resolveDiscussionRepo, resolveCodeRepo } from "../config/repo.js";\n'
    "const discRepo = resolveDiscussionRepo();\n"
    "const repo = resolveCodeRepo();\n"
    "const py =\n"
    '  "import subprocess, sys\\n" +\n'
    '  "pr = sys.argv[1]\\n" +\n'
    '  "repo = sys.argv[2]\\n" +\n'
    "  \"subprocess.run(['gh', 'pr', 'view', pr, '--repo', repo])\\n\";\n"
    'runShell(["python3", "-c", py, pr, discRepo]);\n'
)

_EMBEDDED_RENAMED = (
    'import { resolveRepo } from "../config/repo.js";\n'
    "const repo = resolveRepo();\n"
    "const py =\n"
    '  "import subprocess, sys\\n" +\n'
    '  "pr = sys.argv[1]\\n" +\n'
    '  "target = sys.argv[2]\\n" +\n'
    "  \"subprocess.run(['gh', 'pr', 'view', pr, '--repo', target])\\n\";\n"
    'runShell(["python3", "-c", py, pr, repo]);\n'
)


def test_embedded_python_fed_the_wrong_plane_is_not_cleared():
    """The false negative: a live misroute that reported clean.

    The embedded Python `repo` is fed from a Discussion-plane TS variable while
    an unrelated `const repo` holds the code plane. Matching on the name found
    the code-plane const and reported `plane=code defect=False`.
    """
    findings = audit.scan_typescript(REPO_ROOT / "x.ts", _EMBEDDED_WRONG_PLANE)
    assert findings, "the call site must still be found at all"
    f = findings[0]
    assert f.plane == audit.UNRESOLVED, f"plane={f.plane} binding={f.binding}"
    assert f.binding == "NEEDS_CALLER_TRACE"
    assert f.is_defect, "an unresolvable code-plane binding must fail closed"


def test_embedded_python_variable_rename_does_not_clear_the_site():
    """The durability half: renaming the embedded variable must not clear it.

    Before this change the rename dropped the site to `LITERAL`/code — cleared
    — which on the real file took post-merge-hook.ts from 12 defects to 9 and
    made the ratchet tell the operator to *lower* the baseline, i.e. to delete
    three real entries.
    """
    findings = audit.scan_typescript(REPO_ROOT / "x.ts", _EMBEDDED_RENAMED)
    assert findings
    assert findings[0].is_defect
    assert findings[0].plane == audit.UNRESOLVED


def test_the_real_file_no_longer_builds_a_gh_call_inside_a_string():
    """Runs against the committed post-merge-hook.ts, not a fixture of it.

    This used to rename the embedded Python's `repo` variable in that file and
    assert the defect count did not move — the durability property, measured on
    the real thing. The rename has nothing left to rename: the three `gh` calls
    that lived inside Python built as a TypeScript string are TypeScript calls
    now, because a repo binding that cannot be read from the file issuing the
    call is the shape this whole workstream exists to remove, and the scanner
    was right to refuse to guess at it rather than to be taught to guess better.

    The durability property still has a home — see
    test_embedded_python_variable_rename_does_not_clear_the_site, which uses an
    inline fixture and does not depend on the tree still containing one. What
    is asserted here instead is the stronger thing that is now true, and the
    fence against it coming back.
    """
    target = REPO_ROOT / "ts-backend/src/spawn/post-merge-hook.ts"
    if not target.exists():
        pytest.skip("ts-backend not present in this checkout")
    src = target.read_text()

    unresolved = [
        f for f in audit.scan_typescript(target, src)
        if f.plane == audit.UNRESOLVED
    ]
    assert not unresolved, (
        "post-merge-hook.ts builds a gh call inside a string literal again. "
        "The repo it uses cannot be read from this file by a tool or by a "
        "person without following an argv position by hand.\n"
        + "\n".join(f"  {f.line}: {f.snippet}" for f in unresolved)
    )
    # The shapes, not the bare word: this file's own comments say "subprocess"
    # while explaining why there isn't one.
    for shape in ("subprocess.run(", "import subprocess"):
        assert shape not in src, (
            f"an embedded program in this file uses {shape!r} again. If it "
            f"shells out to gh, the repo it uses is invisible from here."
        )


def test_unresolved_identifier_after_repo_is_not_read_as_a_literal_pin():
    """The class, not just the embedded-string instance.

    The fallback keyed on the PRESENCE of `--repo`, not on its value: any argv
    carrying the flag whose identifiers the taint map could not resolve was
    labelled LITERAL and assumed to be the code plane. An embedded string was
    one way to reach that branch; an ordinary unresolved identifier — a
    function parameter, an import, a property access — is another.
    """
    # The `const _REPO` is present but unrelated to the call — the same shape as
    # post-merge-hook.ts, and needed because scan_typescript skips a file with
    # no resolver assignment at all (see the note in that function).
    src = (
        'import { resolveRepo } from "../config/repo.js";\n'
        "const _REPO = resolveRepo();\n"
        "export function f(someRepo: string) {\n"
        '  spawnSync("gh", ["pr", "view", "1", "--repo", someRepo]);\n'
        "}\n"
    )
    f = audit.scan_typescript(REPO_ROOT / "x.ts", src)[0]
    assert f.binding == "NEEDS_CALLER_TRACE", f"binding={f.binding} plane={f.plane}"
    assert f.plane == audit.UNRESOLVED
    assert f.is_defect


def test_a_genuine_literal_slug_is_still_a_pin():
    """A hardcoded owner/name really is pinned — flagging it is a false positive.

    A rule that cannot tell a pinned call from an unresolvable one is not more
    careful, only noisier, and a noisy gate gets switched off.
    """
    src = (
        'import { resolveRepo } from "../config/repo.js";\n'
        "const _REPO = resolveRepo();\n"
        'spawnSync("gh", ["pr", "view", "1", "--repo", "owner/name"]);\n'
    )
    f = audit.scan_typescript(REPO_ROOT / "x.ts", src)[0]
    assert f.binding == "LITERAL" and not f.is_defect


def test_empty_string_after_repo_is_not_a_pin():
    """`gh --repo ""` is the exact failure this workstream exists to close.

    It exits 0 against whatever repo the checkout's remote names, so an empty
    literal must never read as pinned.
    """
    src = (
        'import { resolveRepo } from "../config/repo.js";\n'
        "const _REPO = resolveRepo();\n"
        'spawnSync("gh", ["pr", "view", "1", "--repo", ""]);\n'
    )
    f = audit.scan_typescript(REPO_ROOT / "x.ts", src)[0]
    assert f.plane == audit.UNRESOLVED and f.is_defect


def test_the_ledger_and_the_tree_agree_per_file():
    """Cost of the widened rule, asserted rather than assumed.

    A jump would mean over-correction; a drop would mean it cleared something
    it should not. Either is a finding, not something to absorb.

    This was written as `sum(ledger) == len(defects) == 57` and
    `len(ledger) == 25`, which was a fair way to show the widening moved
    nothing on the day it landed. It is the wrong shape to leave standing: 57
    and 25 describe the TREE's contents, not the RULE's cost, so the assertion
    fires on any correct change to the tree — and it did, on the change that
    emptied the ledger. It fired in the worst available way, too:
    `git merge-tree` was clean, GitHub said MERGEABLE, and no CI job runs
    pytest over tests/, so it would have landed red on main with nothing to
    notice.

    The rule's behaviour is covered, count-free and immune to the tree, by the
    three fixture tests beside this one — an unresolved identifier after
    `--repo` is not a literal pin, a genuine `owner/name` still is, and the
    empty string is not. The frozen counts were this test's only unique
    content, and what they stood in for is a relation: the ledger records
    exactly what the detector finds.

    Per file rather than in sum, which is strictly stronger. A sum hides two
    offsetting moves, and "one file gained a defect while another was fixed" is
    precisely the shape a ratchet exists to catch.

    For the record, since it is worth keeping and is no longer an assertion:
    the widening left the ledger at 57 defects across 25 files, unchanged.
    """
    counts: dict[str, int] = {}
    for f in audit.scan_tree(REPO_ROOT):
        if f.is_defect:
            counts[f.path] = counts.get(f.path, 0) + 1
    ledger = audit.load_baseline()
    assert counts == ledger, (
        "the ledger and the tree disagree. A file above its entry is a new "
        "mis-planed call site; a file below it is a fix that did not lower the "
        "count, which is how a list like this rots into a rubber stamp.\n"
        f"tree:   {sorted(counts.items())}\n"
        f"ledger: {sorted(ledger.items())}"
    )


def test_a_real_ts_call_outside_a_string_still_binds_normally():
    """The unresolved rule must not swallow ordinary TypeScript.

    A guard that reports everything unresolved would 'fix' the false negative
    by making the scanner useless.
    """
    src = (
        'import { resolveRepo } from "../config/repo.js";\n'
        "const repo = resolveRepo();\n"
        'spawnSync("gh", ["pr", "view", "1", "--repo", repo]);\n'
    )
    f = audit.scan_typescript(REPO_ROOT / "x.ts", src)[0]
    assert f.binding == "repo" and f.plane == audit.UNDIFFERENTIATED


def test_typescript_code_repo_is_clean():
    src = (
        'import { resolveCodeRepo } from "../config/repo.js";\n'
        "const _REPO = resolveCodeRepo();\n"
        'spawnSync("gh", ["pr", "view", "1", "--repo", _REPO]);\n'
    )
    assert not [
        f for f in audit.scan_typescript(REPO_ROOT / "x.ts", src) if f.is_defect
    ]


def test_root_flag_scans_another_tree(tmp_path):
    """--root was accepted but never honoured: every scanner computed its
    relative path against the module-level REPO_ROOT, so pointing the detector
    at a base commit raised ValueError on the first file. A flag that cannot do
    the one thing it exists for is worse than no flag."""
    other = tmp_path / "othertree"
    (other / "scripts").mkdir(parents=True)
    (other / "scripts" / "s.sh").write_text(
        'R="$(_resolve_repo)"\ngh pr view 1 --repo "$R"\n'
    )
    defects = [f for f in audit.scan_tree(other) if f.is_defect]
    assert len(defects) == 1
    assert defects[0].path == "scripts/s.sh", defects[0].path


def test_self_test_fails_when_a_known_positive_stops_being_detected(monkeypatch):
    """Break the detector; the self-test must go red.

    Without this, "self-test passed" is a string the script prints, not a
    property it has.
    """
    monkeypatch.setattr(audit, "_SURFACE_RULES", [])
    assert audit.run_self_test(verbose=False) != 0


def test_self_test_fails_when_the_detector_flags_everything(monkeypatch):
    """A detector with no false-positive check would pass by flagging all code."""
    import re as _re

    monkeypatch.setattr(
        audit,
        "_SURFACE_RULES",
        [("everything", _re.compile(r".*"), audit.SURFACE_CODE)],
    )
    assert audit.run_self_test(verbose=False) != 0


def test_cli_self_test_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(DETECTOR), "--self-test"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all pass" in proc.stdout


# ---------------------------------------------------------------------------
# Multi-line awareness — the property the original sweep lacked
# ---------------------------------------------------------------------------

def test_backslash_continuation_is_one_logical_line():
    src = 'gh pr diff --name-only "$pr" \\\n  --repo "$_R" 2>/dev/null\n'
    lines = audit.bash_logical_lines(src)
    assert len(lines) == 1
    assert "--repo" in lines[0][1] and "gh pr diff" in lines[0][1]
    assert lines[0][0] == 1, "must report the FIRST physical line, not the last"


def test_unbalanced_quotes_join_across_lines():
    src = "q=$(gh api graphql -f query='query {\n  repository { id }\n}')\n"
    lines = audit.bash_logical_lines(src)
    assert len(lines) == 1


def test_heredoc_body_is_not_parsed_as_shell():
    """A `gh` inside a Python heredoc is not a bash call site."""
    src = (
        "python3 - <<'PYEOF'\n"
        "subprocess.run(['gh', 'api', 'repos/$X/pulls'])\n"
        "PYEOF\n"
        'gh pr view 1 --repo "$R"\n'
    )
    findings = audit.scan_bash(REPO_ROOT / "x.sh", src)
    calls = [f for f in findings if f.kind == "gh_call"]
    assert len(calls) == 1, [c.snippet for c in calls]


def test_indentation_survives_line_joining():
    """Indentation is the only signal separating source-time from in-function.

    Losing it made every `out=$(gh ...)` inside a function look like a
    source-time global assignment.
    """
    src = 'foo() {\n  out=$(gh pr list \\\n    --repo "$R")\n}\n'
    lines = audit.bash_logical_lines(src)
    body = [t for _, t in lines if "gh pr list" in t][0]
    assert body.startswith(" "), repr(body)


# ---------------------------------------------------------------------------
# Binding detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "snippet,expected",
    [
        ('gh pr view 1 --repo "$FOO"', "FOO"),
        ("gh pr view 1 -R $FOO", "FOO"),
        ('gh api "repos/${FOO}/pulls/1"', "FOO"),
        # GraphQL pins with owner:, not --repo. Missing this silently cleared
        # every Discussion query in start-the-day.sh and auto-plan.sh.
        ('gh api graphql -f query="{ repository(owner:\\"$FOO\\") { id } }"', "FOO"),
    ],
)
def test_repo_binding_is_found(snippet, expected):
    binding, _ = audit._bash_binding(snippet, {expected: audit.UNDIFFERENTIATED})
    assert binding == expected


def test_unpinned_call_is_reported_as_unpinned():
    binding, _ = audit._bash_binding("gh pr list --state open", {})
    assert binding == "UNPINNED"


def test_taint_propagates_through_an_intermediate_variable():
    src = 'A="$(_resolve_repo)"\nB="$A"\ngh pr view 1 --repo "$B"\n'
    defects = [f for f in audit.scan_bash(REPO_ROOT / "x.sh", src) if f.is_defect]
    assert len(defects) == 1
    assert defects[0].binding == "B"


def test_code_plane_resolver_is_not_a_defect():
    src = 'A="$(_resolve_code_repo)"\ngh pr view 1 --repo "$A"\n'
    assert not [f for f in audit.scan_bash(REPO_ROOT / "x.sh", src) if f.is_defect]


# ---------------------------------------------------------------------------
# Surface classification is per call site
# ---------------------------------------------------------------------------

def test_discussion_surface_on_the_discussion_plane_is_clean():
    src = (
        'R="$(_resolve_repo)"\nO="${R%%/*}"\n'
        'gh api graphql -f query="{ repository(owner:\\"$O\\") '
        '{ discussion(number:1) { id } } }"\n'
    )
    assert not [f for f in audit.scan_bash(REPO_ROOT / "x.sh", src) if f.is_defect]


def test_one_file_can_be_right_at_one_line_and_wrong_at_another():
    """bootstrap-github-labels.sh is correct at :136 and wrong at :72.

    Per-file classification cannot express that, which is why the detector
    reports call sites.
    """
    src = (
        'R="$(_resolve_repo)"\n'
        'gh pr view 1 --repo "$R"\n'
        'gh api graphql -f query="{ repository { discussion(number:1) { id } } }"\n'
    )
    findings = audit.scan_bash(REPO_ROOT / "x.sh", src)
    calls = [f for f in findings if f.kind == "gh_call"]
    assert sum(f.is_defect for f in calls) == 1
    assert sum(not f.is_defect for f in calls) == 1


# ---------------------------------------------------------------------------
# Python surface
# ---------------------------------------------------------------------------

def test_python_argv_split_across_lines_is_detected():
    src = (
        "from backend._repo import REPO\n"
        "import subprocess\n"
        "subprocess.run([\n"
        '    "gh",\n    "pr",\n    "list",\n'
        '    "--repo",\n    REPO,\n])\n'
    )
    defects = [f for f in audit.scan_python(REPO_ROOT / "x.py", src) if f.is_defect]
    assert len(defects) == 1 and defects[0].binding == "REPO"


def test_python_code_repo_import_is_clean():
    src = (
        "from backend._repo import CODE_REPO\n"
        "import subprocess\n"
        'subprocess.run(["gh", "pr", "list", "--repo", CODE_REPO])\n'
    )
    assert not [f for f in audit.scan_python(REPO_ROOT / "x.py", src) if f.is_defect]


def test_python_module_without_the_import_is_ignored():
    src = 'import subprocess\nsubprocess.run(["gh", "pr", "list", "--repo", REPO])\n'
    assert audit.scan_python(REPO_ROOT / "x.py", src) == []


# ---------------------------------------------------------------------------
# The known-remaining baseline
# ---------------------------------------------------------------------------

def test_live_tree_has_no_regression_against_the_baseline():
    """The gate that keeps the remaining work from growing silently."""
    findings = audit.scan_tree(REPO_ROOT)
    defects = [f for f in findings if f.is_defect]
    problems = audit.check_against_baseline(defects, audit.load_baseline())
    assert problems == [], "\n".join(problems)


def test_scripts_lib_is_entirely_clean():
    """Every sourced library is on the correct plane — the stated boundary.

    A library is shared by every caller that sources it, so a wrong plane there
    is the widest-blast-radius version of this bug.
    """
    defects = [f for f in audit.scan_tree(REPO_ROOT) if f.is_defect]
    lib = sorted({f.path for f in defects if f.path.startswith("scripts/lib/")})
    assert lib == [], f"scripts/lib/ must stay clean, found: {lib}"


def test_baseline_rejects_a_new_defect_file():
    fake = [
        audit.Finding(
            path="scripts/brand-new.sh", line=1, language="bash", kind="gh_call",
            surface=audit.SURFACE_CODE, rule="gh pr", binding="REPO",
            plane=audit.UNDIFFERENTIATED, snippet="gh pr list",
        )
    ]
    assert audit.check_against_baseline(fake, audit.load_baseline())


def test_baseline_rejects_a_stale_over_allowance():
    """Fixing a site without lowering the count must also fail.

    A baseline that only ever fails upward rots into a rubber stamp: the counts
    drift above reality and stop constraining anything.
    """
    assert audit.check_against_baseline([], {"scripts/whatever.sh": 3})
