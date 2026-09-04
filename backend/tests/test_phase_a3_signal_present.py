"""Real-data signal test for Phase A.3 classifiers (Discussion #511, AC #3).

Loads the last 7d of real transcripts from /tmp/claude-* (matching run_analyst
default window) and asserts each new classifier fires >= 1 time. Uses 7d not 24h
because rare violations (e.g. preflight_skipped) may not appear in any given 24h
window when agents are well-behaved that day. If real sessions have no transcripts,
the test is skipped (not failed) — guards against CI environments without live data.

HARD RULE: This test MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from transcript_reader import iter_transcripts
from run_analyst import (
    classify_git_rm_usage,
    classify_preflight_skipped,
    classify_sensitive_file_unlabeled,
    classify_tool_output_ignored,
    classify_lied_exit_code,
    classify_claim_transcript_mismatch,
    classify_bash_retry_cosmetic_variants,
    _run_phase_a3_classifiers,
    _CLAIM_PAT,
    Finding,
)

SINCE_SECONDS = 7 * 24 * 3600  # 7 days — matches run_analyst default; 24h may miss rare patterns

PHASE_A3_CLASSIFIERS = [
    ("classify_git_rm_usage", classify_git_rm_usage),
    ("classify_preflight_skipped", classify_preflight_skipped),
    ("classify_sensitive_file_unlabeled", classify_sensitive_file_unlabeled),
    ("classify_tool_output_ignored", classify_tool_output_ignored),
    ("classify_lied_exit_code", classify_lied_exit_code),
    ("classify_claim_transcript_mismatch", classify_claim_transcript_mismatch),
    ("classify_bash_retry_cosmetic_variants", classify_bash_retry_cosmetic_variants),
]


def _get_real_transcripts() -> list[list]:
    """Load all transcripts in window into buffered turn lists. Returns [] if none found."""
    result = []
    for path, turns_iter in iter_transcripts(since_seconds=SINCE_SECONDS):
        turns = list(turns_iter)
        if turns:
            result.append(turns)
    return result


@pytest.fixture(scope="module")
def real_transcripts():
    transcripts = _get_real_transcripts()
    if not transcripts:
        pytest.skip("No real transcript data found in last 24h — skipping signal test")
    return transcripts


def test_phase_a3_bulk_findings_nonzero(real_transcripts):
    """_run_phase_a3_classifiers produces at least 1 finding across all 7d transcripts."""
    findings = _run_phase_a3_classifiers(since_seconds=SINCE_SECONDS)
    assert len(findings) >= 1, (
        "Phase A.3 classifiers found 0 findings in 7d transcripts. "
        "Either sessions are all well-behaved or regexes need tuning."
    )


def _corpus_has_claim_shaped_statements(transcripts: list[list]) -> bool:
    """Return True if any assistant turn in the corpus has a claim-shaped statement
    (matches _CLAIM_PAT with a path-like token) — qualifying data for the
    classify_claim_transcript_mismatch canary."""
    for turns in transcripts:
        for t in turns:
            if t.role != "assistant":
                continue
            for m in _CLAIM_PAT.finditer(t.text):
                c = m.group(1).strip("`'\"")
                if len(c) > 5 and ("/" in c or "_" in c):
                    return True
    return False


@pytest.mark.parametrize("classifier_name,classifier_fn", PHASE_A3_CLASSIFIERS)
def test_classifier_signal_present(classifier_name, classifier_fn, real_transcripts):
    """Each individual Phase A.3 classifier fires >= 1 time against 24h session data.

    Exception: classify_claim_transcript_mismatch skips when the corpus contains no
    claim-shaped statements (claim verb + path-like token) — the classifier cannot fire
    on data that contains no qualifying input.  This mirrors the top-level
    test_phase_a3_bulk_findings_nonzero skip-on-empty pattern.  The other 6 classifiers
    are not allowed to skip: they match patterns (git rm, preflight skip, etc.) that
    always appear in a healthy 7d corpus.
    """
    if classifier_name == "classify_claim_transcript_mismatch":
        if not _corpus_has_claim_shaped_statements(real_transcripts):
            pytest.skip(
                "No claim-shaped statements (claim verb + path token) found in 7d corpus "
                "— corpus is well-behaved; classify_claim_transcript_mismatch has no qualifying data"
            )

    all_findings: list[Finding] = []
    for turns in real_transcripts:
        all_findings.extend(classifier_fn(iter(turns)))

    assert len(all_findings) >= 1, (
        f"Classifier {classifier_name} found 0 findings in {len(real_transcripts)} "
        f"real transcripts from the last 7d. "
        f"Either the pattern never occurs (regression in signal) or the regex is wrong."
    )
