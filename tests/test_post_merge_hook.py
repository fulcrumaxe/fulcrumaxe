"""
tests/test_post_merge_hook.py — unit tests for umbrella Discussion detection
in scripts/post-merge-hook.sh.

Tests the pure-logic helpers extracted into a Python module so they can be
exercised without GitHub API calls. The bash integration path is covered by
the existing shell test files (test_post_merge_hook_pull.sh,
test_post_merge_hook_browser_queue.sh).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Pure-logic helpers (mirrors the bash detection logic) ─────────────────────

def batch_letters(disc_body: str) -> list[str]:
    """Return the distinct "**Batch <letter>" markers in a Discussion body.

    Mirrors the bash regex in scripts/post-merge-hook.sh: requires a
    non-letter character immediately after the captured letter so a mid-word
    match (e.g. "**Batch ordering**" capturing the "o" of "ordering") isn't
    counted as a distinct batch letter.
    """
    matches = re.findall(r'\*\*Batch\s+([A-Za-z])[^A-Za-z]', disc_body, re.IGNORECASE)
    return sorted({m.upper() for m in matches})


def slice_labels(disc_body: str) -> list[str]:
    """Return the distinct "Slice <letter><digit>" sub-slice labels in a body.

    Mirrors the bash regex in scripts/post-merge-hook.sh. Requires a digit
    suffix on the label (e.g. "Slice B1", "Slice B2") — this is what
    distinguishes a Discussion's own multi-slice commitment (D#1535's
    "Slice B1"/"Slice B2") from bare prose that merely *describes* another
    Discussion's slices (D#1526/D#1528's "D#1528 Slice A"/"Slice B", which
    never carry a digit suffix). A bold "**Slice <letter>" prefix — the
    approach suggested by D#1584's own Discussion body, mirroring the
    "**Batch <letter>" precedent — does NOT actually appear in D#1535's real
    text, so it was rejected in favor of this verified signal.
    """
    matches = re.findall(r'Slice\s+([A-Za-z][0-9]+)[^A-Za-z0-9]', disc_body, re.IGNORECASE)
    return sorted({m.upper() for m in matches})


def is_umbrella(disc_body: str) -> bool:
    """Return True if the Discussion body is an umbrella spec."""
    has_marker = bool(re.search(r'UMBRELLA:[0-9]+-PR', disc_body))
    pr_sections = len(re.findall(r'^### PR-[a-z]:', disc_body, re.MULTILINE))
    has_batches = len(batch_letters(disc_body)) >= 2
    has_slices = len(slice_labels(disc_body)) >= 2
    return has_marker or pr_sections > 1 or has_batches or has_slices


def planned_count(disc_body: str) -> int:
    """Return the number of planned PRs for an umbrella Discussion."""
    # Count explicit PR sections first
    sections = re.findall(r'^### PR-[a-z]:', disc_body, re.MULTILINE)
    if sections:
        return len(sections)
    # Fall back to UMBRELLA:N-PR marker
    m = re.search(r'UMBRELLA:([0-9]+)-PR', disc_body)
    if m:
        return int(m.group(1))
    # Fall back to distinct "**Batch <letter>" markers
    letters = batch_letters(disc_body)
    if len(letters) >= 2:
        return len(letters)
    # Fall back to distinct "Slice <letter><digit>" sub-slice labels
    slices = slice_labels(disc_body)
    if len(slices) >= 2:
        return len(slices)
    return 0


def planned_labels(disc_body: str) -> list[str]:
    """Return the list of planned PR labels (e.g. ['PR-a', 'PR-b'])."""
    return [s.lstrip('### ').rstrip(':') for s in
            re.findall(r'^### PR-[a-z]:', disc_body, re.MULTILINE)]


def remaining_labels(all_labels: list[str], merged_pr_texts: list[str]) -> list[str]:
    """Return labels not yet mentioned in any merged PR body/title."""
    combined = ' '.join(merged_pr_texts).lower()
    return [lbl for lbl in all_labels if not re.search(r'\b' + re.escape(lbl.lower()) + r'\b', combined)]


def is_deliverable_pr(title: str, discussion: int) -> bool:
    """Return True if a merged PR's title marks it as a genuine batch
    deliverable of the given umbrella Discussion.

    Mirrors the post-D#1574 fix in scripts/post-merge-hook.sh: MERGED_COUNT
    (and the completion_block equivalent) now select on the PR *title*
    prefix ("#N: <description>") instead of a body-wide substring search for
    "#N". A body-wide search matches any merged PR that merely mentions
    "#N" as incidental context (e.g. an incident bug-fix PR), which caused
    D#1534 to be prematurely closed after only 4 of 5 real deliverables had
    merged.
    """
    return title.startswith(f"#{discussion}:")


def build_progress_comment(pr_number: int, remaining: list[str], total: int) -> str:
    """Build the progress comment text."""
    remaining_count = len(remaining)
    if remaining:
        return f"PR #{pr_number} merged. {remaining_count} of {total} PRs remaining: {' '.join(remaining)}."
    return f"PR #{pr_number} merged. Approximately {remaining_count} of {total} PRs remaining."


# ── Fixtures ──────────────────────────────────────────────────────────────────

SINGLE_PR_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-11T10:00:00Z -->

## Overview

Fix the thing.

## Spec

Add foo.

### Acceptance Criteria

1. foo works
"""

UMBRELLA_BODY_MARKER_ONLY = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-11T10:00:00Z UMBRELLA:3-PR -->

## Overview

Big feature split into 3 PRs.

## Spec

Lots of work.
"""

UMBRELLA_BODY_SECTIONS = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-11T10:00:00Z -->

## Spec

### PR-a: Foundation layer

Add base types.

### PR-b: API layer

Expose endpoints.

### PR-c: UI layer

Render in dashboard.
"""

UMBRELLA_BODY_SECTIONS_TWO = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-11T10:00:00Z -->

## Spec

### PR-a: First part

Do the first thing.

### PR-b: Second part

Do the second thing.
"""

# Mirrors D#1534's real Spec: a "Batch ordering" prose line (which must NOT
# be mistaken for a 5th batch letter — the "o" of "ordering" is the trap that
# shipped a regex bug) plus the four actual "**Batch X —" bullets.
UMBRELLA_BODY_BATCH_LETTERS = """\
<!-- STATUS:SPEC_READY SINCE:2026-07-02T10:00:00Z -->

## Spec

7. **Batch ordering**: gate then A then B then C then D then README.

- **Batch A — mechanical sweeps.** State-dir path rewrite.
- **Batch B — Sidebar + Changelog + glob fix.** Coupled fixes.
- **Batch C — narrative rewrites.** Needs code cross-check.
- **Batch D — archive batch.** One atomic git mv.
"""

# Unrelated prose mentioning multiple batch letters outside of a PR-count
# context — should NOT be detected as an umbrella (no bolded "**Batch X"
# markers, just prose referencing letters).
NOT_UMBRELLA_BATCH_PROSE = """\
<!-- STATUS:SPEC_READY SINCE:2026-07-02T10:00:00Z -->

## Spec

Batch ordering: gate then A then B then C then D then README. Batches A, B, C
touch disjoint files and MAY run concurrently.
"""

# Excerpt of D#1535's real body (verified 2026-07-03 via gh api graphql). It
# uses "Slice B1"/"Slice B2" as bare prose (no bold prefix) to describe its
# own two-part Spec — the case this fix must detect. It also mentions
# undigited "Slice A"/"Slice B" for the parent effort, which must NOT
# themselves inflate the count past the two genuine digit-suffixed labels.
UMBRELLA_BODY_SLICE_LABELS_D1535 = """\
<!-- UMBRELLA:2-PR (Slice B1 merged as PR #1540; Slice B2 -- three-way merge \
and conflict-resolver -- still needed) -->

## [Critical] Cross-project update-distribution channel -- Slice B (pull \
mechanism + three-way merge)

Follow-on to D#1528. Slice A (PR #1531, merged) shipped the read-only \
foundation.

Explicitly recommended the B1/B2 split: B1 = pull + classify + lockfile + \
dry-run (~250 lines); B2 = PR generation + conflict-marked PRs + LLM \
resolver (~200 lines).

### Intent
This Spec scopes Slice B1 only: pull + hash-classify + dry-run report.

### Slice B2 (documented follow-on -- NOT in this Spec)
A separate Discussion/Spec will cover PR generation and the LLM \
conflict-resolver.
"""

# Excerpts of D#1526 and D#1528's real bodies (verified 2026-07-03). Both
# mention "Slice A"/"Slice B" repeatedly, but always as bare, undigited
# prose describing ANOTHER Discussion's plan (D#1528's), not committing to
# deliver multiple slices themselves. Must NOT trigger the new Slice signal.
NOT_UMBRELLA_SLICE_PROSE_D1526 = """\
<!-- STATUS:DONE SINCE:2026-07-02T00:00:00Z -->

## [Feature] Harden the coldstart/bootstrap pipeline

Hard-block on D#1528 Slice B. In the meantime, D#1528 Slice A is proceeding
independently. This Discussion is a hard-block on D#1528 Slice B (PR not
yet opened).
"""

NOT_UMBRELLA_SLICE_PROSE_D1528 = """\
<!-- STATUS:SPEC_READY SINCE:2026-07-01T00:00:00Z -->

## [Feature] Engine provisioning layer

Rohan recommends **splitting: Slice A = manifest-only (no PR) ~200 lines;
Slice B = PR generation. This resolves the same way: Slice A has been
scoped, Slice B will be written when Slice A lands. Note: **this Spec is
Slice A only** -- Slice B is a separate follow-on Discussion.
"""


# ── Umbrella detection tests ──────────────────────────────────────────────────

class TestUmbrellaDetection:

    def test_single_pr_body_not_umbrella(self):
        assert is_umbrella(SINGLE_PR_BODY) is False

    def test_umbrella_marker_detected(self):
        assert is_umbrella(UMBRELLA_BODY_MARKER_ONLY) is True

    def test_umbrella_two_sections_detected(self):
        assert is_umbrella(UMBRELLA_BODY_SECTIONS_TWO) is True

    def test_umbrella_three_sections_detected(self):
        assert is_umbrella(UMBRELLA_BODY_SECTIONS) is True

    def test_single_section_not_umbrella(self):
        body = "## Spec\n\n### PR-a: Only one\n\nSome content.\n"
        assert is_umbrella(body) is False

    def test_empty_body_not_umbrella(self):
        assert is_umbrella("") is False

    def test_umbrella_5pr_marker(self):
        body = "<!-- STATUS:SPEC_READY UMBRELLA:5-PR -->\n## Spec\n"
        assert is_umbrella(body) is True

    def test_umbrella_batch_letters_detected(self):
        assert is_umbrella(UMBRELLA_BODY_BATCH_LETTERS) is True

    def test_batch_ordering_prose_not_umbrella(self):
        # Unrelated prose mentioning batch letters, no bolded "**Batch X"
        # markers — should not trigger umbrella detection.
        assert is_umbrella(NOT_UMBRELLA_BATCH_PROSE) is False

    def test_single_batch_letter_not_umbrella(self):
        body = "## Spec\n\n- **Batch A — only one batch.**\n"
        assert is_umbrella(body) is False

    def test_umbrella_slice_labels_detected_d1535(self):
        # D#1535's real Spec (excerpted) — "Slice B1"/"Slice B2" must be
        # detected as a 2-slice umbrella, independent of the UMBRELLA:2-PR
        # marker it also happens to carry (a temporary manual patch that
        # this fix makes redundant).
        assert is_umbrella(UMBRELLA_BODY_SLICE_LABELS_D1535) is True

    def test_slice_prose_not_umbrella_d1526(self):
        # D#1526's real body mentions "Slice A"/"Slice B" repeatedly but
        # always undigited and describing D#1528's plan, not its own.
        assert is_umbrella(NOT_UMBRELLA_SLICE_PROSE_D1526) is False

    def test_slice_prose_not_umbrella_d1528(self):
        # D#1528's real body mentions "Slice A"/"Slice B" (even inside
        # "**splitting: Slice A" and "**this Spec is Slice A only" — bold
        # text that doesn't bold-*prefix* "Slice" itself) but never with a
        # digit suffix, so it must not trigger the new signal either.
        assert is_umbrella(NOT_UMBRELLA_SLICE_PROSE_D1528) is False


# ── Planned count tests ───────────────────────────────────────────────────────

class TestPlannedCount:

    def test_count_from_sections(self):
        assert planned_count(UMBRELLA_BODY_SECTIONS) == 3

    def test_count_from_two_sections(self):
        assert planned_count(UMBRELLA_BODY_SECTIONS_TWO) == 2

    def test_count_from_marker(self):
        assert planned_count(UMBRELLA_BODY_MARKER_ONLY) == 3

    def test_count_sections_preferred_over_marker(self):
        body = "<!-- UMBRELLA:5-PR -->\n## Spec\n### PR-a: one\n\n### PR-b: two\n"
        # 2 sections present — sections take precedence over marker
        assert planned_count(body) == 2

    def test_single_pr_count_zero(self):
        # Single-PR Discussions have no umbrella markers
        assert planned_count(SINGLE_PR_BODY) == 0

    def test_count_from_batch_letters(self):
        # Real D#1534 body: 4 distinct batch letters (A/B/C/D), the
        # "**Batch ordering**" prose line must NOT be counted as a 5th.
        assert planned_count(UMBRELLA_BODY_BATCH_LETTERS) == 4

    def test_batch_ordering_line_not_counted(self):
        # Regression for the exact bug found in review: "**Batch ordering**"
        # must not contribute a spurious "O" letter.
        assert "O" not in batch_letters(UMBRELLA_BODY_BATCH_LETTERS)

    def test_count_from_slice_labels_d1535(self):
        # Real D#1535 body: 2 distinct digit-suffixed slice labels (B1, B2).
        # The undigited "Slice A"/"Slice B" mentions of the parent effort
        # must not inflate this count.
        assert planned_count(UMBRELLA_BODY_SLICE_LABELS_D1535) == 2

    def test_slice_labels_dedup_and_digit_required(self):
        labels = slice_labels(UMBRELLA_BODY_SLICE_LABELS_D1535)
        assert labels == ["B1", "B2"]

    def test_slice_prose_zero_labels_d1526(self):
        assert slice_labels(NOT_UMBRELLA_SLICE_PROSE_D1526) == []

    def test_slice_prose_zero_labels_d1528(self):
        assert slice_labels(NOT_UMBRELLA_SLICE_PROSE_D1528) == []


# ── Planned labels tests ──────────────────────────────────────────────────────

class TestPlannedLabels:

    def test_labels_from_sections(self):
        labels = planned_labels(UMBRELLA_BODY_SECTIONS)
        assert labels == ["PR-a", "PR-b", "PR-c"]

    def test_labels_from_two_sections(self):
        labels = planned_labels(UMBRELLA_BODY_SECTIONS_TWO)
        assert labels == ["PR-a", "PR-b"]

    def test_labels_empty_for_marker_only(self):
        labels = planned_labels(UMBRELLA_BODY_MARKER_ONLY)
        assert labels == []

    def test_labels_empty_for_single_pr(self):
        labels = planned_labels(SINGLE_PR_BODY)
        assert labels == []


# ── Remaining labels tests ────────────────────────────────────────────────────

class TestRemainingLabels:

    def test_all_remaining_when_no_merges(self):
        labels = ["PR-a", "PR-b", "PR-c"]
        remaining = remaining_labels(labels, [])
        assert remaining == ["PR-a", "PR-b", "PR-c"]

    def test_first_pr_merged_two_remain(self):
        labels = ["PR-a", "PR-b", "PR-c"]
        merged_texts = ["Implements PR-a foundation layer (#562)"]
        remaining = remaining_labels(labels, merged_texts)
        assert remaining == ["PR-b", "PR-c"]

    def test_last_pr_leaves_empty_remaining(self):
        labels = ["PR-a", "PR-b"]
        merged_texts = ["Closes #562 — PR-a done", "PR-b complete"]
        remaining = remaining_labels(labels, merged_texts)
        assert remaining == []

    def test_case_insensitive_matching(self):
        labels = ["PR-a"]
        merged_texts = ["Merging pr-a into main"]
        remaining = remaining_labels(labels, merged_texts)
        assert remaining == []


# ── Deliverable PR selection tests (D#1574 regression) ─────────────────────────
# MERGED_COUNT must count PRs by TITLE prefix ("#N: <description>"), not by a
# body-wide substring search for "#N" — the latter over-counts any merged PR
# that merely mentions the Discussion number as incidental context (e.g. an
# incident bug-fix PR). This caused D#1534 to be prematurely closed after
# only 4 of 5 real deliverables merged (PR #1551, #1554, #1569 all mentioned
# "#1534" in their bodies but were not title-prefixed deliverables).

class TestDeliverablePrSelection:

    def test_title_prefixed_pr_is_deliverable(self):
        assert is_deliverable_pr("#1534: Batch D — archive dated audit snapshots", 1534) is True

    def test_body_only_mention_is_not_deliverable(self):
        # Mirrors PR #1551 / #1554 / #1569 — mentions "#1534" as context but
        # the title is not prefixed with it.
        assert is_deliverable_pr("fix post-merge-hook umbrella detection for Batch-style Specs", 1534) is False

    def test_title_mentioning_number_mid_sentence_is_not_deliverable(self):
        # A title that references the number without the anchored "#N:"
        # prefix (e.g. referencing a different, unrelated Discussion inline)
        # must not count.
        assert is_deliverable_pr("#1566: fix grep -c double-zero (context: #1534 incident)", 1534) is False

    def test_different_discussion_number_is_not_deliverable(self):
        assert is_deliverable_pr("#1535: unrelated fix", 1534) is False

    def test_d1534_real_world_merged_count(self):
        # Real merged-PR titles from D#1534's history (verified 2026-07-03).
        # 4 genuine batch deliverables + 3 false-positive body-only mentions
        # that must now be excluded.
        merged_titles = [
            "#1534: wiki-linkcheck gate + Batch A path sweeps",   # genuine
            "#1534: Batch B — sidebar/changelog/sync-wiki fix",   # genuine
            "#1534: Batch C — narrative wiki rewrites",           # genuine
            "#1534: Batch D — archive dated audit snapshots",     # genuine
            "fix post-merge-hook umbrella detection for Batch-style Specs",  # #1551, body-only
            "#1553: ship tui/ improvements",                       # #1554, body-only
            "#1566: fix grep -c double-zero and dedupe umbrella detection",  # #1569, body-only
        ]
        merged_count = sum(1 for t in merged_titles if is_deliverable_pr(t, 1534))
        assert merged_count == 4

    def test_d1552_regression_not_broken(self):
        # D#1552's 3 genuine batch PRs must still count correctly under the
        # title-prefix rule (regression check per D#1574 verification step).
        merged_titles = [
            "#1552: Batch A — backend/ OSS audit scan+fixes",
            "#1552: Batch B — server-mode (server.py) audit pass",
            "#1552: Batch C — manifest + export hardening for backend/",
        ]
        merged_count = sum(1 for t in merged_titles if is_deliverable_pr(t, 1552))
        assert merged_count == 3


# ── Progress comment tests ────────────────────────────────────────────────────

class TestProgressComment:

    def test_comment_with_remaining_labels(self):
        comment = build_progress_comment(123, ["PR-b", "PR-c"], 3)
        assert "PR #123 merged" in comment
        assert "2 of 3 PRs remaining" in comment
        assert "PR-b" in comment
        assert "PR-c" in comment

    def test_comment_no_labels_uses_approximate(self):
        comment = build_progress_comment(99, [], 5)
        # All planned PRs accounted for — remaining is 0
        assert "PR #99 merged" in comment
        assert "0 of 5" in comment

    def test_comment_single_remaining(self):
        comment = build_progress_comment(200, ["PR-e"], 5)
        assert "1 of 5 PRs remaining" in comment
        assert "PR-e" in comment


# ── Integration: bash detection logic via subprocess ─────────────────────────

class TestBashDetectionLogic:
    """
    Smoke-tests for the bash grep patterns used in post-merge-hook.sh.
    These verify the regex patterns produce the same results as the Python logic above.
    """

    def _run_bash(self, script: str) -> tuple[int, str]:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        return result.returncode, (result.stdout + result.stderr).strip()

    def test_bash_umbrella_marker_detected(self):
        body = "<!-- STATUS:SPEC_READY UMBRELLA:3-PR -->"
        rc, out = self._run_bash(
            f"""DISC_BODY={repr(body)}
UMBRELLA_MARKER=$(echo "$DISC_BODY" | grep -oE 'UMBRELLA:[0-9]+-PR' | head -1)
echo "MARKER=$UMBRELLA_MARKER"
[ -n "$UMBRELLA_MARKER" ] && echo IS_UMBRELLA"""
        )
        assert "IS_UMBRELLA" in out

    def test_bash_pr_sections_counted(self):
        # Use $'...' ANSI-C quoting so \n expands to real newlines in bash
        rc, out = self._run_bash(
            r"""DISC_BODY=$'### PR-a: First\n\n### PR-b: Second\n'
PR_SECTIONS=$(printf '%s\n' "$DISC_BODY" | grep -cE '^### PR-[a-z]:' || echo 0)
echo "SECTIONS=$PR_SECTIONS"
[ "$PR_SECTIONS" -gt 1 ] && echo IS_UMBRELLA"""
        )
        assert "IS_UMBRELLA" in out

    def test_bash_single_section_not_umbrella(self):
        rc, out = self._run_bash(
            r"""DISC_BODY=$'### PR-a: Only one\n\nsome content\n'
PR_SECTIONS=$(printf '%s\n' "$DISC_BODY" | grep -cE '^### PR-[a-z]:' || echo 0)
[ "$PR_SECTIONS" -gt 1 ] && echo IS_UMBRELLA || echo NOT_UMBRELLA"""
        )
        assert "NOT_UMBRELLA" in out

    def test_bash_planned_labels_extracted(self):
        rc, out = self._run_bash(
            r"""DISC_BODY=$'### PR-a: one\n\n### PR-b: two\n\n### PR-c: three\n'
PLANNED=$(printf '%s\n' "$DISC_BODY" | grep -oE '^### PR-[a-z]:' | sed 's/^### //' | sed 's/://' | sort)
echo "$PLANNED" """
        )
        assert "PR-a" in out
        assert "PR-b" in out
        assert "PR-c" in out

    def test_bash_batch_letters_excludes_ordering_prose(self):
        # Exact regex used in scripts/post-merge-hook.sh (post-fix). The
        # "**Batch ordering**" line must not contribute a spurious letter.
        rc, out = self._run_bash(
            r"""CURRENT_BODY=$'7. **Batch ordering**: gate then A then B then C then D then README.\n\n- **Batch A \xe2\x80\x94 mechanical sweeps.**\n- **Batch B \xe2\x80\x94 sidebar fix.**\n- **Batch C \xe2\x80\x94 narrative rewrites.**\n- **Batch D \xe2\x80\x94 archive batch.**\n'
BATCH_LETTERS=$(echo "$CURRENT_BODY" | grep -oiE '\*\*Batch[[:space:]]+[A-Za-z][^A-Za-z]' | sed -E 's/^\*\*Batch[[:space:]]+([A-Za-z]).$/\1/' | tr 'a-z' 'A-Z' | sort -u || true)
BATCH_COUNT=$(echo "$BATCH_LETTERS" | grep -c '[A-Z]' || true)
echo "COUNT=$BATCH_COUNT"
echo "LETTERS=$BATCH_LETTERS" """
        )
        assert "COUNT=4" in out
        assert "O" not in out.split("LETTERS=")[1]

    def test_bash_batch_letters_below_threshold_not_umbrella(self):
        rc, out = self._run_bash(
            r"""CURRENT_BODY=$'- **Batch A \xe2\x80\x94 only batch.**\n'
BATCH_LETTERS=$(echo "$CURRENT_BODY" | grep -oiE '\*\*Batch[[:space:]]+[A-Za-z][^A-Za-z]' | sed -E 's/^\*\*Batch[[:space:]]+([A-Za-z]).$/\1/' | tr 'a-z' 'A-Z' | sort -u || true)
BATCH_COUNT=$(echo "$BATCH_LETTERS" | grep -c '[A-Z]' || true)
BATCH_COUNT="${BATCH_COUNT:-0}"
[ "$BATCH_COUNT" -ge 2 ] && echo IS_UMBRELLA || echo NOT_UMBRELLA"""
        )
        assert "NOT_UMBRELLA" in out

    def test_bash_grep_c_no_double_zero_on_empty_input(self):
        # Regression for D#1566 Bug 1: `grep -c ... || echo "0"` produces a
        # two-line "0\n0" string when grep finds no match (grep -c already
        # prints a clean "0" to stdout with a non-zero exit code, so the
        # `|| echo "0"` fallback ALSO fires). The fixed pattern (`|| true`,
        # matching line ~161's BATCH_COUNT) must yield a single-line "0".
        rc, out = self._run_bash(
            r"""PLANNED_LABELS=""
# Buggy pattern (pre-fix): grep -c already emits "0", || echo "0" double-fires
BUGGY=$(echo "$PLANNED_LABELS" | grep -c '[a-z]' || echo "0")
# Fixed pattern (post-fix): no redundant fallback
FIXED=$(echo "$PLANNED_LABELS" | grep -c '[a-z]' || true)
FIXED="${FIXED:-0}"
echo "BUGGY_LINES=$(echo "$BUGGY" | wc -l)"
echo "FIXED_LINES=$(echo "$FIXED" | wc -l)"
echo "FIXED_VALUE=$FIXED" """
        )
        assert "BUGGY_LINES=2" in out, f"expected the pre-fix pattern to reproduce the double-zero bug, got: {out}"
        assert "FIXED_LINES=1" in out
        assert "FIXED_VALUE=0" in out

    def test_bash_grep_c_fixed_pattern_arithmetic_safe(self):
        # The double-zero bug breaks `[[ "$PLANNED_COUNT" -eq 0 ]]` and
        # `$((PLANNED_COUNT - MERGED_COUNT))` with a "syntax error in
        # expression" — confirm the fixed pattern is safe for both.
        rc, out = self._run_bash(
            r"""PLANNED_LABELS=""
PLANNED_COUNT=$(echo "$PLANNED_LABELS" | grep -c '[a-z]' || true)
PLANNED_COUNT="${PLANNED_COUNT:-0}"
[[ "$PLANNED_COUNT" -eq 0 ]] && echo EQ_ZERO_OK
MERGED_COUNT=0
REMAINING=$((PLANNED_COUNT - MERGED_COUNT))
echo "REMAINING=$REMAINING" """
        )
        assert rc == 0, f"arithmetic should not error, got rc={rc} out={out}"
        assert "EQ_ZERO_OK" in out
        assert "REMAINING=0" in out

    def test_bash_jq_title_prefix_excludes_body_only_mentions(self):
        # D#1574 regression: exercises the exact jq filter now used in
        # scripts/post-merge-hook.sh for MERGED_COUNT (title startswith
        # "#N:"), fed synthetic `gh pr list --json number,title`-shaped
        # input so it runs offline without a `gh` API call. Confirms the
        # fixed filter excludes PRs that merely mention "#1534" in the title
        # text without the anchored prefix, while still counting genuine
        # title-prefixed deliverables.
        pr_json = """[
  {"number": 1544, "title": "#1534: wiki-linkcheck gate + Batch A path sweeps"},
  {"number": 1549, "title": "#1534: Batch B \\u2014 sidebar/changelog/sync-wiki fix"},
  {"number": 1555, "title": "#1534: Batch C \\u2014 narrative wiki rewrites"},
  {"number": 1572, "title": "#1534: Batch D \\u2014 archive dated audit snapshots"},
  {"number": 1551, "title": "fix post-merge-hook umbrella detection for Batch-style Specs"},
  {"number": 1554, "title": "#1553: ship tui/ improvements"},
  {"number": 1569, "title": "#1566: fix grep -c double-zero and dedupe umbrella detection"}
]"""
        rc, out = self._run_bash(
            f"""echo '{pr_json}' | jq "[.[] | select(.title | startswith(\\"#1534:\\"))] | length" """
        )
        assert rc == 0, f"jq should not error, got rc={rc} out={out}"
        assert out == "4", f"expected 4 genuine deliverables, got: {out}"


class TestDetectUmbrellaFunctionDedup:
    """
    Sources the real `detect_umbrella` function straight out of
    scripts/post-merge-hook.sh (rather than a re-implementation) and calls
    it exactly as both the discussion_close and completion_block steps do.
    This is the regression guard for D#1566 Bug 2: prior to the fix, the two
    steps carried independent copies of umbrella detection that drifted out
    of sync (completion_block's copy missed the Batch-letter signal). Now
    that both steps call this one function, a single test here covers both
    call sites — they cannot drift again without this test catching it.
    """

    def _run_detect_umbrella(self, body: str) -> dict:
        script_path = REPO_ROOT / "scripts" / "post-merge-hook.sh"
        source_text = script_path.read_text()
        start = source_text.index("detect_umbrella() {")
        end = source_text.index("\n}\n", start) + len("\n}\n")
        function_src = source_text[start:end]

        script = f"""
set -uo pipefail
{function_src}
detect_umbrella "$1"
echo "IS_UMBRELLA=$UMBRELLA_IS_UMBRELLA"
echo "PLANNED_COUNT=$UMBRELLA_PLANNED_COUNT"
echo "PLANNED_LABELS=[$UMBRELLA_PLANNED_LABELS]"
"""
        result = subprocess.run(
            ["bash", "-c", script, "_", body],
            capture_output=True,
            text=True,
        )
        out = result.stdout
        return {
            "is_umbrella": "IS_UMBRELLA=true" in out,
            "planned_count": int(re.search(r"PLANNED_COUNT=(\d+)", out).group(1)),
            "raw": out,
            "returncode": result.returncode,
        }

    def test_batch_letter_umbrella_three_planned(self):
        # Mirrors D#1552's real convention: 3 Batch A/B/C mentions, no
        # UMBRELLA:N-PR marker, no ### PR-a: headings. The completion_block
        # step's stale pre-fix copy would have reported IS_UMBRELLA=false
        # here (it only checked marker + PR sections), which is exactly the
        # bug that caused a premature completion block after 1/3 batches.
        body = (
            "**Batch A — first.** Some work.\n\n"
            "**Batch B — second.** More work.\n\n"
            "**Batch C — third.** Final work.\n"
        )
        result = self._run_detect_umbrella(body)
        assert result["is_umbrella"] is True, result["raw"]
        assert result["planned_count"] == 3, result["raw"]

    def test_no_signals_not_umbrella_zero_planned(self):
        result = self._run_detect_umbrella("Just a normal single-PR Discussion body.")
        assert result["is_umbrella"] is False, result["raw"]
        assert result["planned_count"] == 0, result["raw"]

    def test_marker_umbrella_five_planned(self):
        result = self._run_detect_umbrella("<!-- STATUS:SPEC_READY UMBRELLA:5-PR -->")
        assert result["is_umbrella"] is True, result["raw"]
        assert result["planned_count"] == 5, result["raw"]

    def test_pr_sections_umbrella_two_planned(self):
        body = "### PR-a: First\n\n### PR-b: Second\n"
        result = self._run_detect_umbrella(body)
        assert result["is_umbrella"] is True, result["raw"]
        assert result["planned_count"] == 2, result["raw"]

    def test_slice_labels_umbrella_two_planned(self):
        # D#1584 regression: exercises the real detect_umbrella() bash
        # function (not the Python re-implementation) against D#1535's
        # real Spec convention. Strips the UMBRELLA:2-PR marker so this
        # test proves the new "Slice B1"/"Slice B2" signal alone triggers
        # detection — not the pre-existing marker that was only a manual
        # patch applied after D#1535 closed prematurely.
        body = (
            "Follow-on to D#1528. Slice A (PR #1531, merged) shipped the "
            "read-only foundation.\n\n"
            "Explicitly recommended the B1/B2 split: B1 = pull + classify "
            "(~250 lines); B2 = PR generation + LLM resolver (~200 lines).\n\n"
            "This Spec scopes Slice B1 only.\n\n"
            "### Slice B2 (documented follow-on -- NOT in this Spec)\n"
            "A separate Discussion/Spec will cover PR generation.\n"
        )
        result = self._run_detect_umbrella(body)
        assert result["is_umbrella"] is True, result["raw"]
        assert result["planned_count"] == 2, result["raw"]

    def test_slice_prose_not_umbrella_no_digit(self):
        # D#1526/D#1528-style bare "Slice A"/"Slice B" mentions (no digit
        # suffix) describing another Discussion's plan must NOT trigger.
        body = (
            "Hard-block on D#1528 Slice B. In the meantime, D#1528 Slice A "
            "is proceeding independently. This Discussion is a hard-block "
            "on D#1528 Slice B (PR not yet opened).\n"
        )
        result = self._run_detect_umbrella(body)
        assert result["is_umbrella"] is False, result["raw"]
        assert result["planned_count"] == 0, result["raw"]
