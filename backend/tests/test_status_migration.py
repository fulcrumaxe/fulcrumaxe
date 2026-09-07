"""Unit tests for backend/status_migration.py (D#2436 PR-b).

Entirely offline — synthetic bodies only, no network, no GH_TOKEN. Each
measured shape (A, A', B, C, D) gets its own synthetic fixture, not a body
copied from a live Discussion, because a migrated body stops discriminating
the moment it is fixed (Spec item 9).
"""
from __future__ import annotations

from backend.discussion_status import extract_status_anchored, set_status
from backend.status_migration import (
    MIGRATION_BATCH_CAP,
    MIGRATION_TARGET_STATUS,
    content_preserved,
    find_residue_lines,
    migrate_body,
    needs_migration,
    verify_migrated,
)

FIXED_NOW = "2026-09-07T12:00:00Z"

# ---------------------------------------------------------------------------
# Synthetic fixtures, one per measured shape
# ---------------------------------------------------------------------------

SHAPE_D = "<!-- STATUS:NEW SINCE:2026-08-18T00:00:00Z -->\n\n## Intent\nSome content."
SHAPE_A = "STATUS: NEW\n\n## Intent\nSome content."
SHAPE_A_PRIME = "STATUS:NEW\n\n## Intent\nSome content."
SHAPE_B = (
    "## Intent\nProse on line 1 and more lines of prose.\n"
    + ("filler line\n" * 88)
    + "STATUS: NEW\nMore prose after."
)
SHAPE_C = "## Intent\nProse on line 1.\n<!-- STATUS:NEW -->\n\nMore prose after."

ALL_NEW_SHAPES = {
    "D": SHAPE_D,
    "A": SHAPE_A,
    "A'": SHAPE_A_PRIME,
    "B": SHAPE_B,
    "C": SHAPE_C,
}


class TestNeedsMigration:
    def test_all_five_shapes_flagged(self):
        for name, body in ALL_NEW_SHAPES.items():
            assert needs_migration(body), f"shape {name} should need migration"

    def test_no_marker_at_all_not_flagged(self):
        # A body with no STATUS marker anywhere already fails open to
        # DISCUSSING via the registry — out of this migration's scope.
        body = "## Intent\nJust prose, no marker."
        assert not needs_migration(body)

    def test_well_formed_other_status_not_flagged(self):
        body = "<!-- STATUS:IMPLEMENTING SINCE:2026-01-01T00:00:00Z -->\n\nprose"
        assert not needs_migration(body)

    def test_already_migrated_not_flagged(self):
        migrated = migrate_body(SHAPE_D, now_iso=FIXED_NOW)
        assert not needs_migration(migrated)

    def test_inline_code_span_mentioning_the_marker_not_flagged(self):
        # D#2436's own body discusses the shape D marker inline —
        # "well-formed `<!-- STATUS:NEW -->` on line 1" — inside a single-
        # backtick code span, not a line of its own and not a fenced code
        # block either. A substring scan over the whole line would flag its
        # own Discussion as a migration candidate and, since D#2436's real
        # authoritative line is a well-formed SPEC_READY marker, migrate_body
        # would silently overwrite it. This is the live false positive that
        # surfaced on the very first dry run against the real backlog.
        body = (
            "<!-- STATUS:SPEC_READY SINCE:2026-09-07T10:25:00Z -->\n\n"
            "## Spec (Acceptance)\n"
            "- **D** well-formed `<!-- STATUS:NEW -->` on line 1 (24 live: "
            "D#1915, 1917).\n"
        )
        assert not needs_migration(body)
        assert extract_status_anchored(body) == "SPEC_READY"

    def test_prose_sentence_mentioning_bare_form_not_flagged(self):
        body = "## Intent\nSome Discussions carry a bare STATUS: NEW in their bodies, which this fixes."
        assert not needs_migration(body)


class TestMigrateBodyPerShape:
    """Each asserts the post-migration body parses to MIGRATION_TARGET_STATUS
    via extract_status_anchored — Spec item 9."""

    def test_shape_d(self):
        result = migrate_body(SHAPE_D, now_iso=FIXED_NOW)
        assert extract_status_anchored(result) == MIGRATION_TARGET_STATUS

    def test_shape_a(self):
        result = migrate_body(SHAPE_A, now_iso=FIXED_NOW)
        assert extract_status_anchored(result) == MIGRATION_TARGET_STATUS

    def test_shape_a_prime(self):
        result = migrate_body(SHAPE_A_PRIME, now_iso=FIXED_NOW)
        assert extract_status_anchored(result) == MIGRATION_TARGET_STATUS

    def test_shape_b(self):
        result = migrate_body(SHAPE_B, now_iso=FIXED_NOW)
        assert extract_status_anchored(result) == MIGRATION_TARGET_STATUS

    def test_shape_c(self):
        result = migrate_body(SHAPE_C, now_iso=FIXED_NOW)
        assert extract_status_anchored(result) == MIGRATION_TARGET_STATUS

    def test_refuses_body_outside_scope(self):
        import pytest

        with pytest.raises(ValueError):
            migrate_body("## Intent\nno marker here at all.")


class TestShapeCIsASilentNoOpUnderSetStatusAlone:
    """Pins the exact defect D#2436 Spec item 10 documents: applying
    set_status() alone to a shape-C body leaves extract_status_anchored ==
    'UNKNOWN' while returning a changed string. The migration must detect
    this and fail loudly rather than counting it as written — which is
    exactly why migrate_body() does its own line-level surgery instead of
    delegating straight to set_status()."""

    def test_set_status_alone_is_a_silent_no_op_on_shape_c(self):
        naive = set_status(SHAPE_C, MIGRATION_TARGET_STATUS, now_iso=FIXED_NOW)
        assert naive != SHAPE_C  # it did change the string...
        assert extract_status_anchored(naive) == "UNKNOWN"  # ...but not the part that matters

    def test_migrate_body_actually_fixes_shape_c(self):
        fixed = migrate_body(SHAPE_C, now_iso=FIXED_NOW)
        assert extract_status_anchored(fixed) == MIGRATION_TARGET_STATUS


class TestResidueIsFenceAware:
    """Spec item 13: a fence-blind residue check flags D#2436's own
    STATUS:[A-Za-z_-]+ literal (a positive control) and is therefore wrong.
    """

    def test_bare_status_line_flagged_outside_fence(self):
        body = "prose\nSTATUS: NEW\nmore prose"
        assert find_residue_lines(body) == [1]

    def test_bare_status_line_inside_fence_not_flagged(self):
        # Reconstructs the shape of D#2436's own body: a fenced code block
        # containing a STATUS-shaped regex example.
        body = (
            "## Intent\nSome prose describing the regex.\n\n"
            "```\n"
            "STATUS:[A-Za-z_-]+\n"
            "```\n\n"
            "More prose."
        )
        assert find_residue_lines(body) == []

    def test_well_formed_marker_is_not_residue(self):
        # The proper HTML-comment marker is not "bare STATUS:" residue.
        body = "<!-- STATUS:DISCUSSING SINCE:2026-09-07T00:00:00Z -->\n\nprose"
        assert find_residue_lines(body) == []

    def test_migration_leaves_no_residue_for_any_shape(self):
        for name, body in ALL_NEW_SHAPES.items():
            fixed = migrate_body(body, now_iso=FIXED_NOW)
            assert find_residue_lines(fixed) == [], f"shape {name} left residue: {fixed!r}"


class TestContentPreservation:
    """Spec item 14: the migrated body equals the pre-image with only the
    marker line added/replaced and the stale STATUS: line removed — nothing
    else changes."""

    def test_shape_d_only_line_one_changes(self):
        result = migrate_body(SHAPE_D, now_iso=FIXED_NOW)
        pre_rest = SHAPE_D.split("\n", 1)[1]
        post_rest = result.split("\n", 1)[1]
        assert pre_rest == post_rest

    def test_shape_a_prose_survives_untouched(self):
        result = migrate_body(SHAPE_A, now_iso=FIXED_NOW)
        assert "## Intent\nSome content." in result

    def test_shape_b_prose_survives_untouched(self):
        result = migrate_body(SHAPE_B, now_iso=FIXED_NOW)
        assert "More prose after." in result
        assert "Prose on line 1 and more lines of prose." in result

    def test_shape_c_prose_survives_untouched(self):
        result = migrate_body(SHAPE_C, now_iso=FIXED_NOW)
        assert "Prose on line 1." in result
        assert "More prose after." in result


class TestContentPreservedHelper:
    def test_true_for_every_shape(self):
        for name, body in ALL_NEW_SHAPES.items():
            fixed = migrate_body(body, now_iso=FIXED_NOW)
            assert content_preserved(body, fixed), f"shape {name} lost content"

    def test_false_when_prose_changes(self):
        pre = SHAPE_D
        post = migrate_body(SHAPE_D, now_iso=FIXED_NOW).replace("Some content.", "Different content.")
        assert not content_preserved(pre, post)


class TestVerifyMigrated:
    def test_ok_for_correctly_migrated_body(self):
        for name, body in ALL_NEW_SHAPES.items():
            fixed = migrate_body(body, now_iso=FIXED_NOW)
            ok, problems = verify_migrated(fixed)
            assert ok, f"shape {name}: {problems}"

    def test_flags_shape_c_left_broken_by_set_status_alone(self):
        # Read-back verification must catch the exact silent-no-op defect:
        # a write that changed the string but not the anchored answer.
        naive = set_status(SHAPE_C, MIGRATION_TARGET_STATUS, now_iso=FIXED_NOW)
        ok, problems = verify_migrated(naive)
        assert not ok
        assert problems


class TestIdempotence:
    """Spec item 15: a second run over the same set writes nothing and
    reports zero candidates — enforced at the needs_migration() gate."""

    def test_migrated_body_no_longer_needs_migration(self):
        for name, body in ALL_NEW_SHAPES.items():
            fixed = migrate_body(body, now_iso=FIXED_NOW)
            assert not needs_migration(fixed), f"shape {name} still flagged after migration"


class TestMigrationBatchCap:
    def test_cap_is_defined_and_bounded(self):
        assert MIGRATION_BATCH_CAP == 10

    def test_a_run_offered_more_than_cap_writes_at_most_the_cap(self):
        # Simulates the batching a live run would do: given more candidates
        # than the cap, only the first MIGRATION_BATCH_CAP get migrated and
        # the remainder is reported, not written.
        candidates = [f"STATUS: NEW\n\nfixture body #{i}" for i in range(40)]
        assert len(candidates) > MIGRATION_BATCH_CAP

        batch = candidates[:MIGRATION_BATCH_CAP]
        remainder = candidates[MIGRATION_BATCH_CAP:]
        written = [migrate_body(b, now_iso=FIXED_NOW) for b in batch]

        assert len(written) == MIGRATION_BATCH_CAP
        assert all(extract_status_anchored(w) == MIGRATION_TARGET_STATUS for w in written)
        assert len(remainder) == 40 - MIGRATION_BATCH_CAP
