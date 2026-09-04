"""
Unit tests for backend/spec_verification_substance.py (D#2008).

Covers the substance-matching acceptance criteria from D#2008's Spec:
  - AC1: default classification of empty text is "absent"
  - AC2: the canonical ## Real-world verification path is unchanged
  - AC5: the anti-looseness proof — a prose-only equivalent section does
         NOT satisfy the gate, even though it contains backtick spans
  - AC10: the anti-looseness predicate is load-bearing — inverting it
          makes AC5's test fail

These are pure-text tests (no network, no `gh`) — see
backend/spec_verification_substance.py's module docstring for why the
module itself is offline-testable by design.
"""

from __future__ import annotations

import textwrap

import pytest

from backend import spec_verification_substance as svs


# ---------------------------------------------------------------------------
# AC1: matcher is importable and defaults to absent
# ---------------------------------------------------------------------------


class TestDefaultAbsent:
    def test_empty_string_is_absent(self):
        result = svs.classify_spec_text("")
        assert result["status"] == "absent"

    def test_none_like_falsy_is_absent(self):
        # classify_spec_text tolerates a falsy/empty body the same as "".
        result = svs.classify_spec_text("")
        assert result["matched_section"] is None
        assert result["commands"] == []
        assert result["flags"] == []


# ---------------------------------------------------------------------------
# AC2: the canonical section still works, unchanged
# ---------------------------------------------------------------------------


class TestCanonicalSection:
    def test_canonical_heading_satisfied_no_flag(self):
        text = textwrap.dedent("""\
            ## Real-world verification

            Commands:
            - `echo hello`

            Negative checks:
            - exit code must be 0
        """)
        result = svs.classify_spec_text(text)
        assert result["status"] == "satisfied"
        assert result["matched_section"] == "## Real-world verification"
        assert result["commands"] == ["echo hello"]
        assert result["flags"] == []

    def test_canonical_negative_checks_extracted(self):
        text = textwrap.dedent("""\
            ## Real-world verification

            Commands:
            - `python3 backend/kpi_engine.py show --json`

            Negative checks:
            - exit code must be 0
            - stdout must NOT contain "Invalid Date"
        """)
        result = svs.classify_spec_text(text)
        assert result["negative_checks"] == [
            "exit code must be 0",
            'stdout must NOT contain "Invalid Date"',
        ]


# ---------------------------------------------------------------------------
# AC5: prose does not qualify — the anti-looseness proof
# ---------------------------------------------------------------------------


class TestAntiLoosenessProof:
    """A ## Verification section whose only backtick spans are bare
    identifiers must not satisfy the gate. A matcher that passes this
    fixture is too loose (D#2008's Spec, AC5, verbatim)."""

    PROSE_ONLY = textwrap.dedent("""\
        ## Verification

        The feature is verified once `feature_verified` is set on `main`
        and the PR carries `code-review-passed`.
    """)

    def test_prose_only_section_is_absent(self):
        result = svs.classify_spec_text(self.PROSE_ONLY)
        assert result["status"] == "absent"
        assert result["commands"] == []

    def test_bare_identifier_rejected_directly(self):
        assert svs._is_command("feature_verified") is False
        assert svs._is_command("main") is False
        assert svs._is_command("code-review-passed") is False

    def test_real_command_accepted_directly(self):
        assert svs._is_command("python3 backend/kpi_engine.py show --json") is True


# ---------------------------------------------------------------------------
# D#2008 code review, second round: English prose led by a real binary
# name must still be rejected — the leader-allowlist check alone only
# screens the first token, so "test whether the endpoint returns 200",
# "make sure the dashboard loads", and "git blame shows the origin" all
# passed the original predicate (test/make/git are real executable
# leaders) and would have been EXECUTED for real by
# scripts/run-backend-verification.sh's loose extraction, fabricating a
# non-zero exit code and forcing a false needs-fix — the exact deadlock
# this PR exists to close, re-entered through a different door.
# ---------------------------------------------------------------------------


class TestEnglishProseLedByRealBinary:
    """The must-not-regress case: multi-token English prose whose first
    word happens to be a real executable leader must classify as NOT a
    command, even though the bare leader-allowlist + token-count check
    alone would accept it."""

    @pytest.mark.parametrize(
        "sentence",
        [
            "test whether the endpoint returns 200",
            "make sure the dashboard loads",
            "git blame shows the origin",
            "make sure this is correct",
            "test that the build succeeds",
        ],
    )
    def test_prose_led_by_real_leader_rejected(self, sentence):
        assert svs._is_command(sentence) is False, (
            f"{sentence!r} is English prose, not a command, even though its "
            "first word is a real executable leader"
        )

    def test_prose_section_with_real_leader_words_is_absent(self):
        """End-to-end: a ## Verification section made entirely of the kind
        of ordinary English a PM would write, backtick-quoted the way a
        PM would quote a phrase for emphasis, where a couple of sentences
        happen to start with a real binary name, must not satisfy the
        gate. (Backticks are required to reach the predicate at all — a
        candidate with none never becomes a command regardless of
        _is_command, so this fixture must use them to actually exercise
        the mutation-proof below.)"""
        text = textwrap.dedent("""\
            ## Verification

            To confirm this works, `test whether the endpoint returns 200`
            and `make sure the dashboard loads` without errors.
        """)
        result = svs.classify_spec_text(text)
        assert result["status"] == "absent"
        assert result["commands"] == []

    def test_real_command_with_subcommand_word_still_accepted(self):
        """A word list that treats "show"/"returns" as suspicious would
        just reintroduce false positives in the other direction — confirm
        the fix didn't do that."""
        assert svs._is_command("python3 backend/kpi_engine.py show --json") is True
        assert svs._is_command("git show HEAD~1") is True


# ---------------------------------------------------------------------------
# D#2008 code review, THIRD round: two more findings.
#
# (1) Tightening classify_spec_text's predicate never touched the actual
#     failure mode. scripts/run-backend-verification.sh's
#     extract_commands_for_run used loose extraction — no _is_command call
#     at all — so prose sentences kept executing regardless of how strict
#     classify_spec_text became; the gate-status decision and the
#     execution decision were two different code paths. Round 3 makes
#     extract_commands_for_run consult _is_command too, so nothing it
#     returns as runnable can be a candidate the predicate rejects.
#
# (2) The round-2 fix used an English-stopword blacklist ("the", "is",
#     "whether", ...), which the reviewer showed still passes new prose
#     ("diff confirms output matches expected", "test confirms deployment
#     succeeded", "grep confirms matches exist" — none use a stopword) and,
#     separately, is unsound on its own terms: "in"/"if"/"and" are common
#     English *and* real Python/bash keywords (`"x" in y`, `if [ -f f ];
#     then`, `assert x and y`), so a stopword list either misses prose like
#     the reviewer's three sentences or rejects real commands that
#     legitimately use those words as syntax. Replaced with an
#     argument-shape signal (see _ARG_SHAPE_RE) — no stopword list at all.
# ---------------------------------------------------------------------------


class TestArgumentShapeCatchesNonStopwordProse:
    """The reviewer's round-2 evidence that stopwords can't scale: none of
    these three sentences uses any word from the retired stopword list,
    and all three must still be rejected."""

    @pytest.mark.parametrize(
        "sentence",
        [
            "diff confirms output matches expected",
            "test confirms deployment succeeded",
            "grep confirms matches exist",
        ],
    )
    def test_non_stopword_prose_rejected(self, sentence):
        assert svs._is_command(sentence) is False, (
            f"{sentence!r} has no argument-shape token among its remaining "
            "tokens (no path, no flag, no quote, no extension) — prose, "
            "not a command, even with no stopword present"
        )


class TestStopwordCollisionWithRealSyntax:
    """The retired mechanism's own failure mode: "in"/"if"/"and" are both
    common English words AND real Python/bash keywords. A predicate that
    rejects candidates containing them would misclassify these real,
    legitimate verification commands — confirm the argument-shape-only
    design does not."""

    def test_python_membership_check_accepted(self):
        cmd = (
            'python3 -c "import json,sys; '
            's=json.load(open(\\"/tmp/x.json\\")); '
            'assert \\"hooks\\" in s"'
        )
        assert svs._is_command(cmd) is True

    def test_bash_if_statement_accepted(self):
        assert svs._is_command("bash -c 'if [ -f /tmp/x ]; then echo yes; fi'") is True

    def test_python_and_operator_accepted(self):
        assert svs._is_command('python3 -c "assert x and y"') is True


class TestRealCommandFalseNegatives:
    """Non-blocking (per the frozen Spec's ruling — `absent` never blocks):
    a handful of real commands the original predicate missed. Fixing these
    must not reopen the false-positive door above — see
    test_bare_tool_mention_in_prose_still_rejected."""

    def test_yarn_and_tsc_now_recognised(self):
        """yarn/tsc weren't in the original leader allowlist at all."""
        assert svs._is_command("yarn test") is True
        assert svs._is_command("tsc --noEmit") is True

    def test_bare_single_token_commands_still_rejected(self):
        """A bare tool name — even a real one — stays a false negative on
        purpose. scripts/lib/resolve-spec-text.sh resolves D#1944's own
        Spec comment, which contains 'assert via `jq` that ...' as
        descriptive prose, not a standalone invocation — recognising a
        bare `jq` as a command would misclassify that exact real Spec.
        The frozen Spec's ruling values this direction of failure less:
        an unrecognised real command only costs a missed `satisfied`
        flag, never a merge block."""
        assert svs._is_command("pytest") is False
        assert svs._is_command("jq") is False

    def test_bare_tool_mention_in_prose_still_rejected(self):
        text = (
            "assert via `jq` that `.old` and `.new` are both present "
            "and non-empty"
        )
        # None of these backtick spans should count as commands: `jq` is a
        # bare tool mention, `.old`/`.new` are field names, not leaders.
        result = svs._extract_commands_strict(text)
        assert result == []


# ---------------------------------------------------------------------------
# Equivalent-section satisfaction (D#1997's shape)
# ---------------------------------------------------------------------------


class TestEquivalentSection:
    def test_spec_acceptance_numbered_continuation_shape(self):
        """D#1997's exact shape: numbered items with the command on an
        indented continuation line, no dash bullets at all."""
        text = textwrap.dedent("""\
            ## Spec (Acceptance)

            1. **Canonical resolver exists.**
               `python3 -c "import backend._repo_root as m; print(m.repo_root())"` — prints a path.

            2. **Guard passes.**
               `bash scripts/check-no-hardcoded-checkout-paths.sh` — exit 0.
        """)
        result = svs.classify_spec_text(text)
        assert result["status"] == "satisfied_with_flag"
        assert result["matched_section"] == "## Spec (Acceptance)"
        assert result["flags"] == ["equivalent_section"]
        assert len(result["commands"]) == 2

    def test_acceptance_heading_variants_recognised(self):
        for heading in ("## Acceptance", "### Acceptance", "## Verification"):
            text = f"{heading}\n\n- `bash scripts/check-no-hardcoded-checkout-paths.sh`\n"
            result = svs.classify_spec_text(text)
            assert result["status"] == "satisfied_with_flag", heading
            assert result["matched_section"] == heading

    def test_canonical_takes_priority_over_equivalent(self):
        text = textwrap.dedent("""\
            ## Spec (Acceptance)
            - `git status`

            ## Real-world verification

            Commands:
            - `echo hello`
        """)
        result = svs.classify_spec_text(text)
        assert result["status"] == "satisfied"
        assert result["matched_section"] == "## Real-world verification"


# ---------------------------------------------------------------------------
# matched_section reported even when commands are empty (AC9's dependency)
# ---------------------------------------------------------------------------


class TestMatchedSectionWithoutCommands:
    def test_recognised_heading_with_zero_commands_reports_heading(self):
        text = "## Real-world verification\n\nJust prose, no backtick spans at all.\n"
        result = svs.classify_spec_text(text)
        assert result["status"] == "absent"
        assert result["matched_section"] == "## Real-world verification"
        assert result["commands"] == []

    def test_no_heading_at_all_reports_none(self):
        text = "## Spec\n\nSome spec text without verification.\n"
        result = svs.classify_spec_text(text)
        assert result["status"] == "absent"
        assert result["matched_section"] is None


# ---------------------------------------------------------------------------
# AC10: mutation proof — the anti-looseness predicate is load-bearing
# ---------------------------------------------------------------------------


class TestMutationProof:
    """Invert _is_command so every backtick span counts as a command, and
    prove AC5's test fails under the mutation. This is the D#1984
    discipline applied to this PR's own tests — a mutation that leaves the
    suite green means the anti-looseness rule is not actually tested.

    Run:
        python3 -m pytest backend/tests/test_spec_verification_substance.py -q
    both before and after flipping MUTATE_IS_COMMAND below, and record both
    counts in the PR body (AC10).
    """

    def test_mutated_predicate_breaks_anti_looseness_proof(self, monkeypatch):
        # The mutation: invert the predicate so every candidate qualifies.
        monkeypatch.setattr(svs, "_is_command", lambda candidate: True)

        result = svs.classify_spec_text(TestAntiLoosenessProof.PROSE_ONLY)

        # Under the real predicate this is "absent" / [] (see
        # TestAntiLoosenessProof.test_prose_only_section_is_absent). Under
        # the mutation it must NOT be — proving the real predicate is what
        # makes that test pass, not an accident of the fixture.
        assert result["status"] != "absent", (
            "mutating _is_command to always return True should make the "
            "prose-only fixture satisfy the gate — if it doesn't, AC5's "
            "test does not actually depend on the anti-looseness predicate"
        )
        assert result["commands"] != []
