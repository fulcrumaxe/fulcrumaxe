"""tests/test_gate_label_drift.py — D#1958: pin the merge-gate label vocabulary.

CLAUDE.md, the role `.md` files, and the spawn `.tmpl` files are prose read by
an LLM, not code that imports a shared constant — so nothing forces them to
agree with the labels `scripts/loop-phased-step5.sh` actually reads. This
file is the drift guard. It exists because the obvious way to write it is a
can't-fail test: `grep -q security-passed scripts/loop-phased-step5.sh`
PASSES on the pre-fix tree because the string appears on two comment lines.
Every rule below is built to have a real, executed failure path — proved by
a paired negative-fixture test, not a comment claiming immunity.

Five rules:
  Rule 1 (test_extraction_count) — the CLAUDE.md label-bullet extraction
          itself is asserted (== 3), not just a property of whatever comes
          back (>= 0 would pass on an empty list).
  Rule 2 (test_call_site_labels_match_real_script) — the loop script's gate
          labels are extracted by matching `_has_label "$PR_NUM" "<label>"`
          call sites, never by grepping the label string anywhere in the
          file. See test_comment_line_is_not_a_call_site for the trap this
          avoids.
  Rule 3 (test_call_site_labels_are_named_or_allowlisted) — every call-site
          label the loop reads is either named in CLAUDE.md's list or on an
          explicit, reasoned allowlist.
  Rule 4 (test_applied_labels_are_all_known) — every `labels[]="<x>"`
          literal applied by a role `.md` or spawn `.tmpl` is a label
          something downstream (the gate, the NACK list, or the sweep
          script) actually reads.
  Rule 5 (test_gate_labels_are_all_created_by_bootstrap) — D#1910: every
          label the loop reads at a `_has_label` call site must also be
          created by `scripts/bootstrap-github-labels.sh`. Rules 1-4 all
          check documentation/application consistency; none of them had
          label *creation* as an input, so this guard stayed green on a
          fresh-adopter tree where the merge gate blocks on a label that
          bootstrap never creates. See test_bootstrap_missing_label_fixture_
          fails for the non-vacuity proof.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
LOOP_SCRIPT = REPO_ROOT / "scripts" / "loop-phased-step5.sh"
SWEEP_SCRIPT = REPO_ROOT / "scripts" / "sweep-stuck-prs.sh"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
SPAWN_TEMPLATES_DIR = REPO_ROOT / "backend" / "spawn_templates"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-github-labels.sh"

# Rule 3 allowlist: call-site labels the loop reads that are conditional or
# gate-off-by-default, and therefore legitimately absent from CLAUDE.md's
# bullet list of the headline labels. Each entry states why.
CALL_SITE_ALLOWLIST = {
    "browser-test-passed": "dashboard-conditional — only required for PRs that touch the dashboard",
    "debater-confirmed": "gate off by default (gates.debater_pass=false)",
}

EXPECTED_CALL_SITE_LABELS = {
    "browser-test-passed",
    "code-review-passed",
    "debater-confirmed",
    "security-review-passed",
}

EXPECTED_APPLIED_LABEL_COUNT = 6


# ---------------------------------------------------------------------------
# Extraction helpers — these are the mechanism under test. Each is exercised
# against both real repo files (drift checks) and hand-built fixtures (trap
# / non-vacuity checks).
# ---------------------------------------------------------------------------


def _extract_merge_gate_bullets(claude_md_text: str) -> list[str]:
    """Extract the backtick-quoted label bullets under CLAUDE.md's
    '## Merge Gate Protocol' -> 'Default (loop auto-merge)' paragraph.

    Narrow on purpose: only `- \\`label-name\\`` lines immediately following
    that paragraph, up to the first blank line. Rule 1 asserts the *count*
    this returns, not just a property of it — a fixture whose fence holds 2
    entries must make the count check fail (test_extraction_count_fixture_
    with_two_entries_fails).
    """
    m = re.search(r"## Merge Gate Protocol\n\n(.*?)\n\n", claude_md_text, re.DOTALL)
    assert m, "CLAUDE.md must have a '## Merge Gate Protocol' section"
    section = m.group(1)
    return re.findall(r"^- `([a-z0-9-]+)`", section, re.MULTILINE)


def _extract_has_label_call_sites(loop_script_text: str) -> set[str]:
    """Rule 2: match `_has_label "$PR_NUM" "<label>"` call sites only.

    This is deliberately NOT `grep -q <label>` against the whole file — that
    also matches comment lines, which is the exact defect D#1958 exists to
    catch: `security-passed` appears on two comment lines in the real
    script and would make a naive grep pass on the unfixed tree.
    """
    return set(re.findall(r'_has_label\s+"\$PR_NUM"\s+"([a-z0-9-]+)"', loop_script_text))


def _extract_nack_labels(loop_script_text: str) -> set[str]:
    m = re.search(r"_NACK_LABELS=\((.*?)\)", loop_script_text, re.DOTALL)
    assert m, "scripts/loop-phased-step5.sh must define _NACK_LABELS"
    return set(re.findall(r'"([a-zA-Z0-9_-]+)"', m.group(1)))


def _extract_known_labels(sweep_script_text: str) -> set[str]:
    m = re.search(r"_KNOWN_LABELS=\((.*?)\)", sweep_script_text, re.DOTALL)
    assert m, "scripts/sweep-stuck-prs.sh must define _KNOWN_LABELS"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _extract_applied_labels(text: str) -> set[str]:
    """Every `labels[]="<x>"` literal — the syntax that actually reaches the
    `gh api ... -f labels[]=` call in role `.md` files and spawn `.tmpl`
    files applying a label to a live PR."""
    return set(re.findall(r'labels\[\]="([a-zA-Z0-9_-]+)"', text))


def _extract_created_labels(bootstrap_script_text: str) -> set[str]:
    """Rule 5: match `create_label "<label>"` call sites at line start —
    same shape as `_extract_has_label_call_sites` above. Anchoring on line
    start (not just `create_label\\s+"..."` anywhere) keeps this immune to
    the same comment-line trap Rule 2 exists to avoid, since the script's
    own header comment block lists label names in prose above the calls."""
    return set(
        re.findall(r'^create_label\s+"([a-zA-Z0-9_-]+)"', bootstrap_script_text, re.MULTILINE)
    )


def _known_label_union(loop_script_text: str, sweep_script_text: str) -> set[str]:
    """Union of the loop's _has_label call-site labels, its _NACK_LABELS,
    and the sweep script's _KNOWN_LABELS — the full vocabulary something
    downstream actually reads."""
    return (
        _extract_has_label_call_sites(loop_script_text)
        | _extract_nack_labels(loop_script_text)
        | _extract_known_labels(sweep_script_text)
    )


def _all_applied_labels() -> set[str]:
    applied: set[str] = set()
    for path in sorted(AGENTS_DIR.glob("*.md")):
        applied |= _extract_applied_labels(path.read_text())
    for path in sorted(SPAWN_TEMPLATES_DIR.glob("*.tmpl")):
        applied |= _extract_applied_labels(path.read_text())
    return applied


# ---------------------------------------------------------------------------
# Rule 1 — CLAUDE.md extraction count (AC 3)
# ---------------------------------------------------------------------------


def test_extraction_count():
    labels = _extract_merge_gate_bullets(CLAUDE_MD.read_text())
    assert len(labels) == 3, (
        f"expected exactly 3 labels in CLAUDE.md's Merge Gate Protocol bullet "
        f"list, got {len(labels)}: {labels}"
    )


def test_extraction_count_fixture_with_two_entries_fails():
    """Non-vacuity proof for Rule 1: a fence holding 2 entries must make the
    count check fail, so `len(labels) == 3` is a real assertion and not
    decoration a reviewer would need to take on faith."""
    fixture = (
        "## Merge Gate Protocol\n\n"
        "**Default (loop auto-merge):** The loop's merging phase checks these labels:\n"
        "- `code-review-passed` — required, unconditionally\n"
        "- `security-review-passed` — conditional\n\n"
        "This is enforced elsewhere.\n"
    )
    labels = _extract_merge_gate_bullets(fixture)
    assert len(labels) == 2
    with pytest.raises(AssertionError):
        assert len(labels) == 3, "fixture fence deliberately holds only 2 entries"


# ---------------------------------------------------------------------------
# Rule 2 — call-site extraction, set equality against the real script (AC 4)
# ---------------------------------------------------------------------------


def test_call_site_labels_match_real_script():
    labels = _extract_has_label_call_sites(LOOP_SCRIPT.read_text())
    assert labels == EXPECTED_CALL_SITE_LABELS


# ---------------------------------------------------------------------------
# AC 5 — the trap, as an executed test case: a comment-only occurrence of a
# label string must not be extracted as a call site.
# ---------------------------------------------------------------------------


def test_comment_line_is_not_a_call_site():
    fixture_script = (
        '# provenance:external must treat security-passed as a hard merge-gate\n'
        '_has_label() { :; }\n'
        'if _has_label "$PR_NUM" "code-review-passed"; then\n'
        '  echo yes\n'
        'fi\n'
    )
    extracted = _extract_has_label_call_sites(fixture_script)
    assert "security-passed" not in extracted
    assert extracted == {"code-review-passed"}


# ---------------------------------------------------------------------------
# Rule 3 — reverse direction: every real call-site label is named in
# CLAUDE.md or on the reasoned allowlist (AC 6)
# ---------------------------------------------------------------------------


def test_call_site_labels_are_named_or_allowlisted():
    claude_labels = set(_extract_merge_gate_bullets(CLAUDE_MD.read_text()))
    call_sites = _extract_has_label_call_sites(LOOP_SCRIPT.read_text())
    unnamed = call_sites - claude_labels - set(CALL_SITE_ALLOWLIST)
    assert not unnamed, (
        f"call-site label(s) {unnamed} are neither named in CLAUDE.md's Merge "
        f"Gate Protocol list nor on CALL_SITE_ALLOWLIST with a stated reason"
    )


def test_call_site_allowlist_fixture_fails_on_new_label():
    """Non-vacuity proof for Rule 3: a fixture adding a fifth _has_label
    label, with CLAUDE.md's list and the allowlist untouched, must fail."""
    claude_labels = {"code-review-passed", "security-review-passed", "acceptance-passed"}
    fixture_script = (
        '_has_label "$PR_NUM" "code-review-passed"\n'
        '_has_label "$PR_NUM" "some-new-gate-label"\n'
    )
    call_sites = _extract_has_label_call_sites(fixture_script)
    unnamed = call_sites - claude_labels - set(CALL_SITE_ALLOWLIST)
    assert unnamed == {"some-new-gate-label"}


# ---------------------------------------------------------------------------
# Rule 4 — source-side assertion: every labels[]="<x>" literal applied by a
# role .md or spawn .tmpl is a label something downstream reads (AC 7, 8)
# ---------------------------------------------------------------------------


def test_applied_labels_extraction_nonempty_and_exact_count():
    """The extracted labels[] set is asserted non-empty AND equal to an
    expected literal count before any membership check runs (AC 7) — an
    empty extraction would make the membership check below vacuously pass."""
    applied = _all_applied_labels()
    assert len(applied) > 0
    assert len(applied) == EXPECTED_APPLIED_LABEL_COUNT, (
        f"expected exactly {EXPECTED_APPLIED_LABEL_COUNT} distinct "
        f'labels[]="<x>" literals across .claude/agents/*.md and '
        f"backend/spawn_templates/*.tmpl, got {len(applied)}: {sorted(applied)}"
    )


def test_applied_labels_are_all_known():
    applied = _all_applied_labels()
    known = _known_label_union(LOOP_SCRIPT.read_text(), SWEEP_SCRIPT.read_text())
    unknown = applied - known
    assert not unknown, (
        f'labels[]="<x>" literal(s) {unknown} are applied by a role .md or '
        f"spawn .tmpl but are not read by the loop gate, its NACK list, or "
        f"the sweep script's known-label list — this is the D#1958 defect shape"
    )


def test_unknown_applied_label_fixture_fails():
    """Non-vacuity proof for Rule 4 (AC 8): a fixture .md whose body applies
    labels[]="security-passed" must fail — this is the case that would have
    caught security-reviewer.md:84 at the source.

    Deliberately built from fixture loop/sweep text rather than the real
    files: the real sweep script's _KNOWN_LABELS keeps `security-passed` on
    the list forever (AC 19 — the GitHub label itself is never deleted, only
    the code paths that apply it), so a check against the live union alone
    would never flag this literal again after D#1958 lands. This fixture
    isolates the mechanism — extraction + set-difference — from that
    permanent historical entry, and proves the mechanism itself still fails
    correctly on an out-of-vocabulary label.
    """
    fixture_md = 'gh api ... -f labels[]="security-passed"\n'
    fixture_loop = (
        '_has_label "$PR_NUM" "code-review-passed"\n'
        '_NACK_LABELS=(\n'
        '  "acceptance-failed"\n'
        ')\n'
    )
    fixture_sweep = (
        '_KNOWN_LABELS=(\n'
        '  "code-review-passed"\n'
        '  "acceptance-passed"\n'
        '  "acceptance-failed"\n'
        ')\n'
    )
    applied = _extract_applied_labels(fixture_md)
    known = _known_label_union(fixture_loop, fixture_sweep)
    unknown = applied - known
    assert unknown == {"security-passed"}


# ---------------------------------------------------------------------------
# Rule 5 — D#1910: every gate label the loop reads is created by bootstrap
# ---------------------------------------------------------------------------


def test_gate_labels_are_all_created_by_bootstrap():
    call_site_labels = _extract_has_label_call_sites(LOOP_SCRIPT.read_text())
    created_labels = _extract_created_labels(BOOTSTRAP_SCRIPT.read_text())
    missing = call_site_labels - created_labels
    assert not missing, (
        f"label(s) {missing} are read by the loop's merge gate at a _has_label "
        f"call site but scripts/bootstrap-github-labels.sh never creates them — "
        f"a fresh adopter repo can never satisfy this gate. This is the D#1910 "
        f"defect shape: creation was never an input to the other four rules."
    )


def test_bootstrap_missing_label_fixture_fails():
    """Non-vacuity proof for Rule 5: a fixture bootstrap script that omits
    `security-review-passed` (the exact D#1910 defect — creating the old
    `security-passed` name instead) must make the comparison report it
    missing. This is what proves Rule 5 has a real, executed failure path
    rather than a comment claiming immunity."""
    call_site_labels = {
        "browser-test-passed",
        "code-review-passed",
        "debater-confirmed",
        "security-review-passed",
    }
    fixture_bootstrap = (
        'create_label "code-review-passed"    "0E8A16" "..."\n'
        'create_label "security-passed"       "0E8A16" "..."\n'
        'create_label "security-needs-fix"    "B60205" "..."\n'
        'create_label "browser-test-passed"   "0E8A16" "..."\n'
        'create_label "debater-confirmed"     "0E8A16" "..."\n'
    )
    created_labels = _extract_created_labels(fixture_bootstrap)
    missing = call_site_labels - created_labels
    assert missing == {"security-review-passed"}
