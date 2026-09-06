"""Tests for scripts/lib/pr_intake_gate.py — D#2404, the PR-author gate.

These cover the gate's decision surface. The *loop behaviour* it produces —
no agent spawned, no label applied — is asserted separately in
tests/test_pr_pickup_gate_loop.sh, which drives the real pickup path with a
stubbed `gh`; a helper returning the right boolean is not the acceptance
criterion (D#2377).

Coverage:
  AC1  external author, no approval          -> blocked
  AC2  internal author                        -> not blocked, no security force
  AC3  provenance label never confers trust; an approval applied by the PR
       author is not an approval
  AC4  approval by a trusted account          -> flows, security_required
  fail-closed: unreadable PR, unreadable timeline, unresolvable trust set,
       missing author
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import intake_baseline  # noqa: E402
import pr_intake_gate as gate  # noqa: E402

SLUG = "example-org/example-code"
TRUST = {"team-bot", "example-owner"}

#: D#2421 — fixed 40-char hex heads, distinct from each other.
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40


def _gh_fake(*, author="drive-by", labels=(), events=None, fail_pr=False, fail_events=False,
             head_sha=HEAD_A, pr=7, record_calls=None):
    """A stand-in for the `gh` runner. Returns JSON strings; raises where the
    real one would raise, so fail-closed paths are exercised rather than
    described.

    ``head_sha`` (D#2421) rides along on the same `pulls/{pr}` response —
    defaults to a fixed value so every pre-existing call site keeps working
    unchanged; tests that care about head movement pass distinct SHAs.
    """

    def _call(args):
        if record_calls is not None:
            record_calls.append(list(args))
        joined = " ".join(args)
        if f"pulls/{pr}" in joined or joined.endswith(f"pulls/{pr}"):
            if fail_pr:
                raise RuntimeError("gh api failed (exit 1): Not Found")
            user = {"login": author, "id": 4242} if author is not None else None
            body = {"user": user, "labels": [{"name": n} for n in labels]}
            if head_sha is not None:
                body["head"] = {"sha": head_sha}
            return json.dumps(body)
        if "/events" in joined:
            if fail_events:
                raise RuntimeError("gh api failed (exit 1): timeline unavailable")
            return json.dumps(events or [])
        raise AssertionError(f"unexpected gh call: {joined}")

    return _call


def _labeled_event(actor, created_at="2026-09-05T10:00:00Z", name="intake-approved"):
    return {
        "event": "labeled",
        "created_at": created_at,
        "label": {"name": name},
        "actor": {"login": actor},
    }


# ---------------------------------------------------------------------------
# AC1 — a PR from outside the trust set is blocked
# ---------------------------------------------------------------------------


def test_external_author_without_approval_is_blocked():
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author="drive-by"), allowlist=TRUST)
    assert result["blocked"] is True
    assert result["reason"] == gate.REASON_AWAITING
    assert result["provenance"] == "external"
    assert result["security_required"] is True


def test_missing_author_is_blocked():
    """A deleted account / ghost has no login. Untrusted, never trusted-by-default."""
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author=None), allowlist=TRUST)
    assert result["blocked"] is True
    assert result["provenance"] == "external"


# ---------------------------------------------------------------------------
# AC2 — the no-op direction: our own PRs flow exactly as before
# ---------------------------------------------------------------------------


def test_internal_author_flows():
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author="team-bot"), allowlist=TRUST)
    assert result["blocked"] is False
    assert result["reason"] == gate.REASON_INTERNAL
    assert result["provenance"] == "internal"
    # HG-7 parity: an internal PR does NOT gain a mandatory security review.
    assert result["security_required"] is False


def test_internal_author_case_insensitive():
    """GitHub logins are unique case-insensitively, so Team-Bot is team-bot."""
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author="Team-Bot"), allowlist=TRUST)
    assert result["blocked"] is False
    assert result["provenance"] == "internal"


def test_internal_author_with_no_labels_needs_no_timeline_read():
    """The daily path must not gain an API call. The fake raises on any call
    other than the single PR read, so a timeline fetch here would fail loudly."""

    def _only_pr(args):
        assert "/events" not in " ".join(args), "internal PR should not read the timeline"
        return json.dumps({"user": {"login": "team-bot", "id": 1}, "labels": []})

    assert gate.check_pr(7, SLUG, gh=_only_pr, allowlist=TRUST)["blocked"] is False


# ---------------------------------------------------------------------------
# AC3 — live identity decides; labels do not
# ---------------------------------------------------------------------------


def test_provenance_internal_label_does_not_confer_trust():
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(author="drive-by", labels=("provenance:internal",)),
        allowlist=TRUST,
    )
    assert result["blocked"] is True
    assert result["provenance"] == "external"


def test_intake_approved_applied_by_the_pr_author_is_not_an_approval():
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(
            author="drive-by",
            labels=("intake-approved",),
            events=[_labeled_event("drive-by")],
        ),
        allowlist=TRUST,
    )
    assert result["blocked"] is True
    assert result["reason"] == gate.REASON_UNTRUSTED_APPROVER


def test_latest_labeled_event_wins(tmp_path):
    """A maintainer re-applying after the author's own attempt approves it."""
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(
            author="drive-by",
            labels=("intake-approved",),
            events=[
                _labeled_event("drive-by", "2026-09-05T09:00:00Z"),
                _labeled_event("example-owner", "2026-09-05T11:00:00Z"),
            ],
        ),
        allowlist=TRUST,
        baseline_path=tmp_path / "pr-baselines.json",
    )
    assert result["blocked"] is False
    assert result["reason"] == gate.REASON_APPROVED


def test_label_event_for_a_different_label_is_ignored():
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(
            author="drive-by",
            labels=("intake-approved",),
            events=[_labeled_event("example-owner", name="needs-boss")],
        ),
        allowlist=TRUST,
    )
    assert result["blocked"] is True
    assert result["reason"] == gate.REASON_UNTRUSTED_APPROVER


# ---------------------------------------------------------------------------
# AC4 — approved external PR flows, and forces security review
# ---------------------------------------------------------------------------


def test_human_approved_external_pr_flows_and_forces_security_review(tmp_path):
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(
            author="drive-by",
            labels=("intake-approved",),
            events=[_labeled_event("example-owner")],
        ),
        allowlist=TRUST,
        baseline_path=tmp_path / "pr-baselines.json",
    )
    assert result["blocked"] is False
    assert result["reason"] == gate.REASON_APPROVED
    assert result["security_required"] is True


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_unreadable_pr_blocks():
    result = gate.check_pr(7, SLUG, gh=_gh_fake(fail_pr=True), allowlist=TRUST)
    assert result["blocked"] is True
    assert result["reason"] == gate.REASON_PR_UNREADABLE
    assert result["security_required"] is True


def test_unreadable_timeline_blocks_an_otherwise_approved_pr():
    result = gate.check_pr(
        7,
        SLUG,
        gh=_gh_fake(author="drive-by", labels=("intake-approved",), fail_events=True),
        allowlist=TRUST,
    )
    assert result["blocked"] is True
    assert result["reason"] == gate.REASON_TIMELINE_UNREADABLE


def test_unresolvable_trust_set_blocks(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("collaborators API unreachable")

    monkeypatch.setattr(gate, "resolve_allowlist", _boom)
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author="team-bot"))
    assert result["blocked"] is True
    assert result["reason"] == "trust_set_unresolvable"
    assert result["security_required"] is True


def test_empty_trust_set_blocks_everyone():
    """resolve_allowlist() fails closed to the bot/boss base; an empty set here
    means nobody is trusted, and nothing should slip through it."""
    result = gate.check_pr(7, SLUG, gh=_gh_fake(author="team-bot"), allowlist=set())
    assert result["blocked"] is True


# ---------------------------------------------------------------------------
# D#2421 — intake-approved bound to the commit SHA that was approved, not
# just the PR number. AC-1..AC-11, driven through the real check_pr with the
# head actually moving (Spec: "None of them is satisfied by a grep, a
# symbol-existence check, or a collected-test count").
# ---------------------------------------------------------------------------


def _approved_events(actor="example-owner"):
    return [_labeled_event(actor)]


def test_ac1_head_move_after_approval_blocks(tmp_path):
    store = tmp_path / "pr-baselines.json"

    r1 = gate.check_pr(
        7, SLUG,
        gh=_gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A),
        allowlist=TRUST, baseline_path=store,
    )
    assert r1["blocked"] is False

    r2 = gate.check_pr(
        7, SLUG,
        gh=_gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B),
        allowlist=TRUST, baseline_path=store,
    )
    assert r2["blocked"] is True
    assert r2["reason"] == gate.REASON_HEAD_CHANGED


def test_ac2_no_false_positive_on_repeated_calls(tmp_path):
    """An invalidation that fires on every call is not a fix."""
    store = tmp_path / "pr-baselines.json"
    gh = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    for _ in range(3):
        r = gate.check_pr(7, SLUG, gh=gh, allowlist=TRUST, baseline_path=store)
        assert r["blocked"] is False
        assert r["reason"] == gate.REASON_APPROVED


def test_ac3_sha_equality_and_zero_net_new_api_calls(tmp_path):
    """Two heads describing an identical tree (an amended commit) still
    invalidate — SHA equality, never tree equality — and the recorded gh
    calls are exactly the two the module already made, no third call."""
    store = tmp_path / "pr-baselines.json"
    gate.check_pr(
        7, SLUG,
        gh=_gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A),
        allowlist=TRUST, baseline_path=store,
    )

    calls: list = []
    r2 = gate.check_pr(
        7, SLUG,
        gh=_gh_fake(
            labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B,
            record_calls=calls,
        ),
        allowlist=TRUST, baseline_path=store,
    )
    assert r2["blocked"] is True
    assert calls == [
        ["api", f"repos/{SLUG}/pulls/7"],
        ["api", "--paginate", f"repos/{SLUG}/issues/7/events"],
    ]


def test_ac4_baseline_verdict_excludes_absent_and_none(tmp_path, monkeypatch):
    """Spy on should_block_spawn as bound inside pr_intake_gate across four
    store states: no row, matching row, drifted row, and store ok=False.
    The recorded values are a subset of {match, drifted, unknown} — "absent"
    and None never appear."""
    store = tmp_path / "pr-baselines.json"
    recorded: list = []
    original = gate.should_block_spawn

    def _spy(*args, **kwargs):
        recorded.append(kwargs.get("baseline_verdict"))
        return original(*args, **kwargs)

    monkeypatch.setattr(gate, "should_block_spawn", _spy)

    # 1. no row (first observation)
    gh_a = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=store)

    # 2. matching row
    gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=store)

    # 3. drifted row
    gh_b = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B)
    gate.check_pr(7, SLUG, gh=gh_b, allowlist=TRUST, baseline_path=store)

    # 4. store ok=False (marker present, store file missing)
    unreadable = tmp_path / "missing" / "pr-baselines.json"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    marker = unreadable.with_name(f".{unreadable.name}.initialized")
    marker.write_text("2026-01-01T00:00:00+00:00")
    gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=unreadable)

    assert set(recorded) <= {"match", "drifted", "unknown"}
    assert "absent" not in recorded
    assert None not in recorded


def test_ac5_record_baseline_failure_on_first_observation(tmp_path, monkeypatch):
    store = tmp_path / "pr-baselines.json"
    gh = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(intake_baseline, "record_baseline", _boom)

    r = gate.check_pr(7, SLUG, gh=gh, allowlist=TRUST, baseline_path=store)
    assert r["blocked"] is True
    assert r["reason"] == "external_pr_head_unrecorded"
    assert str(store) not in r["reason"]


def test_ac5_store_unreadable(tmp_path):
    unreadable = tmp_path / "missing2" / "pr-baselines.json"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    marker = unreadable.with_name(f".{unreadable.name}.initialized")
    marker.write_text("2026-01-01T00:00:00+00:00")

    gh = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    r = gate.check_pr(7, SLUG, gh=gh, allowlist=TRUST, baseline_path=unreadable)
    assert r["blocked"] is True
    assert str(unreadable) not in r["reason"]


def test_ac6_ceiling_trips_on_third_distinct_new_head_and_survives_rebaseline(tmp_path):
    store = tmp_path / "pr-baselines.json"
    heads = [HEAD_A, HEAD_B, HEAD_C, HEAD_D]
    results = []
    for h in heads:
        gh = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=h)
        results.append(gate.check_pr(7, SLUG, gh=gh, allowlist=TRUST, baseline_path=store))

    assert results[0]["blocked"] is False  # baseline established at HEAD_A
    assert results[1]["blocked"] is True and results[1]["reason"] == gate.REASON_HEAD_CHANGED
    assert results[2]["blocked"] is True and results[2]["reason"] == gate.REASON_HEAD_CHANGED
    assert results[3]["blocked"] is True
    assert results[3]["reason"] == gate.REASON_CEILING

    # Recovery after the ceiling is reached leaves the PR blocked with the
    # same reason — the count survives re-baselining.
    gh_current = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_D)
    rb = gate.rebaseline_pr(7, SLUG, gh=gh_current, baseline_path=store)
    assert rb["ok"] is True

    r_after = gate.check_pr(7, SLUG, gh=gh_current, allowlist=TRUST, baseline_path=store)
    assert r_after["blocked"] is True
    assert r_after["reason"] == gate.REASON_CEILING


def test_ac7_ceiling_counts_distinct_heads_not_poll_passes(tmp_path):
    store = tmp_path / "pr-baselines.json"
    gh_a = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=store)

    gh_b = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B)
    for _ in range(10):
        gate.check_pr(7, SLUG, gh=gh_b, allowlist=TRUST, baseline_path=store)

    entry = intake_baseline.get_entry(f"{SLUG}#7", path=store)
    assert entry is not None
    assert entry["invalidation_count"] == 1


def test_ac8_pr_and_discussion_stores_never_collide(tmp_path):
    """Even when both planes resolve to the same slug (the code_repo revert
    path CLAUDE.md documents), a PR baseline and a Discussion baseline for
    the same number never share a row — because they never share a file."""
    pr_store = tmp_path / "pr-baselines.json"
    disc_store = tmp_path / "disc-baselines.json"
    slug = "same-org/same-repo"
    number = 42
    key = f"{slug}#{number}"

    intake_baseline.record_baseline(
        key, content_sha256="discussion-content-hash", last_edited_at=None,
        edit_count=0, editor="alice", path=disc_store,
    )

    gh = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A, pr=number)
    gate.check_pr(number, slug, gh=gh, allowlist=TRUST, baseline_path=pr_store)

    disc_entry = intake_baseline.get_entry(key, path=disc_store)
    assert disc_entry is not None
    assert disc_entry["content_sha256"] == "discussion-content-hash"

    pr_entry = intake_baseline.get_entry(key, path=pr_store)
    assert pr_entry is not None
    assert pr_entry["content_sha256"] == HEAD_A


def test_ac9_security_required_stays_true_in_every_outcome(tmp_path):
    store = tmp_path / "pr-baselines.json"

    gh_a = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    assert gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=store)["security_required"] is True

    gh_b = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B)
    assert gate.check_pr(7, SLUG, gh=gh_b, allowlist=TRUST, baseline_path=store)["security_required"] is True

    gh_c = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_C)
    gate.check_pr(7, SLUG, gh=gh_c, allowlist=TRUST, baseline_path=store)
    gh_d = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_D)
    r_ceiling = gate.check_pr(7, SLUG, gh=gh_d, allowlist=TRUST, baseline_path=store)
    assert r_ceiling["reason"] == gate.REASON_CEILING
    assert r_ceiling["security_required"] is True

    unreadable = tmp_path / "missing3" / "pr-baselines.json"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    marker = unreadable.with_name(f".{unreadable.name}.initialized")
    marker.write_text("2026-01-01T00:00:00+00:00")
    r_unreadable = gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=unreadable)
    assert r_unreadable["security_required"] is True


def test_ac10_internal_pr_touches_no_pr_head_store():
    """An internal PR pays nothing: the baseline store path is never even
    dereferenced. Points baseline_path at an object that raises on any
    attribute access, so a touch would fail the test rather than pass
    silently."""

    class _ExplodingPath:
        def __getattr__(self, item):
            raise AssertionError(f"pr_head_baseline store touched for an internal PR: {item}")

    gh = _gh_fake(author="team-bot", labels=(), head_sha=HEAD_A)
    r = gate.check_pr(7, SLUG, gh=gh, allowlist=TRUST, baseline_path=_ExplodingPath())
    assert r["blocked"] is False
    assert r["reason"] == gate.REASON_INTERNAL
    assert r["security_required"] is False


def test_ac11_recovery_below_ceiling_returns_to_approved(tmp_path):
    store = tmp_path / "pr-baselines.json"
    gh_a = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_A)
    gate.check_pr(7, SLUG, gh=gh_a, allowlist=TRUST, baseline_path=store)

    gh_b = _gh_fake(labels=("intake-approved",), events=_approved_events(), head_sha=HEAD_B)
    r_drift = gate.check_pr(7, SLUG, gh=gh_b, allowlist=TRUST, baseline_path=store)
    assert r_drift["blocked"] is True

    rb = gate.rebaseline_pr(7, SLUG, gh=gh_b, baseline_path=store)
    assert rb["ok"] is True

    r_after = gate.check_pr(7, SLUG, gh=gh_b, allowlist=TRUST, baseline_path=store)
    assert r_after["blocked"] is False
    assert r_after["reason"] == gate.REASON_APPROVED


# ---------------------------------------------------------------------------
# CLI exit-code contract — loop-phased-step5.sh branches on these numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,expected_rc,expected_out",
    [
        ({"security_required": True, "reason": "external_approved", "blocked": False}, 0, "true"),
        ({"security_required": False, "reason": "internal", "blocked": False}, 1, "false"),
        ({"security_required": True, "reason": gate.REASON_PR_UNREADABLE, "blocked": True}, 3, "unknown"),
        ({"security_required": True, "reason": "trust_set_unresolvable", "blocked": True}, 3, "unknown"),
    ],
)
def test_security_required_pr_exit_codes(monkeypatch, capsys, verdict, expected_rc, expected_out):
    monkeypatch.setattr(gate, "check_pr", lambda *_a, **_k: verdict)
    rc = gate._main(["pr_intake_gate.py", "security-required-pr", "7"])
    assert rc == expected_rc
    assert capsys.readouterr().out.strip() == expected_out


def test_check_pr_cli_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check_pr", lambda *_a, **_k: {"blocked": True, "reason": "x"})
    assert gate._main(["pr_intake_gate.py", "check-pr", "7"]) == 1
    assert json.loads(capsys.readouterr().out)["blocked"] is True

    monkeypatch.setattr(gate, "check_pr", lambda *_a, **_k: {"blocked": False, "reason": "internal"})
    assert gate._main(["pr_intake_gate.py", "check-pr", "7"]) == 0


def test_cli_usage_errors_are_not_a_pass():
    """Exit 2, never 0 — a caller that treats "not blocked" as 0 must not read
    a malformed invocation as permission."""
    assert gate._main(["pr_intake_gate.py"]) == 2
    assert gate._main(["pr_intake_gate.py", "check-pr"]) == 2
    assert gate._main(["pr_intake_gate.py", "check-pr", "not-a-number"]) == 2
    assert gate._main(["pr_intake_gate.py", "check-pr", "7", "--repo"]) == 2
